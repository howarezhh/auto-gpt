from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


class ProviderHealthStateService:
    """维护 provider/model 路由热路径可直接读取的 Redis 可用状态。"""

    FIXED_SUCCESS_RESPONSE_ERROR_CODE = "fixed_success_response_detected"
    STATE_TTL_SECONDS = 60 * 60
    CAPABILITY_STATE_TTL_SECONDS = 60 * 60
    ROUTE_METRIC_TTL_SECONDS = 10 * 60
    ROUTE_METRIC_WINDOW_MINUTES = 5
    EWMA_ALPHA = 0.3
    HEALTH_TO_AVAILABILITY = {
        "healthy": "available",
        "degraded": "degraded",
        "unknown": "unknown",
        "unhealthy": "unavailable",
    }

    @staticmethod
    def provider_key(provider_id: int) -> str:
        return f"health:provider:{provider_id}"

    @staticmethod
    def model_key(provider_id: int, provider_model_id: int) -> str:
        return f"health:model:{provider_id}:{provider_model_id}"

    @staticmethod
    def capability_key(provider_id: int, provider_model_id: int) -> str:
        return f"health:model-capability:{provider_id}:{provider_model_id}"

    @classmethod
    def get_provider_state(cls, provider_id: int) -> dict[str, Any] | None:
        return cls._get_json(cls.provider_key(provider_id))

    @classmethod
    def get_model_state(cls, provider_id: int, provider_model_id: int) -> dict[str, Any] | None:
        return cls._get_json(cls.model_key(provider_id, provider_model_id))

    @classmethod
    def get_model_capability_state(cls, provider_id: int, provider_model_id: int) -> dict[str, Any] | None:
        return cls._get_json(cls.capability_key(provider_id, provider_model_id))

    @classmethod
    def clear_provider_runtime_state(cls, provider: Provider) -> None:
        """清理手动治理后可能覆盖数据库状态的运行时可用性缓存。"""
        keys = [cls.provider_key(provider.id)]
        for provider_model in getattr(provider, "provider_models", []) or []:
            keys.append(cls.model_key(provider.id, provider_model.id))
            keys.append(cls.capability_key(provider.id, provider_model.id))
        try:
            RedisService.get_sync_client().delete(*keys)
        except Exception:
            return

    @classmethod
    def effective_provider_health(cls, provider: Provider) -> dict[str, Any]:
        state = cls.get_provider_state(provider.id) or {}
        return cls._effective_health_payload(
            db_health=getattr(provider, "health_status", None),
            db_updated_at=getattr(provider, "updated_at", None) or getattr(provider, "last_check_at", None),
            runtime_state=state,
        )

    @classmethod
    def effective_model_health(cls, provider_model: ProviderModel) -> dict[str, Any]:
        provider_id = int(getattr(provider_model, "provider_id", 0) or 0)
        state = cls.get_model_state(provider_id, provider_model.id) if provider_id else None
        return cls._effective_health_payload(
            db_health=getattr(provider_model, "health_status", None),
            db_updated_at=getattr(provider_model, "updated_at", None) or getattr(provider_model, "last_check_at", None),
            runtime_state=state or {},
        )

    @classmethod
    def record_provider_probe(
        cls,
        provider: Provider,
        *,
        success: bool,
        latency_ms: int,
        health_status: str | None = None,
        circuit_state: str | None = None,
    ) -> None:
        now = cls._now()
        existing = cls.get_provider_state(provider.id) or {}
        payload = {
            **existing,
            "health_status": health_status or provider.health_status,
            "circuit_state": circuit_state or provider.circuit_state,
            "last_probe_ok_at": now if success else existing.get("last_probe_ok_at"),
            "last_probe_failed_at": existing.get("last_probe_failed_at") if success else now,
            "consecutive_probe_failures": 0 if success else int(existing.get("consecutive_probe_failures") or 0) + 1,
            "ewma_latency_ms": cls._ewma(existing.get("ewma_latency_ms"), latency_ms),
            "updated_at": now,
        }
        payload["health_score"] = cls._health_score(payload)
        cls._set_json(cls.provider_key(provider.id), payload, ttl_seconds=cls.STATE_TTL_SECONDS)

    @classmethod
    def record_model_probe(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        success: bool,
        health_status: str,
        circuit_state: str,
        latency_ms: int,
        ttfb_ms: int | None = None,
    ) -> None:
        now = cls._now()
        existing = cls.get_model_state(provider.id, provider_model.id) or {}
        payload = {
            **existing,
            "health_status": health_status,
            "circuit_state": circuit_state,
            "last_probe_ok_at": now if success else existing.get("last_probe_ok_at"),
            "last_probe_failed_at": existing.get("last_probe_failed_at") if success else now,
            "consecutive_probe_failures": 0 if success else int(existing.get("consecutive_probe_failures") or 0) + 1,
            "ewma_latency_ms": cls._ewma(existing.get("ewma_latency_ms"), latency_ms),
            "ewma_ttfb_ms": cls._ewma(existing.get("ewma_ttfb_ms"), ttfb_ms) if ttfb_ms is not None else existing.get("ewma_ttfb_ms"),
            "updated_at": now,
        }
        payload["health_score"] = cls._health_score(payload)
        cls._attach_availability_aliases(payload)
        cls._set_json(cls.model_key(provider.id, provider_model.id), payload, ttl_seconds=cls.STATE_TTL_SECONDS)

    @classmethod
    def record_capability_probe(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        capability: str,
        success: bool,
        latency_ms: int,
        status_code: int | None = None,
        message: str | None = None,
        support_mode: str | None = None,
    ) -> None:
        now = cls._now()
        payload = cls.get_model_capability_state(provider.id, provider_model.id) or {}
        payload[capability] = {
            "success": success,
            "native_ok": bool(success and support_mode == "native"),
            "support_mode": support_mode,
            "last_probe_ok_at": now if success else None,
            "last_probe_failed_at": None if success else now,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": str(message or "")[:500],
            "updated_at": now,
        }
        payload["updated_at"] = now
        cls._set_json(
            cls.capability_key(provider.id, provider_model.id),
            payload,
            ttl_seconds=cls.CAPABILITY_STATE_TTL_SECONDS,
        )

    @classmethod
    def record_runtime_metrics(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        metrics: dict[str, Any],
        *,
        health_status: str | None = None,
        circuit_state: str | None = None,
        status_update_reason: str | None = None,
    ) -> dict[str, Any]:
        """写入统一状态模块计算出的短窗口运行指标。

        provider_model.id 是唯一模型挂载 ID；model_name 仅作为自定义展示名保留在观测 payload 中。
        """
        now = cls._now()
        existing = cls.get_model_state(provider.id, provider_model.id) or {}
        total_requests = int(metrics.get("total_requests") or 0)
        success_rate = float(metrics.get("success_rate") if metrics.get("success_rate") is not None else 1.0)
        upstream_failure_rate = float(metrics.get("upstream_failure_rate") or 0.0)
        avg_latency_ms = metrics.get("avg_latency_ms")
        avg_ttfb_ms = metrics.get("avg_ttfb_ms")
        payload = {
            **existing,
            "recent_runtime_window_seconds": int(metrics.get("window_seconds") or 5),
            "recent_total_requests": total_requests,
            "recent_success_requests": int(metrics.get("success_requests") or 0),
            "recent_failed_requests": int(metrics.get("failed_requests") or 0),
            "recent_upstream_failure_requests": int(metrics.get("upstream_failure_requests") or 0),
            "recent_ignored_failure_requests": int(metrics.get("ignored_failure_requests") or 0),
            "recent_success_rate": success_rate,
            "recent_upstream_failure_rate": upstream_failure_rate,
            "success_rate_5m": success_rate,
            "failure_rate_5m": upstream_failure_rate,
            "recent_avg_latency_ms": avg_latency_ms,
            "recent_p95_latency_ms": metrics.get("p95_latency_ms"),
            "recent_avg_ttfb_ms": avg_ttfb_ms,
            "recent_p95_ttfb_ms": metrics.get("p95_ttfb_ms"),
            "recent_runtime_decision": metrics.get("decision"),
            "recent_runtime_confidence": metrics.get("confidence"),
            "runtime_model_name": metrics.get("model_name") or provider_model.model_name,
            "runtime_requested_model": metrics.get("requested_model"),
            "last_runtime_log_at": metrics.get("latest_log_at"),
            "last_runtime_state_update_at": now,
            "updated_at": now,
        }
        if avg_latency_ms is not None:
            payload["ewma_latency_ms"] = cls._ewma(existing.get("ewma_latency_ms"), int(float(avg_latency_ms)))
        if avg_ttfb_ms is not None:
            payload["ewma_ttfb_ms"] = cls._ewma(existing.get("ewma_ttfb_ms"), int(float(avg_ttfb_ms)))
        if metrics.get("latest_error_code") or metrics.get("latest_error_message"):
            payload.update(
                {
                    "last_error": str(metrics.get("latest_error_message") or "")[:500],
                    "last_error_code": metrics.get("latest_error_code"),
                    "last_error_category": metrics.get("latest_error_category"),
                    "last_status_code": metrics.get("latest_status_code"),
                    "last_trace_id": metrics.get("latest_trace_id"),
                    "last_runtime_error_at": metrics.get("latest_error_at"),
                }
            )
        elif total_requests > 0 and int(metrics.get("upstream_failure_requests") or 0) <= 0:
            payload.update(
                {
                    "last_error": None,
                    "last_error_code": None,
                    "last_error_category": None,
                    "last_status_code": None,
                    "last_trace_id": None,
                }
            )
        if health_status is not None:
            payload["health_status"] = health_status
            payload["runtime_health_status"] = health_status
        else:
            payload.setdefault("health_status", provider_model.health_status)
        if circuit_state is not None:
            payload["circuit_state"] = circuit_state
            payload["runtime_circuit_state"] = circuit_state
        else:
            payload.setdefault("circuit_state", provider_model.circuit_state)
        if status_update_reason:
            payload["last_runtime_state_update_reason"] = status_update_reason
        payload["health_score"] = cls._health_score(payload)
        cls._attach_availability_aliases(payload)
        cls._set_json(cls.model_key(provider.id, provider_model.id), payload, ttl_seconds=cls.STATE_TTL_SECONDS)
        return payload

    @classmethod
    def record_fixed_success_response_failure(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        latency_ms: int,
        detection: dict[str, Any],
    ) -> dict[str, Any]:
        cls._increment_route_bucket(provider.id, provider_model.id, success=False)
        rates = cls._route_rates(provider.id, provider_model.id)
        now = cls._now()
        payload = cls.get_model_state(provider.id, provider_model.id) or {}
        payload.update(
            {
                "health_status": "unhealthy",
                "runtime_health_status": "unhealthy",
                "availability_status": "unavailable",
                "runtime_availability_status": "unavailable",
                "circuit_state": "open",
                "runtime_circuit_state": "open",
                "success_rate_5m": rates["success_rate_5m"],
                "failure_rate_5m": rates["failure_rate_5m"],
                "ewma_latency_ms": cls._ewma(payload.get("ewma_latency_ms"), latency_ms),
                "last_error": cls.FIXED_SUCCESS_RESPONSE_ERROR_CODE,
                "last_error_code": cls.FIXED_SUCCESS_RESPONSE_ERROR_CODE,
                "last_error_category": "pseudo_success_response",
                "last_status_code": 200,
                "last_runtime_error_at": now,
                "last_runtime_state_update_at": now,
                "last_runtime_state_update_reason": cls.FIXED_SUCCESS_RESPONSE_ERROR_CODE,
                "fixed_success_response_detection": detection,
                "updated_at": now,
            }
        )
        payload["health_score"] = cls._health_score(payload)
        cls._attach_availability_aliases(payload)
        cls._set_json(cls.model_key(provider.id, provider_model.id), payload, ttl_seconds=cls.STATE_TTL_SECONDS)
        return payload

    @classmethod
    def record_route_success(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        latency_ms: int,
        ttfb_ms: int | None = None,
    ) -> None:
        cls._record_route_outcome(provider, provider_model, success=True, latency_ms=latency_ms, ttfb_ms=ttfb_ms)

    @classmethod
    def record_route_failure(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        latency_ms: int,
        error_message: str | None,
        force_unhealthy: bool = False,
    ) -> None:
        cls._record_route_outcome(
            provider,
            provider_model,
            success=False,
            latency_ms=latency_ms,
            error_message=error_message,
            force_unhealthy=force_unhealthy,
        )

    @classmethod
    def _record_route_outcome(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        success: bool,
        latency_ms: int,
        ttfb_ms: int | None = None,
        error_message: str | None = None,
        force_unhealthy: bool = False,
    ) -> None:
        cls._increment_route_bucket(provider.id, provider_model.id, success=success)
        rates = cls._route_rates(provider.id, provider_model.id)
        payload = cls.get_model_state(provider.id, provider_model.id) or {}
        payload.update(
            {
                "health_status": payload.get("health_status") or provider_model.health_status,
                "circuit_state": payload.get("circuit_state") or provider_model.circuit_state,
                "success_rate_5m": rates["success_rate_5m"],
                "failure_rate_5m": rates["failure_rate_5m"],
                "ewma_latency_ms": cls._ewma(payload.get("ewma_latency_ms"), latency_ms),
                "ewma_ttfb_ms": cls._ewma(payload.get("ewma_ttfb_ms"), ttfb_ms) if ttfb_ms is not None else payload.get("ewma_ttfb_ms"),
                "last_error": None if success else str(error_message or "")[:500],
                "updated_at": cls._now(),
            }
        )
        payload["health_score"] = cls._health_score(payload)
        cls._attach_availability_aliases(payload)
        cls._set_json(cls.model_key(provider.id, provider_model.id), payload, ttl_seconds=cls.STATE_TTL_SECONDS)

    @classmethod
    def _increment_route_bucket(cls, provider_id: int, provider_model_id: int, *, success: bool) -> None:
        try:
            client = RedisService.get_sync_client()
            key = cls._route_bucket_key(provider_id, provider_model_id, cls._minute_bucket())
            field = "success" if success else "failure"
            pipe = client.pipeline()
            pipe.hincrby(key, field, 1)
            pipe.expire(key, cls.ROUTE_METRIC_TTL_SECONDS)
            pipe.execute()
        except Exception:
            return

    @classmethod
    def _route_rates(cls, provider_id: int, provider_model_id: int) -> dict[str, float]:
        try:
            client = RedisService.get_sync_client()
            buckets = [cls._minute_bucket(offset=-offset) for offset in range(cls.ROUTE_METRIC_WINDOW_MINUTES)]
            keys = [cls._route_bucket_key(provider_id, provider_model_id, bucket) for bucket in buckets]
            pipe = client.pipeline()
            for key in keys:
                pipe.hgetall(key)
            values = pipe.execute()
        except Exception:
            return {"success_rate_5m": 1.0, "failure_rate_5m": 0.0}
        success_count = 0
        failure_count = 0
        for bucket in values:
            if not isinstance(bucket, dict):
                continue
            success_count += int(bucket.get("success") or 0)
            failure_count += int(bucket.get("failure") or 0)
        total = success_count + failure_count
        if total <= 0:
            return {"success_rate_5m": 1.0, "failure_rate_5m": 0.0}
        return {
            "success_rate_5m": success_count / total,
            "failure_rate_5m": failure_count / total,
        }

    @classmethod
    def _route_bucket_key(cls, provider_id: int, provider_model_id: int, minute_bucket: int) -> str:
        return f"health:route-metrics:{provider_id}:{provider_model_id}:{minute_bucket}"

    @staticmethod
    def _minute_bucket(*, offset: int = 0) -> int:
        return int(now_beijing().timestamp() // 60) + offset

    @staticmethod
    def _now() -> str:
        return now_beijing().isoformat()

    @staticmethod
    def _effective_health_payload(
        *,
        db_health: Any,
        db_updated_at: Any,
        runtime_state: dict[str, Any],
    ) -> dict[str, Any]:
        db_value = str(db_health or "unknown")
        runtime_value = runtime_state.get("runtime_health_status") or runtime_state.get("health_status")
        runtime_health = str(runtime_value) if runtime_value else None
        effective_health = runtime_health or db_value
        runtime_updated_at = (
            runtime_state.get("last_runtime_state_update_at")
            or runtime_state.get("updated_at")
            or runtime_state.get("last_probe_ok_at")
            or runtime_state.get("last_probe_failed_at")
        )
        updated_at = ProviderHealthStateService._serialize_updated_at(
            runtime_updated_at if runtime_health else db_updated_at
        )
        return {
            "db_health": db_value,
            "runtime_health": runtime_health,
            "effective_health": effective_health,
            "db_availability": ProviderHealthStateService.health_to_availability(db_value),
            "runtime_availability": ProviderHealthStateService.health_to_availability(runtime_health) if runtime_health else None,
            "effective_availability": ProviderHealthStateService.health_to_availability(effective_health),
            "state_source": "runtime" if runtime_health else "db",
            "updated_at": updated_at,
            "health_state_updated_at": updated_at,
            "availability_state_updated_at": updated_at,
        }

    @staticmethod
    def _serialize_updated_at(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @classmethod
    def _get_json(cls, key: str) -> dict[str, Any] | None:
        try:
            raw_value = RedisService.get_sync_client().get(key)
        except Exception:
            return None
        parsed = loads_json(raw_value, None) if raw_value else None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _set_json(cls, key: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        try:
            cls._attach_availability_aliases(payload)
            RedisService.get_sync_client().setex(key, ttl_seconds, dumps_json(payload))
        except Exception:
            return

    @classmethod
    def health_to_availability(cls, value: Any) -> str:
        return cls.HEALTH_TO_AVAILABILITY.get(str(value or "unknown"), "unknown")

    @classmethod
    def _attach_availability_aliases(cls, payload: dict[str, Any]) -> dict[str, Any]:
        health_status = str(payload.get("health_status") or "unknown")
        runtime_health = payload.get("runtime_health_status") or payload.get("health_status")
        payload["availability_status"] = cls.health_to_availability(health_status)
        if runtime_health:
            payload["runtime_availability_status"] = cls.health_to_availability(runtime_health)
        if "health_score" in payload:
            payload["availability_score"] = payload.get("health_score")
        return payload

    @classmethod
    def _ewma(cls, previous_value: Any, current_value: int | None) -> int | None:
        if current_value is None:
            return int(previous_value) if previous_value is not None else None
        current = max(0, int(current_value))
        if previous_value is None:
            return current
        return int((float(previous_value) * (1 - cls.EWMA_ALPHA)) + (current * cls.EWMA_ALPHA))

    @staticmethod
    def _health_score(payload: dict[str, Any]) -> float:
        base = {
            "healthy": 100.0,
            "degraded": 60.0,
            "unknown": 55.0,
            "unhealthy": 0.0,
        }.get(str(payload.get("health_status") or "unknown"), 50.0)
        if payload.get("circuit_state") == "half_open":
            base = min(base, 35.0)
        if payload.get("circuit_state") == "open":
            base = 0.0
        failure_rate = float(payload.get("failure_rate_5m") or 0.0)
        latency = float(payload.get("ewma_latency_ms") or 0.0)
        failure_penalty = min(50.0, failure_rate * 100.0)
        latency_penalty = min(20.0, latency / 200.0)
        return round(max(0.0, min(100.0, base - failure_penalty - latency_penalty)), 2)

from app.utils.timezone import now_beijing
