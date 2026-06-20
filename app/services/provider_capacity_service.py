from __future__ import annotations

import time
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import AsyncIterator
from uuid import uuid4

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.config import get_settings
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.redis_service import RedisService


@dataclass(slots=True)
class ProviderCapacitySnapshot:
    """表示某个 provider 当前的并发、QPS 与 RPM 快照。"""

    active_requests: int = 0
    active_streams: int = 0
    current_qps: int = 0
    current_rpm: int = 0


class ProviderCapacityExceededError(Exception):
    """表示 provider 容量限制被触发。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderCapacityUnavailableError(Exception):
    """表示 provider 容量服务当前不可用。"""

    def __init__(self, message: str = "Redis provider capacity service is unavailable") -> None:
        super().__init__(message)
        self.code = "redis_unavailable"


class ProviderCapacityService:
    """基于 Redis 维护 provider 级别的并发和 QPS 租约。"""

    _redis_client: Redis | None = None

    _ACQUIRE_LUA = """
local lease_key = KEYS[1]
local ttl = tonumber(ARGV[1])
local is_stream = tonumber(ARGV[2])
local scope_count = tonumber(ARGV[3])
local index = 4
local scopes = {}
local first_active = 0
local first_stream = 0
local first_qps = 0
local first_rpm = 0

for i = 1, scope_count do
  local active_key = ARGV[index]
  local stream_key = ARGV[index + 1]
  local qps_key = ARGV[index + 2]
  local rpm_key = ARGV[index + 3]
  local active_limit = tonumber(ARGV[index + 4])
  local stream_limit = tonumber(ARGV[index + 5])
  local qps_limit = tonumber(ARGV[index + 6])
  local rpm_limit = tonumber(ARGV[index + 7])
  local scope_code = ARGV[index + 8]
  local active_current = tonumber(redis.call('GET', active_key) or '0')
  local stream_current = tonumber(redis.call('GET', stream_key) or '0')
  local qps_current = tonumber(redis.call('GET', qps_key) or '0')
  local rpm_current = tonumber(redis.call('GET', rpm_key) or '0')
  if i == 1 then
    first_active = active_current
    first_stream = stream_current
    first_qps = qps_current
    first_rpm = rpm_current
  end
  if active_limit ~= nil and active_limit > 0 and active_current >= active_limit then
    return {scope_code .. '_active_request_limit_exceeded', active_current, stream_current, qps_current, rpm_current}
  end
  if is_stream == 1 and stream_limit ~= nil and stream_limit > 0 and stream_current >= stream_limit then
    return {scope_code .. '_active_stream_limit_exceeded', active_current, stream_current, qps_current, rpm_current}
  end
  if qps_limit ~= nil and qps_limit > 0 and qps_current >= qps_limit then
    return {scope_code .. '_qps_limit_exceeded', active_current, stream_current, qps_current, rpm_current}
  end
  if rpm_limit ~= nil and rpm_limit > 0 and rpm_current >= rpm_limit then
    return {scope_code .. '_rpm_limit_exceeded', active_current, stream_current, qps_current, rpm_current}
  end
  table.insert(scopes, {active_key, stream_key, qps_key, rpm_key})
  index = index + 9
end

local lease_items = {}
for i, scope in ipairs(scopes) do
  local active_current = redis.call('INCR', scope[1])
  redis.call('EXPIRE', scope[1], ttl + 60)
  table.insert(lease_items, scope[1])
  local stream_current = tonumber(redis.call('GET', scope[2]) or '0')
  if is_stream == 1 then
    stream_current = redis.call('INCR', scope[2])
    redis.call('EXPIRE', scope[2], ttl + 60)
    table.insert(lease_items, scope[2])
  end
  local qps_current = redis.call('INCR', scope[3])
  redis.call('EXPIRE', scope[3], 3)
  local rpm_current = redis.call('INCR', scope[4])
  redis.call('EXPIRE', scope[4], 120)
  if i == 1 then
    first_active = active_current
    first_stream = stream_current
    first_qps = qps_current
    first_rpm = rpm_current
  end
end

redis.call('SET', lease_key, cjson.encode(lease_items), 'EX', ttl)
return {'ok', first_active, first_stream, first_qps, first_rpm}
"""

    _RELEASE_LUA = """
local lease_key = KEYS[1]
local payload = redis.call('GET', lease_key)
if not payload then
  return 0
end
redis.call('DEL', lease_key)
local keys = cjson.decode(payload)
for _, key in ipairs(keys) do
  local value = tonumber(redis.call('DECR', key) or '0')
  if value <= 0 then
    redis.call('DEL', key)
  end
