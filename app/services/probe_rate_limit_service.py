from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.redis_service import RedisService


@dataclass(frozen=True)
class ProbeRateLimitResult:
    allowed: bool
    provider_id: int | str
    provider_model_id: int | str
    probe_type: str
    limit: int
    window_seconds: int
    retry_after_seconds: int
    current_count: int
    reason: str

    @property
    def error_code(self) -> str:
        return ProbeRateLimitService.ERROR_CODE

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "provider_id": self.provider_id,
            "provider_model_id": self.provider_model_id,
            "probe_type": self.probe_type,
            "limit": self.limit,
            "window_seconds": self.window_seconds,
            "retry_after_seconds": self.retry_after_seconds,
            "current_count": self.current_count,
            "reason": self.reason,
            "error_code": self.error_code,
        }


class ProbeRateLimitService:
    ERROR_CODE = "probe_rate_limited"
    DEFAULT_LIMIT_PER_MINUTE = 4
    DEFAULT_WINDOW_SECONDS = 60
    _LUA = """
local aggregate_key = KEYS[1]
local type_key = KEYS[2]
local aggregate_limit = tonumber(ARGV[1])
local type_limit = tonumber(ARGV[2])
local window_seconds = tonumber(ARGV[3])
local aggregate_current = tonumber(redis.call('GET', aggregate_key) or '0')
local type_current = tonumber(redis.call('GET', type_key) or '0')
if aggregate_limit ~= nil and aggregate_limit > 0 and aggregate_current >= aggregate_limit then
  local ttl_ms = tonumber(redis.call('PTTL', aggregate_key) or '-1')
  return {0, aggregate_current, ttl_ms, 'aggregate'}
end
if type_limit ~= nil and type_limit > 0 and type_current >= type_limit then
  local ttl_ms = tonumber(redis.call('PTTL', type_key) or '-1')
  return {0, type_current, ttl_ms, 'type'}
end
aggregate_current = tonumber(redis.call('INCR', aggregate_key) or '0')
if aggregate_current == 1 then
  redis.call('EXPIRE', aggregate_key, window_seconds)
end
if aggregate_key ~= type_key then
  type_current = tonumber(redis.call('INCR', type_key) or '0')
  if type_current == 1 then
    redis.call('EXPIRE', type_key, window_seconds)
  end
else
  type_current = aggregate_current
end
local ttl_ms = tonumber(redis.call('PTTL', aggregate_key) or '-1')
return {1, aggregate_current, ttl_ms, 'ok'}
"""
    _local_windows: dict[str, deque[float]] = defaultdict(deque)

    @classmethod
    async def claim(
        cls,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        probe_type: str,
        limit_per_minute: int | None = None,
        window_seconds: int | None = None,
    ) -> ProbeRateLimitResult:
        provider_id = cls._identity(getattr(provider, "id", None), fallback=getattr(provider, "name", "unknown"))
        provider_model_id = cls._identity(
            getattr(provider_model, "id", None),
            fallback=getattr(provider_model, "model_name", "unknown"),
        )
        normalized_probe_type = cls._normalize_probe_type(probe_type)
        limit = int(limit_per_minute or cls.DEFAULT_LIMIT_PER_MINUTE)
        window = int(window_seconds or cls.DEFAULT_WINDOW_SECONDS)
        if limit <= 0:
            return cls._allowed_result(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                probe_type=normalized_probe_type,
                limit=limit,
                window_seconds=window,
                current_count=0,
            )
        aggregate_key = cls._key(
            provider_id=provider_id,
            provider_model_id=provider_model_id,
            probe_type="all",
        )
        type_key = cls._key(
            provider_id=provider_id,
            provider_model_id=provider_model_id,
            probe_type=normalized_probe_type,
        )
        try:
            client = RedisService.get_client()
            result = await client.eval(cls._LUA, 2, aggregate_key, type_key, cls.DEFAULT_LIMIT_PER_MINUTE, limit, window)
            allowed = bool(int(result[0])) if isinstance(result, list) and result else False
            current_count = int(result[1] if isinstance(result, list) and len(result) > 1 else 0)
            ttl_ms = int(result[2] if isinstance(result, list) and len(result) > 2 else window * 1000)
            retry_after = max(1, int((ttl_ms + 999) // 1000)) if ttl_ms >= 0 else window
            if allowed:
                return cls._allowed_result(
                    provider_id=provider_id,
                    provider_model_id=provider_model_id,
                    probe_type=normalized_probe_type,
                    limit=limit,
                    window_seconds=window,
                    current_count=current_count,
                )
            return cls._limited_result(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                probe_type=normalized_probe_type,
                limit=limit,
                window_seconds=window,
                current_count=current_count,
                retry_after_seconds=retry_after,
            )
        except Exception:
            if get_settings().is_production():
                return cls._limited_result(
                    provider_id=provider_id,
                    provider_model_id=provider_model_id,
                    probe_type=normalized_probe_type,
                    limit=limit,
                    window_seconds=window,
                    current_count=limit,
                    retry_after_seconds=window,
                    reason_prefix="探针频率限制服务不可用",
                )
            return cls._claim_local(
                key=aggregate_key,
                type_key=type_key,
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                probe_type=normalized_probe_type,
                limit=limit,
                aggregate_limit=cls.DEFAULT_LIMIT_PER_MINUTE,
                window_seconds=window,
            )

    @classmethod
    def rate_limited_probe_result(
        cls,
        limit_result: ProbeRateLimitResult,
        *,
        endpoint_path: str | None,
        endpoint_label: str,
        support_label: str | None = None,
    ) -> dict[str, Any]:
        return {
            "endpoint_path": endpoint_path,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": cls.ERROR_CODE,
            "support_label": support_label or "探针已限频",
            "latency_ms": 0,
            "status_code": 429,
            "message": limit_result.reason,
            "trace": [],
            "retryable": True,
            "probe_rate_limited": True,
            "error_code": cls.ERROR_CODE,
            "rate_limit": limit_result.to_dict(),
            "update_allowed": False,
        }

    @classmethod
    def is_rate_limited_result(cls, value: Any) -> bool:
        return isinstance(value, dict) and (
            value.get("probe_rate_limited") is True
            or value.get("error_code") == cls.ERROR_CODE
            or value.get("support_mode") == cls.ERROR_CODE
            or value.get("status") == "rate_limited"
        )

    @classmethod
    def contains_rate_limited_result(cls, results: list[dict[str, Any]] | None) -> bool:
        return any(cls.is_rate_limited_result(item) for item in results or [])

    @classmethod
    def _claim_local(
        cls,
        *,
        key: str,
        type_key: str,
        provider_id: int | str,
        provider_model_id: int | str,
        probe_type: str,
        limit: int,
        aggregate_limit: int,
        window_seconds: int,
    ) -> ProbeRateLimitResult:
        now = time.monotonic()
        aggregate_window = cls._local_windows[key]
        type_window = cls._local_windows[type_key]
        for window in (aggregate_window, type_window):
            while window and now - window[0] >= window_seconds:
                window.popleft()
        if len(aggregate_window) >= aggregate_limit:
            retry_after = max(1, int(window_seconds - (now - aggregate_window[0])))
            return cls._limited_result(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                probe_type=probe_type,
                limit=aggregate_limit,
                window_seconds=window_seconds,
                current_count=len(aggregate_window),
                retry_after_seconds=retry_after,
            )
        if len(type_window) >= limit:
            retry_after = max(1, int(window_seconds - (now - type_window[0])))
            return cls._limited_result(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                probe_type=probe_type,
                limit=limit,
                window_seconds=window_seconds,
                current_count=len(type_window),
                retry_after_seconds=retry_after,
            )
        aggregate_window.append(now)
        if type_key != key:
            type_window.append(now)
        return cls._allowed_result(
            provider_id=provider_id,
            provider_model_id=provider_model_id,
            probe_type=probe_type,
            limit=limit,
            window_seconds=window_seconds,
            current_count=len(aggregate_window),
        )

    @classmethod
    def _allowed_result(
        cls,
        *,
        provider_id: int | str,
        provider_model_id: int | str,
        probe_type: str,
        limit: int,
        window_seconds: int,
        current_count: int,
    ) -> ProbeRateLimitResult:
        return ProbeRateLimitResult(
            allowed=True,
            provider_id=provider_id,
            provider_model_id=provider_model_id,
            probe_type=probe_type,
            limit=limit,
            window_seconds=window_seconds,
            retry_after_seconds=0,
            current_count=current_count,
            reason="",
        )

    @classmethod
    def _limited_result(
        cls,
        *,
        provider_id: int | str,
        provider_model_id: int | str,
        probe_type: str,
        limit: int,
        window_seconds: int,
        current_count: int,
        retry_after_seconds: int,
        reason_prefix: str = "探针频率限制",
    ) -> ProbeRateLimitResult:
        reason = (
            f"{reason_prefix}：提供商 {provider_id} / 模型 {provider_model_id} / 探针 {probe_type} "
            f"在 {window_seconds} 秒内最多允许 {limit} 次，请 {retry_after_seconds} 秒后重试。"
        )
        return ProbeRateLimitResult(
            allowed=False,
            provider_id=provider_id,
            provider_model_id=provider_model_id,
            probe_type=probe_type,
            limit=limit,
            window_seconds=window_seconds,
            retry_after_seconds=retry_after_seconds,
            current_count=current_count,
            reason=reason,
        )

    @staticmethod
    def _identity(value: Any, *, fallback: Any) -> int | str:
        if value is None:
            return str(fallback or "unknown")
        try:
            return int(value)
        except Exception:
            return str(value or fallback or "unknown")

    @staticmethod
    def _normalize_probe_type(value: str) -> str:
        normalized = str(value or "probe").strip().lower()
        return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in normalized) or "probe"

    @classmethod
    def _key(cls, *, provider_id: int | str, provider_model_id: int | str, probe_type: str) -> str:
        return f"probe:rate:{provider_id}:{provider_model_id}:{probe_type}"
