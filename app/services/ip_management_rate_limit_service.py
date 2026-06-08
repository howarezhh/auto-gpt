from __future__ import annotations

import hashlib
import time
from datetime import datetime

from app.services.rate_limit_service import RateLimitExceededError
from app.services.redis_service import RedisService


class IpManagementRateLimitService:
    @staticmethod
    async def check_ip_limits(
        *,
        resolved_ip: str | None,
        scope: str,
        qps_limit: int | None,
        rpm_limit: int | None,
    ) -> None:
        if not resolved_ip:
            return
        ip_hash = hashlib.sha256(resolved_ip.encode("utf-8")).hexdigest()[:24]
        if qps_limit and qps_limit > 0:
            current_second = int(time.time())
            await IpManagementRateLimitService._increment_and_check(
                key=f"ipmgmt:rate:qps:{scope}:{ip_hash}:{current_second}",
                ttl_seconds=3,
                limit=qps_limit,
                code="source_ip_rate_limited",
                message="来源 IP QPS 超过限制",
            )
        if rpm_limit and rpm_limit > 0:
            minute_key = datetime.utcnow().strftime("%Y%m%d%H%M")
            await IpManagementRateLimitService._increment_and_check(
                key=f"ipmgmt:rate:rpm:{scope}:{ip_hash}:{minute_key}",
                ttl_seconds=120,
                limit=rpm_limit,
                code="source_ip_rate_limited",
                message="来源 IP RPM 超过限制",
            )

    @staticmethod
    async def _increment_and_check(*, key: str, ttl_seconds: int, limit: int, code: str, message: str) -> int:
        client = RedisService.get_client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds)
            results = await pipe.execute()
        current = int(results[0] or 0)
        if current > int(limit):
            raise RateLimitExceededError(message, code=code, key=key)
        return current
