from __future__ import annotations

import hashlib
import time
from datetime import datetime

from app.services.rate_limit_service import RateLimitExceededError
from app.services.redis_service import RedisService
from app.utils.timezone import now_beijing


class IpManagementRateLimitService:
    _MULTI_LIMIT_LUA = """
local count = tonumber(ARGV[1])
local index = 2

for i = 1, count do
  local key = ARGV[index]
  local limit = tonumber(ARGV[index + 1])
  local current = tonumber(redis.call('GET', key) or '0')
  if limit ~= nil and limit > 0 and current >= limit then
    return {ARGV[index + 2], key}
  end
  index = index + 4
end

index = 2
for i = 1, count do
  local key = ARGV[index]
  local ttl = tonumber(ARGV[index + 3])
  redis.call('INCR', key)
  redis.call('EXPIRE', key, ttl)
  index = index + 4
end
return {'ok', ''}
"""

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
        entries: list[tuple[str, int, str, str, int]] = []
        if qps_limit and qps_limit > 0:
            current_second = int(time.time())
            entries.append(
                (
                    f"ipmgmt:rate:qps:{scope}:{ip_hash}:{current_second}",
                    int(qps_limit),
                    "source_ip_rate_limited",
                    "来源 IP QPS 超过限制",
                    3,
                )
            )
        if rpm_limit and rpm_limit > 0:
            minute_key = now_beijing().strftime("%Y%m%d%H%M")
            entries.append(
                (
                    f"ipmgmt:rate:rpm:{scope}:{ip_hash}:{minute_key}",
                    int(rpm_limit),
                    "source_ip_rate_limited",
                    "来源 IP RPM 超过限制",
                    120,
                )
            )
        await IpManagementRateLimitService._check_entries(entries)

    @staticmethod
    async def _check_entries(entries: list[tuple[str, int, str, str, int]]) -> None:
        if not entries:
            return
        client = RedisService.get_client()
        args: list[str | int] = [len(entries)]
        messages: dict[str, str] = {}
        for key, limit, code, message, ttl in entries:
            args.extend([key, int(limit), code, int(ttl)])
            messages[key] = message
        result = await client.eval(IpManagementRateLimitService._MULTI_LIMIT_LUA, 0, *args)
        code = result[0] if isinstance(result, list) and result else result
        if code != "ok":
            key = str(result[1] if isinstance(result, list) and len(result) > 1 else "")
            raise RateLimitExceededError(messages.get(key, "来源 IP 超过限制"), code=str(code), key=key)
