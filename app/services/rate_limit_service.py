from __future__ import annotations

import time
from datetime import datetime

from app.services.redis_service import RedisService


class RateLimitExceededError(Exception):
    def __init__(self, message: str, *, code: str, key: str) -> None:
        super().__init__(message)
        self.code = code
        self.key = key
        self.message = message


class RateLimitService:
    @staticmethod
    async def seed_realtime_quota_counters(
        *,
        api_key_id: int,
        api_key_total_tokens_used: int | None = None,
        api_key_total_cost_used: float | None = None,
        account_id: int | None = None,
        account_total_tokens_used: int | None = None,
        account_total_cost_used: float | None = None,
    ) -> None:
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
                    max(0.0, float(api_key_total_cost_used)),
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
                    max(0.0, float(account_total_cost_used)),
                    nx=True,
                )
            await pipe.execute()

    @staticmethod
    async def check_api_key_limits(
        *,
        api_key_id: int,
        qps_limit: int | None = None,
        rpm_limit: int | None = None,
        daily_request_limit: int | None = None,
        total_token_limit: int | None = None,
        daily_token_limit: int | None = None,
        total_cost_limit: float | None = None,
        daily_cost_limit: float | None = None,
        tpm_limit: int | None = None,
        account_id: int | None = None,
        account_request_limit_total: int | None = None,
        account_request_limit_daily: int | None = None,
        account_request_limit_monthly: int | None = None,
        account_token_limit_total: int | None = None,
        account_token_limit_daily: int | None = None,
        account_token_limit_monthly: int | None = None,
        account_cost_limit_total: float | None = None,
        account_cost_limit_daily: float | None = None,
        account_cost_limit_monthly: float | None = None,
    ) -> None:
        day_key = datetime.utcnow().strftime("%Y%m%d")
        month_key = datetime.utcnow().strftime("%Y%m")
        if qps_limit and qps_limit > 0:
            current_second = int(time.time())
            key = f"rate:qps:{api_key_id}:{current_second}"
            await RateLimitService._increment_and_check(
                key=key,
                ttl_seconds=3,
                limit=qps_limit,
                code="rate_limit_exceeded",
                message="Api key QPS limit exceeded",
            )
        if rpm_limit and rpm_limit > 0:
            minute_key = datetime.utcnow().strftime("%Y%m%d%H%M")
            key = f"rate:rpm:{api_key_id}:{minute_key}"
            await RateLimitService._increment_and_check(
                key=key,
                ttl_seconds=120,
                limit=rpm_limit,
                code="rate_limit_exceeded",
                message="Api key RPM limit exceeded",
            )
        if daily_request_limit and daily_request_limit > 0:
            key = f"quota:api_key:{api_key_id}:requests:{day_key}"
            await RateLimitService._increment_and_check(
                key=key,
                ttl_seconds=60 * 60 * 26,
                limit=daily_request_limit,
                code="daily_request_quota_exhausted",
                message="Api key daily request quota exhausted",
            )
        if total_token_limit and total_token_limit > 0:
            await RateLimitService._get_and_check(
                key=f"quota:api_key:{api_key_id}:tokens:total",
                limit=total_token_limit,
                code="insufficient_quota",
                message="Api key token quota exhausted",
            )
        if daily_token_limit and daily_token_limit > 0:
            await RateLimitService._get_and_check(
                key=f"quota:api_key:{api_key_id}:tokens:{day_key}",
                limit=daily_token_limit,
                code="daily_token_quota_exhausted",
                message="Api key daily token quota exhausted",
            )
        if total_cost_limit and total_cost_limit > 0:
            await RateLimitService._get_and_check_float(
                key=f"quota:api_key:{api_key_id}:cost:total",
                limit=total_cost_limit,
                code="insufficient_quota",
                message="Api key billing quota exhausted",
            )
        if daily_cost_limit and daily_cost_limit > 0:
            await RateLimitService._get_and_check_float(
                key=f"quota:api_key:{api_key_id}:cost:{day_key}",
                limit=daily_cost_limit,
                code="daily_cost_quota_exhausted",
                message="Api key daily cost quota exhausted",
            )
        if tpm_limit and tpm_limit > 0:
            minute_key = datetime.utcnow().strftime("%Y%m%d%H%M")
            await RateLimitService._get_and_check(
                key=f"quota:api_key:{api_key_id}:tpm:{minute_key}",
                limit=tpm_limit,
                code="tpm_limit_exceeded",
                message="Api key TPM limit exceeded",
            )
        if account_id is not None and account_request_limit_total and account_request_limit_total > 0:
            await RateLimitService._increment_and_check(
                key=f"quota:account:{account_id}:requests:total",
                ttl_seconds=None,
                limit=account_request_limit_total,
                code="account_request_quota_exhausted",
                message="Owner account total request quota exhausted",
            )
        if account_id is not None and account_request_limit_daily and account_request_limit_daily > 0:
            await RateLimitService._increment_and_check(
                key=f"quota:account:{account_id}:requests:{day_key}",
                ttl_seconds=60 * 60 * 26,
                limit=account_request_limit_daily,
                code="account_daily_request_quota_exhausted",
                message="Owner account daily request quota exhausted",
            )
        if account_id is not None and account_request_limit_monthly and account_request_limit_monthly > 0:
            await RateLimitService._increment_and_check(
                key=f"quota:account:{account_id}:requests:{month_key}",
                ttl_seconds=60 * 60 * 24 * 33,
                limit=account_request_limit_monthly,
                code="account_monthly_request_quota_exhausted",
                message="Owner account monthly request quota exhausted",
            )
        if account_id is not None and account_token_limit_total and account_token_limit_total > 0:
            await RateLimitService._get_and_check(
                key=f"quota:account:{account_id}:tokens:total",
                limit=account_token_limit_total,
                code="account_token_quota_exhausted",
                message="Owner account total token quota exhausted",
            )
        if account_id is not None and account_token_limit_daily and account_token_limit_daily > 0:
            await RateLimitService._get_and_check(
                key=f"quota:account:{account_id}:tokens:{day_key}",
                limit=account_token_limit_daily,
                code="account_daily_token_quota_exhausted",
                message="Owner account daily token quota exhausted",
            )
        if account_id is not None and account_token_limit_monthly and account_token_limit_monthly > 0:
            await RateLimitService._get_and_check(
                key=f"quota:account:{account_id}:tokens:{month_key}",
                limit=account_token_limit_monthly,
                code="account_monthly_token_quota_exhausted",
                message="Owner account monthly token quota exhausted",
            )
        if account_id is not None and account_cost_limit_total and account_cost_limit_total > 0:
            await RateLimitService._get_and_check_float(
                key=f"quota:account:{account_id}:cost:total",
                limit=account_cost_limit_total,
                code="account_cost_quota_exhausted",
                message="Owner account total cost quota exhausted",
            )
        if account_id is not None and account_cost_limit_daily and account_cost_limit_daily > 0:
            await RateLimitService._get_and_check_float(
                key=f"quota:account:{account_id}:cost:{day_key}",
                limit=account_cost_limit_daily,
                code="account_daily_cost_quota_exhausted",
                message="Owner account daily cost quota exhausted",
            )
        if account_id is not None and account_cost_limit_monthly and account_cost_limit_monthly > 0:
            await RateLimitService._get_and_check_float(
                key=f"quota:account:{account_id}:cost:{month_key}",
                limit=account_cost_limit_monthly,
                code="account_monthly_cost_quota_exhausted",
                message="Owner account monthly cost quota exhausted",
            )

    @staticmethod
    async def record_api_key_usage(
        *,
        api_key_id: int,
        total_tokens: int | None = None,
        total_cost: float | None = None,
    ) -> None:
        client = RedisService.get_client()
        day_key = datetime.utcnow().strftime("%Y%m%d")
        minute_key = datetime.utcnow().strftime("%Y%m%d%H%M")
        async with client.pipeline(transaction=True) as pipe:
            if total_tokens is not None and total_tokens > 0:
                pipe.incrby(f"quota:api_key:{api_key_id}:tokens:{day_key}", int(total_tokens))
                pipe.expire(f"quota:api_key:{api_key_id}:tokens:{day_key}", 60 * 60 * 26)
                pipe.incrby(f"quota:api_key:{api_key_id}:tpm:{minute_key}", int(total_tokens))
                pipe.expire(f"quota:api_key:{api_key_id}:tpm:{minute_key}", 180)
            if total_cost is not None and total_cost > 0:
                pipe.incrbyfloat(f"quota:api_key:{api_key_id}:cost:{day_key}", float(total_cost))
                pipe.expire(f"quota:api_key:{api_key_id}:cost:{day_key}", 60 * 60 * 26)
            await pipe.execute()

    @staticmethod
    async def check_provider_qps(*, provider_id: int, qps_limit: int | None) -> None:
        if not qps_limit or qps_limit <= 0:
            return
        current_second = int(time.time())
        key = f"rate:provider:qps:{provider_id}:{current_second}"
        await RateLimitService._increment_and_check(
            key=key,
            ttl_seconds=3,
            limit=qps_limit,
            code="provider_qps_limit_exceeded",
            message="Provider QPS limit exceeded",
        )

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
    async def _get_and_check_float(*, key: str, limit: float, code: str, message: str) -> float:
        client = RedisService.get_client()
        current = float(await client.get(key) or 0)
        if current >= float(limit):
            raise RateLimitExceededError(message, code=code, key=key)
        return current