end
return 1
"""

    @classmethod
    def snapshot(cls, provider_id: int) -> ProviderCapacitySnapshot:
        """读取单个 provider 的容量快照。"""
        try:
            redis_snapshot = cls._redis_snapshot(provider_id)
            if redis_snapshot is not None:
                return redis_snapshot
        except ProviderCapacityUnavailableError:
            if cls._allow_local_fallback():
                return ProviderCapacitySnapshot()
            raise
        raise ProviderCapacityUnavailableError()

    @classmethod
    def snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        """批量读取多个 provider 的容量快照。"""
        try:
            redis_snapshots = cls._redis_snapshots(provider_ids)
            if redis_snapshots is not None:
                return redis_snapshots
        except ProviderCapacityUnavailableError:
            if cls._allow_local_fallback():
                return {provider_id: ProviderCapacitySnapshot() for provider_id in provider_ids}
            raise
        raise ProviderCapacityUnavailableError()

    @classmethod
    async def async_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        """异步批量读取多个 provider 的容量快照。"""
        try:
            return await cls._async_redis_snapshots(provider_ids)
        except ProviderCapacityUnavailableError:
            if cls._allow_local_fallback():
                return {provider_id: ProviderCapacitySnapshot() for provider_id in provider_ids}
            raise

    @classmethod
    def can_accept(cls, provider: Provider, *, is_stream: bool) -> bool:
        """判断 provider 当前是否还能接收新请求。"""
        snapshot = cls.snapshot(provider.id)
        return cls._has_capacity(provider, snapshot=snapshot, is_stream=is_stream)

    @classmethod
    @asynccontextmanager
    async def async_lease(
        cls,
        provider: Provider,
        *,
        is_stream: bool,
        provider_model: ProviderModel | None = None,
    ) -> AsyncIterator[ProviderCapacitySnapshot]:
        """异步申请 provider 容量租约，并在退出时自动释放。"""
        lease_id = uuid4().hex
        lease_acquired = False
        try:
            snapshot = await cls._async_redis_acquire(
                provider,
                provider_model=provider_model,
                is_stream=is_stream,
                lease_id=lease_id,
            )
            lease_acquired = True
        except ProviderCapacityUnavailableError:
            if not cls._allow_local_fallback():
                raise
            snapshot = ProviderCapacitySnapshot()
        try:
            yield snapshot
        finally:
            if lease_acquired:
                await cls.async_release(lease_id=lease_id)

    @classmethod
    async def async_release(cls, *, lease_id: str | None) -> bool:
        """释放 provider 容量租约。"""
        if not lease_id:
            return False
        try:
            return await cls._async_redis_release(lease_id)
        except ProviderCapacityUnavailableError:
            if cls._allow_local_fallback():
                return False
            raise

    @classmethod
    def _ensure_capacity(cls, provider: Provider, *, snapshot: ProviderCapacitySnapshot, is_stream: bool) -> None:
        """根据快照校验 provider 是否触达并发或 QPS 上限。"""
        if cls._limit_reached(snapshot.active_requests, provider.max_active_requests):
            raise ProviderCapacityExceededError("Provider active request limit exceeded", code="provider_active_request_limit_exceeded")
        if is_stream and cls._limit_reached(snapshot.active_streams, provider.max_active_streams):
            raise ProviderCapacityExceededError("Provider active stream limit exceeded", code="provider_active_stream_limit_exceeded")
        if cls._limit_reached(snapshot.current_qps, provider.max_qps):
            raise ProviderCapacityExceededError("Provider QPS limit exceeded", code="provider_qps_limit_exceeded")
        if cls._limit_reached(snapshot.current_rpm, provider.max_rpm):
            raise ProviderCapacityExceededError("Provider RPM limit exceeded", code="provider_rpm_limit_exceeded")

    @classmethod
    def _has_capacity(cls, provider: Provider, *, snapshot: ProviderCapacitySnapshot, is_stream: bool) -> bool:
        """返回 provider 是否还有可用容量。"""
        try:
            cls._ensure_capacity(provider, snapshot=snapshot, is_stream=is_stream)
        except ProviderCapacityExceededError:
            return False
        return True

    @staticmethod
    def _limit_reached(current_value: int, limit: int | None) -> bool:
        """判断当前值是否达到限制值。"""
        return limit is not None and limit > 0 and current_value >= limit

    @classmethod
    def _redis(cls) -> Redis | None:
        """返回同步 Redis 客户端。"""
        if RedisService.should_skip_after_recent_error():
            raise ProviderCapacityUnavailableError(RedisService.last_error() or "Redis is temporarily unavailable")
        if not get_settings().redis_url.strip():
            raise ProviderCapacityUnavailableError("REDIS_URL is empty")
        if cls._redis_client is None:
            cls._redis_client = RedisService.create_sync_client()
        return cls._redis_client

    @classmethod
    def _redis_snapshot(cls, provider_id: int) -> ProviderCapacitySnapshot | None:
        """从 Redis 读取单个 provider 的计数值。"""
        client = cls._redis()
        try:
            current_second = int(time.time())
            current_minute = current_second // 60
            keys = [
                f"provider_capacity:provider:{provider_id}:active",
                f"provider_capacity:provider:{provider_id}:streams",
                f"rate:provider:qps:{provider_id}:{current_second}",
                f"rate:provider:rpm:{provider_id}:{current_minute}",
            ]
            values = client.mget(keys)
            RedisService.clear_last_error()
            return ProviderCapacitySnapshot(
                active_requests=int(values[0] or 0),
                active_streams=int(values[1] or 0),
                current_qps=int(values[2] or 0),
                current_rpm=int(values[3] or 0),
            )
        except Exception as exc:
            RedisService.mark_error(exc)
            raise ProviderCapacityUnavailableError(str(exc))

    @classmethod
    def _redis_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot] | None:
        """从 Redis 批量读取 provider 计数值。"""
        client = cls._redis()
        try:
            current_second = int(time.time())
            current_minute = current_second // 60
            keys: list[str] = []
            ordered_ids = sorted(provider_ids)
            for provider_id in ordered_ids:
                keys.extend(
                    [
                        f"provider_capacity:provider:{provider_id}:active",
                        f"provider_capacity:provider:{provider_id}:streams",
                        f"rate:provider:qps:{provider_id}:{current_second}",
                        f"rate:provider:rpm:{provider_id}:{current_minute}",
                    ]
                )
            values = client.mget(keys) if keys else []
            RedisService.clear_last_error()
            snapshots: dict[int, ProviderCapacitySnapshot] = {}
            for index, provider_id in enumerate(ordered_ids):
                offset = index * 4
                snapshots[provider_id] = ProviderCapacitySnapshot(
                    active_requests=int(values[offset] or 0),
                    active_streams=int(values[offset + 1] or 0),
                    current_qps=int(values[offset + 2] or 0),
                    current_rpm=int(values[offset + 3] or 0),
                )
            return snapshots
        except Exception as exc:
            RedisService.mark_error(exc)
            raise ProviderCapacityUnavailableError(str(exc))

    @classmethod
    async def _async_redis(cls) -> AsyncRedis:
        """返回异步 Redis 客户端。"""
        try:
            return RedisService.get_client()
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc

    @classmethod
    async def _async_redis_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        """异步批量读取 provider 计数值。"""
        client = await cls._async_redis()
        current_second = int(time.time())
        current_minute = current_second // 60
        keys: list[str] = []
        ordered_ids = sorted(provider_ids)
        for provider_id in ordered_ids:
            keys.extend(
                [
                f"provider_capacity:provider:{provider_id}:active",
                f"provider_capacity:provider:{provider_id}:streams",
                    f"rate:provider:qps:{provider_id}:{current_second}",
                    f"rate:provider:rpm:{provider_id}:{current_minute}",
                ]
            )
        try:
            values = await client.mget(keys) if keys else []
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc
        snapshots: dict[int, ProviderCapacitySnapshot] = {}
        for index, provider_id in enumerate(ordered_ids):
            offset = index * 4
            snapshots[provider_id] = ProviderCapacitySnapshot(
                active_requests=int(values[offset] or 0),
                active_streams=int(values[offset + 1] or 0),
                current_qps=int(values[offset + 2] or 0),
                current_rpm=int(values[offset + 3] or 0),
            )
        return snapshots

    @classmethod
    async def _async_redis_acquire(
        cls,
        provider: Provider,
        *,
        is_stream: bool,
        lease_id: str | None,
        provider_model: ProviderModel | None = None,
    ) -> ProviderCapacitySnapshot:
        if lease_id is None:
            raise ProviderCapacityUnavailableError("provider capacity lease id is empty")
        client = await cls._async_redis()
        current_second = int(time.time())
        current_minute = current_second // 60
        lease_key = f"provider_capacity:lease:{lease_id}"
        scopes = cls._capacity_scopes(
            provider,
            provider_model=provider_model,
            current_second=current_second,
            current_minute=current_minute,
        )
        args: list[str | int] = [
            max(60, get_settings().concurrency_lease_ttl_seconds),
            1 if is_stream else 0,
            len(scopes),
        ]
        for scope in scopes:
            args.extend(scope)
        try:
            result = await client.eval(
                cls._ACQUIRE_LUA,
                1,
                lease_key,
                *args,
            )
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc
        code = result[0] if isinstance(result, list) and result else result
        if code != "ok":
            messages = {
                "provider_active_request_limit_exceeded": "Provider active request limit exceeded",
                "provider_active_stream_limit_exceeded": "Provider active stream limit exceeded",
                "provider_qps_limit_exceeded": "Provider QPS limit exceeded",
                "provider_rpm_limit_exceeded": "Provider RPM limit exceeded",
                "credential_active_request_limit_exceeded": "Shared credential active request limit exceeded",
                "credential_active_stream_limit_exceeded": "Shared credential active stream limit exceeded",
                "credential_qps_limit_exceeded": "Shared credential QPS limit exceeded",
                "credential_rpm_limit_exceeded": "Shared credential RPM limit exceeded",
                "provider_model_active_request_limit_exceeded": "Provider model active request limit exceeded",
                "provider_model_active_stream_limit_exceeded": "Provider model active stream limit exceeded",
                "provider_model_qps_limit_exceeded": "Provider model QPS limit exceeded",
                "provider_model_rpm_limit_exceeded": "Provider model RPM limit exceeded",
            }
            raise ProviderCapacityExceededError(messages.get(str(code), "Provider capacity limit exceeded"), code=str(code))
        return ProviderCapacitySnapshot(
            active_requests=int(result[1] or 0),
            active_streams=int(result[2] or 0),
            current_qps=int(result[3] or 0),
            current_rpm=int(result[4] or 0),
        )

    @classmethod
    async def _async_redis_release(cls, lease_id: str) -> bool:
        client = await cls._async_redis()
        try:
            result = await client.eval(cls._RELEASE_LUA, 1, f"provider_capacity:lease:{lease_id}")
            return bool(int(result or 0))
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc

    @staticmethod
    def _limit_arg(value: int | float | None) -> int:
        return int(value or 0)

    @classmethod
    def _capacity_scopes(
        cls,
        provider: Provider,
        *,
        provider_model: ProviderModel | None,
        current_second: int,
        current_minute: int,
    ) -> list[list[str | int]]:
        scopes: list[list[str | int]] = [
            cls._scope_args(
                prefix=f"provider:{provider.id}",
                qps_key=f"rate:provider:qps:{provider.id}:{current_second}",
                rpm_key=f"rate:provider:rpm:{provider.id}:{current_minute}",
                active_limit=provider.max_active_requests,
                stream_limit=provider.max_active_streams,
                qps_limit=provider.max_qps,
                rpm_limit=provider.max_rpm,
                scope_code="provider",
            )
        ]
        credential_hash = cls._credential_scope_hash(provider)
        if credential_hash:
            scopes.append(
                cls._scope_args(
                    prefix=f"credential:{credential_hash}",
                    qps_key=f"rate:credential:qps:{credential_hash}:{current_second}",
                    rpm_key=f"rate:credential:rpm:{credential_hash}:{current_minute}",
                    active_limit=provider.max_active_requests,
                    stream_limit=provider.max_active_streams,
                    qps_limit=provider.max_qps,
                    rpm_limit=provider.max_rpm,
                    scope_code="credential",
                )
            )
        if provider_model is not None and provider_model.id is not None:
            scopes.append(
                cls._scope_args(
                    prefix=f"provider_model:{provider_model.id}",
                    qps_key=f"rate:provider_model:qps:{provider_model.id}:{current_second}",
                    rpm_key=f"rate:provider_model:rpm:{provider_model.id}:{current_minute}",
                    active_limit=provider_model.max_active_requests,
                    stream_limit=provider_model.max_active_streams,
                    qps_limit=provider_model.max_qps,
                    rpm_limit=provider_model.max_rpm,
                    scope_code="provider_model",
                )
            )
        return scopes

    @classmethod
    def _scope_args(
        cls,
        *,
        prefix: str,
        qps_key: str,
        rpm_key: str,
        active_limit: int | None,
        stream_limit: int | None,
        qps_limit: int | None,
        rpm_limit: int | None,
        scope_code: str,
    ) -> list[str | int]:
        return [
            f"provider_capacity:{prefix}:active",
            f"provider_capacity:{prefix}:streams",
            qps_key,
            rpm_key,
            cls._limit_arg(active_limit),
            cls._limit_arg(stream_limit),
            cls._limit_arg(qps_limit),
            cls._limit_arg(rpm_limit),
            scope_code,
        ]

    @staticmethod
    def _credential_scope_hash(provider: Provider) -> str | None:
        api_key = str(getattr(provider, "api_key", "") or "").strip()
        if not api_key:
            return None
        base_url = str(getattr(provider, "base_url", "") or "").strip().rstrip("/").lower()
        provider_type = str(getattr(provider, "provider_type", "") or "").strip().lower()
        raw = f"{provider_type}|{base_url}|{api_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _allow_local_fallback() -> bool:
        return False
