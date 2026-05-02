import inspect
import logging
import asyncio
from datetime import datetime
from collections.abc import Callable
from functools import wraps
from uuid import uuid4

from app.database import SessionLocal
from app.scheduler import scheduler
from app.services.data_retention_service import DataRetentionService
from app.services.health_service import HealthService
from app.services.redis_service import RedisService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService


logger = logging.getLogger(__name__)

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def distributed_job_lock(job_name: str, *, ttl_seconds: int) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            token = uuid4().hex
            lock_key = f"scheduler:lock:{job_name}"
            state_key = f"scheduler:job:{job_name}:state"
            try:
                client = RedisService.get_client()
                acquired = await client.set(lock_key, token, nx=True, ex=ttl_seconds)
            except Exception as exc:
                logger.warning("Skip scheduler job %s because Redis lock is unavailable: %s", job_name, exc)
                return None
            if not acquired:
                try:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "skipped_locked",
                            "updated_at": datetime.utcnow().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(ttl_seconds, 300))
                except Exception:
                    pass
                return None
            try:
                await client.hset(
                    state_key,
                    mapping={
                        "status": "running",
                        "token": token,
                        "started_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                )
                await client.expire(state_key, max(ttl_seconds, 300))
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                await client.hset(
                    state_key,
                    mapping={
                        "status": "success",
                        "finished_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                )
                await client.expire(state_key, max(ttl_seconds, 300))
                return result
            except asyncio.CancelledError:
                try:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "cancelled",
                            "finished_at": datetime.utcnow().isoformat(),
                            "updated_at": datetime.utcnow().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(ttl_seconds, 300))
                except Exception:
                    pass
                raise
            except Exception as exc:
                try:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "failed",
                            "error": str(exc)[:1000],
                            "finished_at": datetime.utcnow().isoformat(),
                            "updated_at": datetime.utcnow().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(ttl_seconds, 300))
                except Exception:
                    pass
                raise
            finally:
                try:
                    await client.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if "closed" in str(exc).lower():
                        return None
                    logger.warning("Failed to release scheduler job lock %s: %s", job_name, exc)

        return async_wrapper

    return decorator


@distributed_job_lock("provider_health_check", ttl_seconds=300)
async def scheduled_health_check() -> None:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).auto_health_check:
            await HealthService.check_all(db)
    finally:
        db.close()


@distributed_job_lock("token_usage_backfill", ttl_seconds=120)
def scheduled_token_usage_backfill() -> None:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).enable_token_logging:
            TokenUsageService.backfill_missing_usage()
    finally:
        db.close()


@distributed_job_lock("data_retention_cleanup", ttl_seconds=60 * 60 * 8)
def scheduled_data_retention_cleanup() -> None:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        DataRetentionService.cleanup(
            db,
            request_log_retention_days=setting.request_log_retention_days,
            admin_audit_log_retention_days=setting.admin_audit_log_retention_days,
        )
    finally:
        db.close()


def configure_scheduler() -> None:
    db = SessionLocal()
    try:
        interval = max(10, SettingService.get_or_create(db).health_check_interval_sec)
    finally:
        db.close()
    scheduler.add_job(
        scheduled_health_check,
        "interval",
        seconds=interval,
        id="provider_health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_token_usage_backfill,
        "interval",
        seconds=15,
        id="token_usage_backfill",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_data_retention_cleanup,
        "interval",
        hours=6,
        id="data_retention_cleanup",
        replace_existing=True,
    )
