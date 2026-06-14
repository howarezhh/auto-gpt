from __future__ import annotations

from decimal import Decimal

from app.services.redis_service import RedisService
from app.utils.decimal_utils import money_to_scaled_int


class BillingReservationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "insufficient_balance_for_estimated_request",
        active_reserved_amount: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.active_reserved_amount = active_reserved_amount


class BillingReservationUnavailableError(Exception):
    def __init__(self, message: str = "Redis billing reservation service is unavailable") -> None:
        super().__init__(message)
        self.code = "redis_unavailable"


class BillingReservationService:
    _ACQUIRE_LUA = """
local active_key = KEYS[1]
local lease_key = KEYS[2]
local ttl = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local available = tonumber(ARGV[3])

if amount == nil or amount <= 0 then
  return {'ok', tonumber(redis.call('GET', active_key) or '0')}
end

local existing_amount = tonumber(redis.call('GET', lease_key) or '0')
if existing_amount > 0 then
  if amount <= existing_amount then
    redis.call('EXPIRE', lease_key, ttl)
    redis.call('EXPIRE', active_key, ttl + 60)
    return {'ok', tonumber(redis.call('GET', active_key) or '0')}
  end
  local delta = amount - existing_amount
  local current = tonumber(redis.call('GET', active_key) or '0')
  if available ~= nil and available >= 0 and current + delta > available then
    return {'insufficient_balance_for_estimated_request', current}
  end
  current = redis.call('INCRBY', active_key, delta)
  redis.call('EXPIRE', active_key, ttl + 60)
  redis.call('SET', lease_key, amount, 'EX', ttl)
  return {'ok', current}
end

local current = tonumber(redis.call('GET', active_key) or '0')
if available ~= nil and available >= 0 and current + amount > available then
  return {'insufficient_balance_for_estimated_request', current}
end

current = redis.call('INCRBY', active_key, amount)
redis.call('EXPIRE', active_key, ttl + 60)
redis.call('SET', lease_key, amount, 'EX', ttl)
return {'ok', current}
"""

    _RELEASE_LUA = """
local active_key = KEYS[1]
local lease_key = KEYS[2]
local amount = tonumber(redis.call('GET', lease_key) or '0')
if amount <= 0 then
  return 0
end
redis.call('DEL', lease_key)
local current = tonumber(redis.call('DECRBY', active_key, amount) or '0')
if current <= 0 then
  redis.call('DEL', active_key)
end
return amount
"""

    @classmethod
    async def acquire(
        cls,
        *,
        user_account_id: int,
        reservation_id: str,
        estimated_amount: Decimal,
        available_balance: Decimal,
        ttl_seconds: int,
    ) -> None:
        amount = money_to_scaled_int(estimated_amount)
        if amount <= 0:
            return
        available = money_to_scaled_int(available_balance)
        try:
            result = await RedisService.get_client().eval(
                cls._ACQUIRE_LUA,
                2,
                cls._active_key(user_account_id),
                cls._lease_key(user_account_id, reservation_id),
                max(60, int(ttl_seconds or 60)),
                amount,
                max(0, available),
            )
        except Exception as exc:
            raise BillingReservationUnavailableError(str(exc)) from exc
        code = result[0] if isinstance(result, list) and result else result
        if code != "ok":
            active_reserved_amount = int(result[1] or 0) if isinstance(result, list) and len(result) > 1 else 0
            raise BillingReservationError(
                "账户可用余额不足以覆盖当前在途请求和本次请求的预估费用",
                code=str(code or "insufficient_balance_for_estimated_request"),
                active_reserved_amount=active_reserved_amount,
            )

    @classmethod
    def release(cls, *, user_account_id: int | None, reservation_id: str | None) -> None:
        if user_account_id is None or not reservation_id:
            return
        try:
            RedisService.get_sync_client().eval(
                cls._RELEASE_LUA,
                2,
                cls._active_key(int(user_account_id)),
                cls._lease_key(int(user_account_id), str(reservation_id)),
            )
        except Exception:
            return

    @staticmethod
    def _active_key(user_account_id: int) -> str:
        return f"billing:reservation:account:{int(user_account_id)}:active"

    @staticmethod
    def _lease_key(user_account_id: int, reservation_id: str) -> str:
        return f"billing:reservation:account:{int(user_account_id)}:lease:{reservation_id}"
