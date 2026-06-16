from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

from app.config import get_settings
from app.services.redis_service import RedisService
from app.utils.decimal_utils import money_to_scaled_int


class RateLimitExceededError(Exception):
    def __init__(self, message: str, *, code: str, key: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.key = key
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class RateLimitService:
    RETRY_FAILURE_LIMIT = 5
    RETRY_FAILURE_WINDOW_SECONDS = 15
    RETRY_FAILURE_COOLDOWN_SECONDS = 30

    _MULTI_WINDOW_LIMIT_LUA = """
local count = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local index = 3

for i = 1, count do
  local key = ARGV[index]
  local limit = tonumber(ARGV[index + 1])
  local code = ARGV[index + 2]
  local current = tonumber(redis.call('GET', key) or '0')
  if limit ~= nil and limit > 0 and current >= limit then
    return {code, key, current}
  end
  index = index + 3
end

index = 3
local max_current = 0
for i = 1, count do
  local key = ARGV[index]
  local current = tonumber(redis.call('INCR', key) or '0')
  redis.call('EXPIRE', key, ttl)
  if current > max_current then
    max_current = current
  end
  index = index + 3
end
return {'ok', '', max_current}
"""

    @staticmethod
    async def seed_realtime_quota_counters(
        *,
        api_key_id: int,
        api_key_total_tokens_used: int | None = None,
        api_key_total_cost_used: Decimal | float | int | str | None = None,
        account_id: int | None = None,
        account_total_tokens_used: int | None = None,
        account_total_cost_used: Decimal | float | int | str | None = None,
    ) -> None:
        try:
            client = RedisService.get_client()
            async with client.pipeline(transaction=True) as pipe:
                if api_key_total_tokens_used is not None:
                    pipe.set(
                        f"quota:api_key:{api_key_id}:tokens:total",
                        max(0, int(api_key_total_tokens_used)),
                        nx=True,
                    )
                if api_key_total_cost_used is not None:
                    pipe.set(
                        f"quota:api_key:{api_key_id}:cost:total",
                        max(0, money_to_scaled_int(api_key_total_cost_used)),
                        nx=True,
                    )
                if account_id is not None and account_total_tokens_used is not None:
                    pipe.set(
                        f"quota:account:{account_id}:tokens:total",
                        max(0, int(account_total_tokens_used)),
                        nx=True,
                    )
                if account_id is not None and account_total_cost_used is not None:
                    pipe.set(
                        f"quota:account:{account_id}:cost:total",
                        max(0, money_to_scaled_int(account_total_cost_used)),
                        nx=True,
                    )
                await pipe.execute()
        except Exception:
            if RateLimitService._allow_local_fallback():
                return
            raise

    @staticmethod
    def reset_realtime_quota_counters(
        *,
        api_key_id: int,
        api_key_total_tokens_used: int | None = None,
        api_key_day_tokens_used: int | None = None,
        api_key_total_cost_used: Decimal | float | int | str | None = None,
        api_key_day_cost_used: Decimal | float | int | str | None = None,
        api_key_day_requests: int | None = None,
        account_id: int | None = None,
        account_total_tokens_used: int | None = None,
        account_day_tokens_used: int | None = None,
        account_month_tokens_used: int | None = None,
        account_total_cost_used: Decimal | float | int | str | None = None,
        account_day_cost_used: Decimal | float | int | str | None = None,
        account_month_cost_used: Decimal | float | int | str | None = None,
        account_total_requests: int | None = None,
        account_day_requests: int | None = None,
        account_month_requests: int | None = None,
    ) -> None:
        try:
            client = RedisService.get_sync_client()
            day_key = now_beijing().strftime("%Y%m%d")
            month_key = now_beijing().strftime("%Y%m")
            pipe = client.pipeline(transaction=True)
            if api_key_total_tokens_used is not None:
                pipe.set(f"quota:api_key:{api_key_id}:tokens:total", max(0, int(api_key_total_tokens_used)))
            if api_key_day_tokens_used is not None:
                pipe.set(f"quota:api_key:{api_key_id}:tokens:{day_key}", max(0, int(api_key_day_tokens_used)), ex=60 * 60 * 26)
            if api_key_total_cost_used is not None:
                pipe.set(f"quota:api_key:{api_key_id}:cost:total", max(0, money_to_scaled_int(api_key_total_cost_used)))
            if api_key_day_cost_used is not None:
                pipe.set(f"quota:api_key:{api_key_id}:cost:{day_key}", max(0, money_to_scaled_int(api_key_day_cost_used)), ex=60 * 60 * 26)
            if api_key_day_requests is not None:
                pipe.set(f"quota:api_key:{api_key_id}:requests:{day_key}", max(0, int(api_key_day_requests)), ex=60 * 60 * 26)
            if account_id is not None:
                if account_total_tokens_used is not None:
                    pipe.set(f"quota:account:{account_id}:tokens:total", max(0, int(account_total_tokens_used)))
                if account_day_tokens_used is not None:
                    pipe.set(f"quota:account:{account_id}:tokens:{day_key}", max(0, int(account_day_tokens_used)), ex=60 * 60 * 26)
                if account_month_tokens_used is not None:
                    pipe.set(f"quota:account:{account_id}:tokens:{month_key}", max(0, int(account_month_tokens_used)), ex=60 * 60 * 24 * 33)
                if account_total_cost_used is not None:
                    pipe.set(f"quota:account:{account_id}:cost:total", max(0, money_to_scaled_int(account_total_cost_used)))
                if account_day_cost_used is not None:
                    pipe.set(f"quota:account:{account_id}:cost:{day_key}", max(0, money_to_scaled_int(account_day_cost_used)), ex=60 * 60 * 26)
                if account_month_cost_used is not None:
                    pipe.set(f"quota:account:{account_id}:cost:{month_key}", max(0, money_to_scaled_int(account_month_cost_used)), ex=60 * 60 * 24 * 33)
                if account_total_requests is not None:
                    pipe.set(f"quota:account:{account_id}:requests:total", max(0, int(account_total_requests)))
                if account_day_requests is not None:
                    pipe.set(f"quota:account:{account_id}:requests:{day_key}", max(0, int(account_day_requests)), ex=60 * 60 * 26)
                if account_month_requests is not None:
                    pipe.set(f"quota:account:{account_id}:requests:{month_key}", max(0, int(account_month_requests)), ex=60 * 60 * 24 * 33)
            pipe.execute()
        except Exception:
            return

    @staticmethod
    async def check_api_key_limits(
        *,
        api_key_id: int,
        qps_limit: int | None = None,
        rpm_limit: int | None = None,
    ) -> None:
        await RateLimitService.check_request_limits(
            api_key_id=api_key_id,
            api_key_qps_limit=qps_limit,
            api_key_rpm_limit=rpm_limit,
        )

    @staticmethod
    async def check_request_limits(
        *,
        api_key_id: int | None = None,
        api_key_qps_limit: int | None = None,
        api_key_rpm_limit: int | None = None,
        account_id: int | None = None,
        account_qps_limit: int | None = None,
        account_rpm_limit: int | None = None,
        global_qps_limit: int | None = None,
        global_rpm_limit: int | None = None,
    ) -> None:
        try:
            current_second = int(time.time())
            minute_key = now_beijing().strftime("%Y%m%d%H%M")
            qps_entries: list[tuple[str, int, str, str]] = []
            rpm_entries: list[tuple[str, int, str, str]] = []
            if global_qps_limit and global_qps_limit > 0:
                qps_entries.append(
                    (
                        f"rate:global:qps:{current_second}",
                        int(global_qps_limit),
                        "rate_limit_exceeded",
                        "Global QPS limit exceeded",
                    )
                )
            if api_key_id is not None and api_key_qps_limit and api_key_qps_limit > 0:
                qps_entries.append(
                    (
                        f"rate:qps:{api_key_id}:{current_second}",
                        int(api_key_qps_limit),
                        "rate_limit_exceeded",
                        "Api key QPS limit exceeded",
                    )
                )
            if account_id is not None and account_qps_limit and account_qps_limit > 0:
                qps_entries.append(
                    (
                        f"rate:account:qps:{account_id}:{current_second}",
                        int(account_qps_limit),
                        "rate_limit_exceeded",
                        "Account QPS limit exceeded",
                    )
                )
            if global_rpm_limit and global_rpm_limit > 0:
                rpm_entries.append(
                    (
                        f"rate:global:rpm:{minute_key}",
                        int(global_rpm_limit),
                        "rate_limit_exceeded",
                        "Global RPM limit exceeded",
                    )
                )
            if api_key_id is not None and api_key_rpm_limit and api_key_rpm_limit > 0:
                rpm_entries.append(
                    (
                        f"rate:rpm:{api_key_id}:{minute_key}",
                        int(api_key_rpm_limit),
                        "rate_limit_exceeded",
                        "Api key RPM limit exceeded",
                    )
                )
            if account_id is not None and account_rpm_limit and account_rpm_limit > 0:
                rpm_entries.append(
                    (
                        f"rate:account:rpm:{account_id}:{minute_key}",
                        int(account_rpm_limit),
                        "rate_limit_exceeded",
                        "Account RPM limit exceeded",
                    )
                )
            await RateLimitService._check_window_limits(entries=qps_entries, ttl_seconds=3)
            await RateLimitService._check_window_limits(entries=rpm_entries, ttl_seconds=120)
        except RateLimitExceededError:
            raise

    @staticmethod
    async def check_abnormal_retry_cooldown(
        *,
        api_key_id: int | None = None,
        account_id: int | None = None,
        source_ip: str | None = None,
    ) -> None:
        if not any((api_key_id, account_id, source_ip)):
            return
        try:
            client = RedisService.get_client()
            keys = RateLimitService._retry_cooldown_keys(
                api_key_id=api_key_id,
                account_id=account_id,
                source_ip=source_ip,
            )
            if not keys:
                return
            values = await client.mget(keys)
            for key, value in zip(keys, values, strict=True):
                if value:
                    ttl = int(await client.ttl(key) or 0)
                    raise RateLimitExceededError(
                        "Too many failed retry attempts",
                        code="retry_backoff_required",
                        key=key,
                        retry_after_seconds=max(1, ttl),
                    )
        except RateLimitExceededError:
            raise
        except Exception:
            if RateLimitService._allow_local_fallback():
                return
            raise

    @staticmethod
    def record_abnormal_retry_failure(
        *,
        api_key_id: int | None = None,
        account_id: int | None = None,
        source_ip: str | None = None,
    ) -> None:
        if not any((api_key_id, account_id, source_ip)):
            return
        try:
            client = RedisService.get_sync_client()
            for key, cooldown_key in RateLimitService._retry_failure_key_pairs(
                api_key_id=api_key_id,
                account_id=account_id,
                source_ip=source_ip,
            ):
                current = int(client.incr(key) or 0)
                client.expire(key, RateLimitService.RETRY_FAILURE_WINDOW_SECONDS)
                if current >= RateLimitService.RETRY_FAILURE_LIMIT:
                    client.setex(cooldown_key, RateLimitService.RETRY_FAILURE_COOLDOWN_SECONDS, "1")
        except Exception:
            return

    @staticmethod
    def _retry_failure_key_pairs(
        *,
        api_key_id: int | None,
        account_id: int | None,
        source_ip: str | None,
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if api_key_id is not None:
            pairs.append((f"retry_failure:api_key:{api_key_id}", f"retry_cooldown:api_key:{api_key_id}"))
        if account_id is not None:
            pairs.append((f"retry_failure:account:{account_id}", f"retry_cooldown:account:{account_id}"))
        if source_ip:
            safe_ip = str(source_ip).replace(":", "_").replace("/", "_")
            pairs.append((f"retry_failure:ip:{safe_ip}", f"retry_cooldown:ip:{safe_ip}"))
        return pairs

    @staticmethod
    def _retry_cooldown_keys(
        *,
        api_key_id: int | None,
        account_id: int | None,
        source_ip: str | None,
    ) -> list[str]:
        return [
            cooldown_key
            for _failure_key, cooldown_key in RateLimitService._retry_failure_key_pairs(
                api_key_id=api_key_id,
                account_id=account_id,
                source_ip=source_ip,
            )
        ]

    @staticmethod
    async def _check_window_limits(*, entries: list[tuple[str, int, str, str]], ttl_seconds: int) -> None:
        if not entries:
            return
        client = RedisService.get_client()
        args: list[str | int] = [len(entries), ttl_seconds]
        messages: dict[str, str] = {}
        for key, limit, code, message in entries:
            args.extend([key, int(limit), code])
            messages[key] = message
        result = await client.eval(RateLimitService._MULTI_WINDOW_LIMIT_LUA, 0, *args)
        code = result[0] if isinstance(result, list) and result else result
        if code != "ok":
            key = str(result[1] if isinstance(result, list) and len(result) > 1 else "")
            raise RateLimitExceededError(
                messages.get(key, "Rate limit exceeded"),
                code=str(code or "rate_limit_exceeded"),
                key=key,
            )

    @staticmethod
    async def record_api_key_usage(
        *,
        api_key_id: int,
        total_tokens: int | None = None,
        total_cost: Decimal | float | int | str | None = None,
    ) -> None:
        try:
            client = RedisService.get_client()
            day_key = now_beijing().strftime("%Y%m%d")
            minute_key = now_beijing().strftime("%Y%m%d%H%M")
            async with client.pipeline(transaction=True) as pipe:
                if total_tokens is not None and total_tokens > 0:
                    pipe.incrby(f"quota:api_key:{api_key_id}:tokens:{day_key}", int(total_tokens))
                    pipe.expire(f"quota:api_key:{api_key_id}:tokens:{day_key}", 60 * 60 * 26)
                    pipe.incrby(f"quota:api_key:{api_key_id}:tpm:{minute_key}", int(total_tokens))
                    pipe.expire(f"quota:api_key:{api_key_id}:tpm:{minute_key}", 180)
                scaled_total_cost = money_to_scaled_int(total_cost) if total_cost is not None else 0
                if scaled_total_cost > 0:
                    pipe.incrby(f"quota:api_key:{api_key_id}:cost:{day_key}", scaled_total_cost)
                    pipe.expire(f"quota:api_key:{api_key_id}:cost:{day_key}", 60 * 60 * 26)
                await pipe.execute()
        except Exception:
            if RateLimitService._allow_local_fallback():
                return
            raise

    @staticmethod
    async def check_provider_qps(*, provider_id: int, qps_limit: int | None) -> None:
        if not qps_limit or qps_limit <= 0:
            return
        try:
            current_second = int(time.time())
            key = f"rate:provider:qps:{provider_id}:{current_second}"
            await RateLimitService._increment_and_check(
                key=key,
                ttl_seconds=3,
                limit=qps_limit,
                code="provider_qps_limit_exceeded",
                message="Provider QPS limit exceeded",
            )
        except RateLimitExceededError:
            raise
        except Exception:
            if RateLimitService._allow_local_fallback():
                return
            raise

    @staticmethod
    async def _increment_and_check(
        *,
        key: str,
        ttl_seconds: int | None,
        limit: int,
        code: str,
        message: str,
    ) -> int:
        client = RedisService.get_client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            if ttl_seconds is not None:
                pipe.expire(key, ttl_seconds)
            results = await pipe.execute()
            current = results[0]
        if int(current or 0) > int(limit):
            raise RateLimitExceededError(message, code=code, key=key)
        return int(current or 0)

    @staticmethod
    async def _get_and_check(*, key: str, limit: int, code: str, message: str) -> int:
        client = RedisService.get_client()
        current = int(await client.get(key) or 0)
        if current >= int(limit):
            raise RateLimitExceededError(message, code=code, key=key)
        return current

    @staticmethod
    async def _get_and_check_cost(*, key: str, limit: Decimal | float | int | str, code: str, message: str) -> int:
        client = RedisService.get_client()
        current = int(await client.get(key) or 0)
        normalized_limit = money_to_scaled_int(limit)
        if current >= normalized_limit:
            raise RateLimitExceededError(message, code=code, key=key)
        return current

    @staticmethod
    def _allow_local_fallback() -> bool:
        return not get_settings().is_production()

from app.utils.timezone import now_beijing
