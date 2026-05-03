from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.services.redis_service import RedisService


@dataclass(slots=True)
class ConcurrencyLimits:
    global_max_active_requests: int | None = None
    global_max_active_streams: int | None = None
    api_key_max_active_requests: int | None = None
    api_key_max_active_streams: int | None = None
    account_max_active_requests: int | None = None
    account_max_active_streams: int | None = None
    provider_max_active_requests: int | None = None
    provider_max_active_streams: int | None = None


@dataclass(slots=True)
class ConcurrencyLease:
    request_id: str
    keys: list[str]


class ConcurrencyLimitExceededError(Exception):
    def __init__(self, message: str, *, code: str, scope: str) -> None:
        super().__init__(message)
        self.code = code
        self.scope = scope
        self.message = message


class ConcurrencyService:
    _ACQUIRE_LUA = """
local lease_key = KEYS[1]
local ttl = tonumber(ARGV[1])
local is_stream = tonumber(ARGV[2])
local count = tonumber(ARGV[3])
local key_index = 4
local items = {}

if redis.call('EXISTS', lease_key) == 1 then
  return {'duplicate_lease', lease_key}
end

for i = 1, count do
  local active_key = ARGV[key_index]
  local stream_key = ARGV[key_index + 1]
  local request_limit = tonumber(ARGV[key_index + 2])
  local stream_limit = tonumber(ARGV[key_index + 3])
  local code = ARGV[key_index + 4]
  local current = tonumber(redis.call('GET', active_key) or '0')
  if request_limit ~= nil and request_limit > 0 and current >= request_limit then
    return {code, active_key}
  end
  if is_stream == 1 and stream_key ~= '' then
    local current_stream = tonumber(redis.call('GET', stream_key) or '0')
    if stream_limit ~= nil and stream_limit > 0 and current_stream >= stream_limit then
      return {'stream_concurrency_exceeded', stream_key}
    end
  end
  table.insert(items, {active_key, stream_key})
  key_index = key_index + 5
end

local lease_items = {}
for _, item in ipairs(items) do
  redis.call('INCR', item[1])
  redis.call('EXPIRE', item[1], ttl + 60)
  table.insert(lease_items, item[1])
  if is_stream == 1 and item[2] ~= '' then
    redis.call('INCR', item[2])
    redis.call('EXPIRE', item[2], ttl + 60)
    table.insert(lease_items, item[2])
  end
end
redis.call('SET', lease_key, cjson.encode(lease_items), 'EX', ttl)
return {'ok', lease_key}
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
    async def acquire(
        cls,
        *,
        request_id: str,
        ttl_seconds: int,
        is_stream: bool,
        limits: ConcurrencyLimits,
        api_key_id: int | None = None,
        account_id: int | None = None,
        provider_id: int | None = None,
    ) -> ConcurrencyLease:
        scoped_items = cls._build_scoped_items(
            limits=limits,
            api_key_id=api_key_id,
            account_id=account_id,
            provider_id=provider_id,
        )
        if not scoped_items:
            return ConcurrencyLease(request_id=request_id, keys=[])
        lease_key = f"concurrency:lease:{request_id}"
        args: list[str | int] = [max(60, ttl_seconds), 1 if is_stream else 0, len(scoped_items)]
        for item in scoped_items:
            args.extend(item)
        try:
            result = await RedisService.get_client().eval(cls._ACQUIRE_LUA, 1, lease_key, *args)
        except Exception:
            if cls._allow_local_fallback():
                return ConcurrencyLease(request_id=request_id, keys=[])
            raise
        code = result[0] if isinstance(result, list) and result else result
        detail = result[1] if isinstance(result, list) and len(result) > 1 else ""
        if code != "ok":
            raise cls._build_error(str(code), str(detail))
        return ConcurrencyLease(request_id=request_id, keys=[item[0] for item in scoped_items])

    @classmethod
    async def release(cls, lease: ConcurrencyLease | None) -> bool:
        if lease is None or not lease.keys:
            return False
        lease_key = f"concurrency:lease:{lease.request_id}"
        try:
            result = await RedisService.get_client().eval(cls._RELEASE_LUA, 1, lease_key)
        except Exception:
            if cls._allow_local_fallback():
                return False
            raise
        return bool(int(result or 0))

    @classmethod
    async def active_snapshot(cls) -> dict[str, int]:
        try:
            client = RedisService.get_client()
            keys = [
                "concurrency:global:active",
                "concurrency:global:streams",
            ]
            values = await client.mget(keys)
            return {key: int(value or 0) for key, value in zip(keys, values, strict=True)}
        except Exception:
            if cls._allow_local_fallback():
                return {
                    "concurrency:global:active": 0,
                    "concurrency:global:streams": 0,
                }
            raise

    @staticmethod
    def _build_scoped_items(
        *,
        limits: ConcurrencyLimits,
        api_key_id: int | None,
        account_id: int | None,
        provider_id: int | None,
    ) -> list[list[str | int]]:
        items: list[list[str | int]] = [
            [
                "concurrency:global:active",
                "concurrency:global:streams",
                ConcurrencyService._limit_value(limits.global_max_active_requests),
                ConcurrencyService._limit_value(limits.global_max_active_streams),
                "concurrency_limit_exceeded",
            ]
        ]
        if api_key_id is not None:
            items.append(
                [
                    f"concurrency:api_key:{api_key_id}:active",
                    f"concurrency:api_key:{api_key_id}:streams",
                    ConcurrencyService._limit_value(limits.api_key_max_active_requests),
                    ConcurrencyService._limit_value(limits.api_key_max_active_streams),
                    "concurrency_limit_exceeded",
                ]
            )
        if account_id is not None:
            items.append(
                [
                    f"concurrency:account:{account_id}:active",
                    f"concurrency:account:{account_id}:streams",
                    ConcurrencyService._limit_value(limits.account_max_active_requests),
                    ConcurrencyService._limit_value(limits.account_max_active_streams),
                    "concurrency_limit_exceeded",
                ]
            )
        if provider_id is not None:
            items.append(
                [
                    f"concurrency:provider:{provider_id}:active",
                    f"concurrency:provider:{provider_id}:streams",
                    ConcurrencyService._limit_value(limits.provider_max_active_requests),
                    ConcurrencyService._limit_value(limits.provider_max_active_streams),
                    "provider_concurrency_limit_exceeded",
                ]
            )
        return items

    @staticmethod
    def _limit_value(value: int | None) -> int:
        return int(value or 0)

    @staticmethod
    def _build_error(code: str, detail: str) -> ConcurrencyLimitExceededError:
        if code == "stream_concurrency_exceeded":
            return ConcurrencyLimitExceededError(
                "Stream concurrency limit exceeded",
                code="stream_concurrency_exceeded",
                scope=detail,
            )
        if code == "provider_concurrency_limit_exceeded":
            return ConcurrencyLimitExceededError(
                "Provider concurrency limit exceeded",
                code="concurrency_limit_exceeded",
                scope=detail,
            )
        return ConcurrencyLimitExceededError(
            "Concurrency limit exceeded",
            code="concurrency_limit_exceeded",
            scope=detail,
        )

    @staticmethod
    def _allow_local_fallback() -> bool:
        return not get_settings().is_production()
