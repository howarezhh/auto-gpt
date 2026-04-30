from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import AsyncIterator
from uuid import uuid4

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.config import get_settings
from app.models.provider import Provider
from app.services.redis_service import RedisService


@dataclass(slots=True)
class ProviderCapacitySnapshot:
    active_requests: int = 0
    active_streams: int = 0
    current_qps: int = 0


class ProviderCapacityExceededError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderCapacityUnavailableError(Exception):
    def __init__(self, message: str = "Redis provider capacity service is unavailable") -> None:
        super().__init__(message)
        self.code = "redis_unavailable"


class ProviderCapacityService:
    _redis_client: Redis | None = None

    _ACQUIRE_LUA = """
local lease_key = KEYS[1]
local active_key = KEYS[2]
local stream_key = KEYS[3]
local qps_key = KEYS[4]
local ttl = tonumber(ARGV[1])
local is_stream = tonumber(ARGV[2])
local active_limit = tonumber(ARGV[3])
local stream_limit = tonumber(ARGV[4])
local qps_limit = tonumber(ARGV[5])
local active_current = tonumber(redis.call('GET', active_key) or '0')
local stream_current = tonumber(redis.call('GET', stream_key) or '0')
local qps_current = tonumber(redis.call('GET', qps_key) or '0')

if active_limit ~= nil and active_limit > 0 and active_current >= active_limit then
  return {'provider_active_request_limit_exceeded', active_current, stream_current, qps_current}
end
if is_stream == 1 and stream_limit ~= nil and stream_limit > 0 and stream_current >= stream_limit then
  return {'provider_active_stream_limit_exceeded', active_current, stream_current, qps_current}
end
if qps_limit ~= nil and qps_limit > 0 and qps_current >= qps_limit then
  return {'provider_qps_limit_exceeded', active_current, stream_current, qps_current}
end

active_current = redis.call('INCR', active_key)
redis.call('EXPIRE', active_key, ttl + 60)
local lease_items = {active_key}
if is_stream == 1 then
  stream_current = redis.call('INCR', stream_key)
  redis.call('EXPIRE', stream_key, ttl + 60)
  table.insert(lease_items, stream_key)
end
qps_current = redis.call('INCR', qps_key)
redis.call('EXPIRE', qps_key, 3)
redis.call('SET', lease_key, cjson.encode(lease_items), 'EX', ttl)
return {'ok', active_current, stream_current, qps_current}
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
        redis_snapshot = cls._redis_snapshot(provider_id)
        if redis_snapshot is not None:
            return redis_snapshot
        raise ProviderCapacityUnavailableError()

    @classmethod
    def snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        redis_snapshots = cls._redis_snapshots(provider_ids)
        if redis_snapshots is not None:
            return redis_snapshots
        raise ProviderCapacityUnavailableError()

    @classmethod
    async def async_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        return await cls._async_redis_snapshots(provider_ids)

    @classmethod
    def can_accept(cls, provider: Provider, *, is_stream: bool) -> bool:
        snapshot = cls.snapshot(provider.id)
        return cls._has_capacity(provider, snapshot=snapshot, is_stream=is_stream)

    @classmethod
    @asynccontextmanager
    async def async_lease(cls, provider: Provider, *, is_stream: bool) -> AsyncIterator[ProviderCapacitySnapshot]:
        lease_id = uuid4().hex
        snapshot = await cls._async_redis_acquire(provider, is_stream=is_stream, lease_id=lease_id)
        try:
            yield snapshot
        finally:
            await cls.async_release(lease_id=lease_id)

    @classmethod
    async def async_release(cls, *, lease_id: str | None) -> bool:
        if not lease_id:
            return False
        return await cls._async_redis_release(lease_id)

    @classmethod
    def _ensure_capacity(cls, provider: Provider, *, snapshot: ProviderCapacitySnapshot, is_stream: bool) -> None:
        if cls._limit_reached(snapshot.active_requests, provider.max_active_requests):
            raise ProviderCapacityExceededError("Provider active request limit exceeded", code="provider_active_request_limit_exceeded")
        if is_stream and cls._limit_reached(snapshot.active_streams, provider.max_active_streams):
            raise ProviderCapacityExceededError("Provider active stream limit exceeded", code="provider_active_stream_limit_exceeded")
        if cls._limit_reached(snapshot.current_qps, provider.max_qps):
            raise ProviderCapacityExceededError("Provider QPS limit exceeded", code="provider_qps_limit_exceeded")

    @classmethod
    def _has_capacity(cls, provider: Provider, *, snapshot: ProviderCapacitySnapshot, is_stream: bool) -> bool:
        try:
            cls._ensure_capacity(provider, snapshot=snapshot, is_stream=is_stream)
        except ProviderCapacityExceededError:
            return False
        return True

    @staticmethod
    def _limit_reached(current_value: int, limit: int | None) -> bool:
        return limit is not None and limit > 0 and current_value >= limit

    @classmethod
    def _redis(cls) -> Redis | None:
        settings = get_settings()
        if not settings.redis_url.strip():
            raise ProviderCapacityUnavailableError("REDIS_URL is empty")
        if cls._redis_client is None:
            cls._redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
        return cls._redis_client

    @classmethod
    def _redis_snapshot(cls, provider_id: int) -> ProviderCapacitySnapshot | None:
        client = cls._redis()
        try:
            current_second = int(time.time())
            keys = [
                f"concurrency:provider:{provider_id}:active",
                f"concurrency:provider:{provider_id}:streams",
                f"rate:provider:qps:{provider_id}:{current_second}",
            ]
            values = client.mget(keys)
            return ProviderCapacitySnapshot(
                active_requests=int(values[0] or 0),
                active_streams=int(values[1] or 0),
                current_qps=int(values[2] or 0),
            )
        except Exception:
            raise ProviderCapacityUnavailableError()

    @classmethod
    def _redis_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot] | None:
        client = cls._redis()
        try:
            current_second = int(time.time())
            keys: list[str] = []
            ordered_ids = sorted(provider_ids)
            for provider_id in ordered_ids:
                keys.extend(
                    [
                        f"concurrency:provider:{provider_id}:active",
                        f"concurrency:provider:{provider_id}:streams",
                        f"rate:provider:qps:{provider_id}:{current_second}",
                    ]
                )
            values = client.mget(keys) if keys else []
            snapshots: dict[int, ProviderCapacitySnapshot] = {}
            for index, provider_id in enumerate(ordered_ids):
                offset = index * 3
                snapshots[provider_id] = ProviderCapacitySnapshot(
                    active_requests=int(values[offset] or 0),
                    active_streams=int(values[offset + 1] or 0),
                    current_qps=int(values[offset + 2] or 0),
                )
            return snapshots
        except Exception:
            raise ProviderCapacityUnavailableError()

    @classmethod
    async def _async_redis(cls) -> AsyncRedis:
        try:
            return RedisService.get_client()
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc

    @classmethod
    async def _async_redis_snapshots(cls, provider_ids: set[int]) -> dict[int, ProviderCapacitySnapshot]:
        client = await cls._async_redis()
        current_second = int(time.time())
        keys: list[str] = []
        ordered_ids = sorted(provider_ids)
        for provider_id in ordered_ids:
            keys.extend(
                [
                    f"concurrency:provider:{provider_id}:active",
                    f"concurrency:provider:{provider_id}:streams",
                    f"rate:provider:qps:{provider_id}:{current_second}",
                ]
            )
        try:
            values = await client.mget(keys) if keys else []
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc
        snapshots: dict[int, ProviderCapacitySnapshot] = {}
        for index, provider_id in enumerate(ordered_ids):
            offset = index * 3
            snapshots[provider_id] = ProviderCapacitySnapshot(
                active_requests=int(values[offset] or 0),
                active_streams=int(values[offset + 1] or 0),
                current_qps=int(values[offset + 2] or 0),
            )
        return snapshots

    @classmethod
    async def _async_redis_acquire(cls, provider: Provider, *, is_stream: bool, lease_id: str | None) -> ProviderCapacitySnapshot:
        if lease_id is None:
            raise ProviderCapacityUnavailableError("provider capacity lease id is empty")
        client = await cls._async_redis()
        current_second = int(time.time())
        lease_key = f"provider_capacity:lease:{lease_id}"
        try:
            result = await client.eval(
                cls._ACQUIRE_LUA,
                4,
                lease_key,
                f"concurrency:provider:{provider.id}:active",
                f"concurrency:provider:{provider.id}:streams",
                f"rate:provider:qps:{provider.id}:{current_second}",
                max(60, get_settings().concurrency_lease_ttl_seconds),
                1 if is_stream else 0,
                cls._limit_arg(provider.max_active_requests),
                cls._limit_arg(provider.max_active_streams),
                cls._limit_arg(provider.max_qps),
            )
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc
        code = result[0] if isinstance(result, list) and result else result
        if code != "ok":
            messages = {
                "provider_active_request_limit_exceeded": "Provider active request limit exceeded",
                "provider_active_stream_limit_exceeded": "Provider active stream limit exceeded",
                "provider_qps_limit_exceeded": "Provider QPS limit exceeded",
            }
            raise ProviderCapacityExceededError(messages.get(str(code), "Provider capacity limit exceeded"), code=str(code))
        return ProviderCapacitySnapshot(
            active_requests=int(result[1] or 0),
            active_streams=int(result[2] or 0),
            current_qps=int(result[3] or 0),
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
