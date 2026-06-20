from __future__ import annotations

import asyncio
import hashlib
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from app.services.cache_service import CacheService
from app.services.routing.context import RoutePolicyContext
from app.utils.json_utils import dumps_json
from app.utils.timezone import BEIJING_TZ, now_beijing_aware


class RouteOutageService:
    ROUTE_OUTAGE_CACHE_PREFIX = "route-outage"
    ROUTE_OUTAGE_CACHE_MAX_TTL_SECONDS = 300

    @staticmethod
    def retry_infinite_enabled(setting: Any, route_context: RoutePolicyContext | None = None) -> bool:
        return bool(getattr(setting, "route_exhausted_retry_infinite_enabled", False))

    @staticmethod
    def max_wait_seconds(setting: Any) -> int:
        value = getattr(setting, "route_exhausted_retry_max_wait_seconds", 600)
        try:
            return max(0, min(int(value or 0), 600))
        except (TypeError, ValueError):
            return 600

    @staticmethod
    def elapsed_seconds(*, started_at: float) -> float:
        return max(0.0, time.perf_counter() - started_at)

    @staticmethod
    def should_retry_diagnostics(diagnostics: dict[str, Any] | None) -> bool:
        if not diagnostics:
            return False
        matching_model_mount_count = int(diagnostics.get("matching_model_mount_count") or 0)
        pre_capacity_candidate_count = int(diagnostics.get("pre_capacity_candidate_count") or 0)
        final_candidate_count = int(diagnostics.get("final_candidate_count") or 0)
        reason_counts = diagnostics.get("reason_counts") or {}
        if matching_model_mount_count <= 0:
            return False
        if pre_capacity_candidate_count > 0 and final_candidate_count == 0:
            return True
        nonrecoverable_match_reasons = {
            "provider_not_authorized",
            "model_disabled",
            "model_globally_disabled",
            "vision_not_supported",
            "image_generation_not_supported",
            "vision_probe_unhealthy",
            "image_generation_probe_unhealthy",
            "chat_not_supported",
            "responses_not_supported",
        }
        nonrecoverable_match_count = sum(int(reason_counts.get(reason) or 0) for reason in nonrecoverable_match_reasons)
        if pre_capacity_candidate_count <= 0 and nonrecoverable_match_count >= matching_model_mount_count:
            return False
        recoverable_reasons = {
            "provider_capacity_exceeded",
            "capacity_snapshot_unavailable",
            "provider_circuit_open",
            "model_circuit_open",
            "model_unhealthy",
        }
        return any(int(reason_counts.get(reason) or 0) > 0 for reason in recoverable_reasons)

    @classmethod
    def build_retry_exhausted_upstream_error(
        cls,
        setting: Any,
        *,
        started_at: float,
        attempt_count: int,
        trace_id: str | None,
        last_upstream_error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        elapsed_seconds = int(round(cls.elapsed_seconds(started_at=started_at)))
        max_wait_seconds = cls.max_wait_seconds(setting)
        detail: dict[str, Any] = {
            "message": f"所有可用提供商在 {max_wait_seconds} 秒等待重试窗口内均不可用或请求失败，已停止内部重试。",
            "code": "all_providers_unavailable_after_retry",
            "attempt_count": attempt_count,
            "elapsed_seconds": elapsed_seconds,
            "max_wait_seconds": max_wait_seconds,
            "retryable": True,
        }
        if trace_id:
            detail["trace_id"] = trace_id
        if last_upstream_error is not None:
            detail["last_status_code"] = last_upstream_error.get("status_code")
            detail["last_error"] = last_upstream_error.get("detail")
        return {
            "status_code": 503,
            "detail": detail,
        }

    @classmethod
    def sleep_seconds(
        cls,
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> float:
        return float(
            cls.wait_plan(
                setting,
                started_at=started_at,
                retry_round=retry_round,
                route_context=route_context,
                upstream_error=upstream_error,
            ).get("sleep_seconds")
            or 0.0
        )

    @classmethod
    def wait_plan(
        cls,
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        backoff_seconds = (1, 2, 5, 10, 15, 30)
        retry_after_seconds = cls.retry_after_seconds_from_upstream_error(upstream_error)
        base_wait_seconds = float(backoff_seconds[min(max(0, retry_round), len(backoff_seconds) - 1)])
        jitter_ratio: float | None = None
        retry_after_jitter_seconds: float | None = None
        wait_source = "retry_after" if retry_after_seconds is not None else "backoff_jitter"
        if retry_after_seconds is not None:
            retry_after_jitter_seconds = random.uniform(0.0, min(0.5, max(0.05, float(retry_after_seconds) * 0.1)))
            wait_seconds = retry_after_seconds + retry_after_jitter_seconds
        else:
            jitter_ratio = random.uniform(-0.2, 0.2)
            wait_seconds = max(0.1, base_wait_seconds * (1.0 + jitter_ratio))
        elapsed_seconds = cls.elapsed_seconds(started_at=started_at)
        infinite_retry_enabled = cls.retry_infinite_enabled(setting, route_context)
        if infinite_retry_enabled:
            return {
                "sleep_seconds": round(wait_seconds, 3),
                "wait_source": wait_source,
                "base_wait_seconds": base_wait_seconds,
                "jitter_ratio": round(jitter_ratio, 6) if jitter_ratio is not None else None,
                "retry_after_jitter_seconds": round(retry_after_jitter_seconds, 3) if retry_after_jitter_seconds is not None else None,
                "retry_after_seconds": retry_after_seconds,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "max_wait_seconds": cls.max_wait_seconds(setting),
                "remaining_wait_seconds": None,
                "limited_by_wait_window": False,
                "infinite_retry_enabled": infinite_retry_enabled,
            }
        max_wait_seconds = cls.max_wait_seconds(setting)
        if max_wait_seconds <= 0:
            remaining_seconds = 0.0
            sleep_seconds = 0.0
        else:
            remaining_seconds = max_wait_seconds - elapsed_seconds
            sleep_seconds = 0.0 if remaining_seconds <= 0 else max(0.0, min(wait_seconds, remaining_seconds))
        return {
            "sleep_seconds": round(sleep_seconds, 3),
            "wait_source": wait_source,
            "base_wait_seconds": base_wait_seconds,
            "jitter_ratio": round(jitter_ratio, 6) if jitter_ratio is not None else None,
            "retry_after_jitter_seconds": round(retry_after_jitter_seconds, 3) if retry_after_jitter_seconds is not None else None,
            "retry_after_seconds": retry_after_seconds,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "max_wait_seconds": max_wait_seconds,
            "remaining_wait_seconds": round(max(0.0, remaining_seconds), 3),
            "limited_by_wait_window": bool(sleep_seconds > 0 and sleep_seconds < wait_seconds),
            "infinite_retry_enabled": infinite_retry_enabled,
        }

    @classmethod
    def cache_key(
        cls,
        *,
        endpoint_path: str,
        model_name: Any,
        route_context: RoutePolicyContext | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
    ) -> str:
        payload = {
            "endpoint_path": endpoint_path,
            "model_name": model_name if isinstance(model_name, str) else None,
            "require_vision": require_vision,
            "require_stream": require_stream,
            "require_tools": require_tools,
            "require_image_generation": require_image_generation,
            "require_chat_completions": require_chat_completions,
            "require_responses": require_responses,
            "forced_provider_id": route_context.forced_provider_id if route_context else None,
            "allowed_provider_ids": route_context.allowed_provider_ids if route_context else None,
            "content_guard_required": route_context.content_guard_required if route_context else None,
            "require_trusted_provider": route_context.require_trusted_provider if route_context else None,
        }
        digest = hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest()[:32]
        return f"{cls.ROUTE_OUTAGE_CACHE_PREFIX}:{digest}"

    @classmethod
    def remember(
        cls,
        *,
        endpoint_path: str,
        model_name: Any,
        route_context: RoutePolicyContext | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
        reason: str,
        retry_wait_plan: dict[str, Any],
        upstream_error: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
        error_code_resolver: Callable[[Any], str | None] | None = None,
    ) -> None:
        sleep_seconds = float(retry_wait_plan.get("sleep_seconds") or 0.0)
        if sleep_seconds <= 0:
            return
        key = cls.cache_key(
            endpoint_path=endpoint_path,
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        now = time.time()
        ttl_seconds = max(1, min(cls.ROUTE_OUTAGE_CACHE_MAX_TTL_SECONDS, int(sleep_seconds) + 5))
        detail = upstream_error.get("detail") if upstream_error else None
        CacheService.set(
            key,
            {
                "reason": reason,
                "created_at": now,
                "next_retry_at": now + sleep_seconds,
                "retry_wait_plan": retry_wait_plan,
                "last_status_code": upstream_error.get("status_code") if upstream_error else None,
                "last_error_code": error_code_resolver(detail) if error_code_resolver is not None and upstream_error else None,
                "diagnostic_summary": diagnostics.get("summary") if diagnostics else None,
                "reason_counts": diagnostics.get("reason_counts") if diagnostics else None,
            },
            ttl_seconds=ttl_seconds,
        )

    @classmethod
    async def maybe_wait(
        cls,
        trace: list[dict],
        *,
        endpoint_path: str,
        model_name: Any,
        route_context: RoutePolicyContext | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
        setting: Any,
        route_retry_started_at: float,
        route_retry_round: int,
    ) -> bool:
        key = cls.cache_key(
            endpoint_path=endpoint_path,
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        outage = CacheService.get(key)
        if not isinstance(outage, dict):
            return False
        next_retry_at = float(outage.get("next_retry_at") or 0.0)
        sleep_seconds = max(0.0, next_retry_at - time.time())
        if sleep_seconds <= 0:
            CacheService.invalidate(key)
            return False
        if not cls.retry_infinite_enabled(setting, route_context):
            remaining = cls.max_wait_seconds(setting) - cls.elapsed_seconds(started_at=route_retry_started_at)
            if remaining <= 0:
                return False
            sleep_seconds = min(sleep_seconds, max(0.0, remaining))
        trace.append(
            {
                "result": "route_outage_backoff_wait",
                "reason": outage.get("reason"),
                "retry_round": route_retry_round + 1,
                "sleep_seconds": round(sleep_seconds, 3),
                "diagnostic_summary": outage.get("diagnostic_summary"),
                "reason_counts": outage.get("reason_counts"),
                "last_status_code": outage.get("last_status_code"),
                "last_error_code": outage.get("last_error_code"),
                "retry_wait_plan": outage.get("retry_wait_plan"),
            }
        )
        await asyncio.sleep(sleep_seconds)
        return True

    @classmethod
    def retry_after_seconds_from_upstream_error(cls, upstream_error: dict[str, Any] | None) -> float | None:
        if not isinstance(upstream_error, dict):
            return None
        parsed = cls.parse_retry_after_seconds(upstream_error.get("retry_after_seconds"))
        if parsed is not None:
            return parsed
        return cls.retry_after_seconds_from_detail(upstream_error.get("detail"))

    @classmethod
    def retry_after_seconds_from_detail(cls, detail: Any) -> float | None:
        if not isinstance(detail, dict):
            return None
        for key in ("retry_after_seconds", "retry_after_sec", "retry_after_ms", "retry_after", "Retry-After"):
            if key not in detail:
                continue
            parsed = cls.parse_retry_after_seconds(detail.get(key), milliseconds=key == "retry_after_ms")
            if parsed is not None:
                return parsed
        error = detail.get("error")
        if isinstance(error, dict):
            return cls.retry_after_seconds_from_detail(error)
        return None

    @staticmethod
    def parse_retry_after_seconds(value: Any, *, milliseconds: bool = False) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000.0 if milliseconds else float(value)
            return max(0.0, min(seconds, 300.0))
        text = str(value).strip()
        if not text:
            return None
        try:
            seconds = float(text)
            if milliseconds:
                seconds /= 1000.0
            return max(0.0, min(seconds, 300.0))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=BEIJING_TZ)
            seconds = (retry_at.astimezone(BEIJING_TZ) - now_beijing_aware()).total_seconds()
            return max(0.0, min(seconds, 300.0))
        except Exception:
            return None
