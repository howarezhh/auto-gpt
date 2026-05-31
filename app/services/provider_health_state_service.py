from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


class ProviderHealthStateService:
    """维护 provider/model 路由热路径可直接读取的 Redis 健康状态。"""

    STATE_TTL_SECONDS = 60 * 60
    CAPABILITY_STATE_TTL_SECONDS = 60 * 60
    ROUTE_METRIC_TTL_SECONDS = 10 * 60
    ROUTE_METRIC_WINDOW_MINUTES = 5
    EWMA_ALPHA = 0.3

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
        health_status = "healthy" if success else ("unhealthy" if force_unhealthy else "degraded")
        circuit_state = "closed" if success else ("open" if force_unhealthy else provider_model.circuit_state)
        cls.record_model_probe(
            provider,
            provider_model,
            success=success,
            health_status=health_status,
            circuit_state=circuit_state,
            latency_ms=latency_ms,
            ttfb_ms=ttfb_ms,
        )
        payload = cls.get_model_state(provider.id, provider_model.id) or {}
        payload.update(
            {
                "success_rate_5m": rates["success_rate_5m"],
                "failure_rate_5m": rates["failure_rate_5m"],
                "last_error": None if success else str(error_message or "")[:500],
            }
        )
        payload["health_score"] = cls._health_score(payload)
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
        return int(datetime.utcnow().timestamp() // 60) + offset

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

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
            RedisService.get_sync_client().setex(key, ttl_seconds, dumps_json(payload))
        except Exception:
            return

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
