import inspect
import logging
import asyncio
from datetime import datetime
from typing import Any
from collections.abc import Callable
from functools import wraps
from uuid import uuid4

from app.database import SessionLocal
from app.logging.sanitizers import dumps_sanitized
from app.models.logging_events import BackgroundJobEvent
from app.scheduler import scheduler
from app.services.data_retention_service import DataRetentionService
from app.services.health_service import HealthService
from app.logging.adapters.background_job_adapter import BackgroundJobLogRecorder
from app.services.redis_service import RedisService
from app.services.responses_chat_adapter_service import ResponsesChatAdapterService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService


logger = logging.getLogger(__name__)
PROVIDER_L0_HEALTH_CHECK_INTERVAL_SEC = 120
MODEL_L2_CAPABILITY_CHECK_MIN_INTERVAL_SEC = 60 * 30

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _duration_ms(started_at: datetime, finished_at: datetime | None = None) -> int:
    finished = finished_at or datetime.utcnow()
    return int((finished - started_at).total_seconds() * 1000)


def _extract_job_metrics(result: Any) -> tuple[int | None, int | None, int | None, Any]:
    if isinstance(result, list):
        processed_count = len(result)
        success_count = sum(1 for item in result if isinstance(item, dict) and bool(item.get("success")))
        failed_count = processed_count - success_count
        summary = {
            "result_type": "list",
            "processed_count": processed_count,
            "success_count": success_count,
            "failed_count": failed_count,
        }
        return processed_count, success_count, failed_count, summary
    if isinstance(result, int):
        count = max(0, result)
        return count, count, 0, {"processed_count": count}
    if isinstance(result, dict):
        explicit_processed = _first_int_value(
            result,
            ("processed_count", "processed", "total_count", "total", "count", "deleted_count"),
        )
        deleted_total = sum(
            int(value)
            for key, value in result.items()
            if str(key).endswith("_deleted") and isinstance(value, int)
        )
        processed_count = explicit_processed if explicit_processed is not None else (deleted_total or None)
        failed_count = _first_int_value(result, ("failed_count", "failed", "error_count", "errors"))
        success_count = _first_int_value(result, ("success_count", "success", "completed_count", "completed"))
        if success_count is None and processed_count is not None and failed_count is not None:
            success_count = max(0, processed_count - failed_count)
        if failed_count is None and processed_count is not None and success_count is not None:
            failed_count = max(0, processed_count - success_count)
        return processed_count, success_count, failed_count, result
    if result is None:
        return None, None, None, None
    return None, None, None, {"result": str(result)}


def _first_int_value(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
    return None


def _safe_record_job_event(
    *,
    job_run_id: str,
    job_name: str,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    lock_key: str | None = None,
    lock_status: str | None = None,
    result_summary: Any = None,
    error: str | None = None,
    processed_count: int | None = None,
    success_count: int | None = None,
    failed_count: int | None = None,
) -> int | None:
    db = SessionLocal()
    try:
        item = BackgroundJobLogRecorder.record_job_event(
            db,
            job_run_id=job_run_id,
            job_name=job_name,
            lock_key=lock_key,
            lock_status=lock_status,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=_duration_ms(started_at, finished_at),
            processed_count=processed_count,
            success_count=success_count,
            failed_count=failed_count,
            result_summary=result_summary,
            error=error,
        )
        return int(item.id) if item is not None and getattr(item, "id", None) is not None else None
    except Exception as exc:
        logger.warning("Failed to record scheduler job %s status %s: %s", job_name, status, exc)
        return None
    finally:
        db.close()


def _safe_finish_job_event(
    job_event_id: int | None,
    *,
    job_run_id: str,
    job_name: str,
    lock_key: str | None,
    lock_status: str | None,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    result_summary: Any = None,
    error: str | None = None,
    processed_count: int | None = None,
    success_count: int | None = None,
    failed_count: int | None = None,
) -> None:
    if job_event_id is None:
        _safe_record_job_event(
            job_run_id=job_run_id,
            job_name=job_name,
            lock_key=lock_key,
            lock_status=lock_status,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            result_summary=result_summary,
            error=error,
            processed_count=processed_count,
            success_count=success_count,
            failed_count=failed_count,
        )
        return
    db = SessionLocal()
    try:
        item = db.get(BackgroundJobEvent, job_event_id)
        if item is None:
            db.close()
            _safe_record_job_event(
                job_run_id=job_run_id,
                job_name=job_name,
                lock_key=lock_key,
                lock_status=lock_status,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                result_summary=result_summary,
                error=error,
                processed_count=processed_count,
                success_count=success_count,
                failed_count=failed_count,
            )
            return
        item.lock_status = lock_status
        item.status = status
        item.finished_at = finished_at
        item.duration_ms = _duration_ms(started_at, finished_at)
        item.processed_count = processed_count
        item.success_count = success_count
        item.failed_count = failed_count
        item.result_summary_json = dumps_sanitized(result_summary)
        item.error = error
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Failed to finish scheduler job %s status %s: %s", job_name, status, exc)
    finally:
        db.close()


def _content_integrity_lock_ttl_seconds() -> int:
    setting = SettingService.get_cached()
    interval_seconds = max(300, int(getattr(setting, "content_guard_probe_interval_sec", 3600) or 3600))
    return max(600, interval_seconds * 2)


def _resolve_lock_ttl_seconds(ttl_seconds: int | Callable[[], int]) -> int:
    if callable(ttl_seconds):
        ttl_seconds = ttl_seconds()
    return max(60, int(ttl_seconds or 60))


def distributed_job_lock(job_name: str, *, ttl_seconds: int | Callable[[], int]) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            token = uuid4().hex
            lock_key = f"scheduler:lock:{job_name}"
            state_key = f"scheduler:job:{job_name}:state"
            job_run_id = BackgroundJobLogRecorder.new_run_id()
            started_at = datetime.utcnow()
            client = None
            lock_acquired = False
            lock_status = "acquired"
            job_event_id: int | None = None
            lock_ttl_seconds = _resolve_lock_ttl_seconds(ttl_seconds)
            try:
                client = RedisService.get_client()
                acquired = await client.set(lock_key, token, nx=True, ex=lock_ttl_seconds)
            except Exception as exc:
                logger.warning("Skip scheduler job %s because Redis distributed lock is unavailable: %s", job_name, exc)
                _safe_record_job_event(
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status="unavailable_skipped",
                    status="skipped_lock_unavailable",
                    started_at=started_at,
                    finished_at=datetime.utcnow(),
                    error=str(exc)[:1000],
                )
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
                    await client.expire(state_key, max(lock_ttl_seconds, 300))
                except Exception:
                    pass
                _safe_record_job_event(
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status="skipped_locked",
                    status="skipped_locked",
                    started_at=started_at,
                    finished_at=datetime.utcnow(),
                )
                return None
            try:
                lock_acquired = lock_status == "acquired"
                if client is not None and lock_acquired:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "running",
                            "token": token,
                            "started_at": datetime.utcnow().isoformat(),
                            "updated_at": datetime.utcnow().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(lock_ttl_seconds, 300))
                job_event_id = _safe_record_job_event(
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="running",
                    started_at=started_at,
                )
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                finished_at = datetime.utcnow()
                if client is not None and lock_acquired:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "success",
                            "finished_at": finished_at.isoformat(),
                            "updated_at": finished_at.isoformat(),
                        },
                    )
                    await client.expire(state_key, max(lock_ttl_seconds, 300))
                processed_count, success_count, failed_count, result_summary = _extract_job_metrics(result)
                _safe_finish_job_event(
                    job_event_id,
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="success",
                    started_at=started_at,
                    finished_at=finished_at,
                    result_summary=result_summary,
                    processed_count=processed_count,
                    success_count=success_count,
                    failed_count=failed_count,
                )
                return result
            except asyncio.CancelledError:
                finished_at = datetime.utcnow()
                try:
                    if client is not None and lock_acquired:
                        await client.hset(
                            state_key,
                            mapping={
                                "status": "cancelled",
                                "finished_at": finished_at.isoformat(),
                                "updated_at": finished_at.isoformat(),
                            },
                        )
                            await client.expire(state_key, max(lock_ttl_seconds, 300))
                except Exception:
                    pass
                _safe_finish_job_event(
                    job_event_id,
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=finished_at,
                )
                raise
            except Exception as exc:
                finished_at = datetime.utcnow()
                try:
                    if client is not None and lock_acquired:
                        await client.hset(
                            state_key,
                            mapping={
                                "status": "failed",
                                "error": str(exc)[:1000],
                                "finished_at": finished_at.isoformat(),
                                "updated_at": finished_at.isoformat(),
                            },
                        )
                            await client.expire(state_key, max(lock_ttl_seconds, 300))
                except Exception:
                    pass
                _safe_finish_job_event(
                    job_event_id,
                    job_run_id=job_run_id,
                    job_name=job_name,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    error=str(exc)[:1000],
                )
                raise
            finally:
                if client is not None and lock_acquired:
                    try:
                        await client.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Failed to release scheduler job lock %s: %s", job_name, exc)

        return async_wrapper

    return decorator


@distributed_job_lock("provider_health_check", ttl_seconds=300)
async def scheduled_health_check() -> dict[str, int]:
    """兼容旧调度入口：不再执行全量模型探针，避免自动任务放大上游压力。"""
    provider_results = await scheduled_provider_l0_health_check()
    model_results = await scheduled_model_l1_text_health_check()
    total_processed = _result_processed_count(provider_results) + _result_processed_count(model_results)
    total_failed = _result_failed_count(provider_results) + _result_failed_count(model_results)
    return {
        "processed_count": total_processed,
        "success_count": max(0, total_processed - total_failed),
        "failed_count": total_failed,
    }


def _result_processed_count(result: Any) -> int:
    processed_count, _, _, _ = _extract_job_metrics(result)
    return int(processed_count or 0)


def _result_failed_count(result: Any) -> int:
    _, _, failed_count, _ = _extract_job_metrics(result)
    return int(failed_count or 0)


@distributed_job_lock("provider_l0_health_check", ttl_seconds=120)
async def scheduled_provider_l0_health_check() -> list[dict]:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).auto_health_check:
            return await HealthService.check_provider_connectivity_all(db)
        return []
    finally:
        db.close()


@distributed_job_lock("model_l1_text_health_check", ttl_seconds=300)
async def scheduled_model_l1_text_health_check() -> list[dict]:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).auto_health_check:
            return await HealthService.check_scheduled_text_models(db)
        return []
    finally:
        db.close()


@distributed_job_lock("model_l2_capability_health_check", ttl_seconds=MODEL_L2_CAPABILITY_CHECK_MIN_INTERVAL_SEC)
async def scheduled_model_l2_capability_health_check() -> list[dict]:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).auto_health_check:
            return await HealthService.check_scheduled_capability_models(db)
        return []
    finally:
        db.close()


@distributed_job_lock("model_l3_content_integrity_health_check", ttl_seconds=_content_integrity_lock_ttl_seconds)
async def scheduled_model_l3_content_integrity_health_check() -> list[dict]:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        if setting.content_guard_enabled and setting.content_guard_precheck_auto_enabled:
            return await HealthService.check_scheduled_content_integrity_models(db)
        return []
    finally:
        db.close()


@distributed_job_lock("token_usage_backfill", ttl_seconds=120)
def scheduled_token_usage_backfill() -> dict[str, int]:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).enable_token_logging:
            processed_count = TokenUsageService.backfill_missing_usage()
            return {
                "processed_count": processed_count,
                "success_count": 0,
                "failed_count": 0,
                "enqueued_count": processed_count,
            }
        return {"processed_count": 0, "success_count": 0, "failed_count": 0, "enqueued_count": 0}
    finally:
        db.close()


@distributed_job_lock("data_retention_cleanup", ttl_seconds=60 * 60 * 8)
def scheduled_data_retention_cleanup() -> dict[str, int]:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        return DataRetentionService.cleanup(
            db,
            request_log_retention_days=setting.request_log_retention_days,
            admin_audit_log_retention_days=setting.admin_audit_log_retention_days,
            request_child_log_retention_days=setting.request_child_log_retention_days,
            exception_log_retention_days=setting.exception_log_retention_days,
            health_log_retention_days=setting.health_log_retention_days,
            billing_log_retention_days=setting.billing_log_retention_days,
            background_job_log_retention_days=setting.background_job_log_retention_days,
            user_operation_log_retention_days=setting.user_operation_log_retention_days,
            asset_log_retention_days=setting.asset_log_retention_days,
        )
    finally:
        db.close()


@distributed_job_lock("responses_chat_adapter_session_cleanup", ttl_seconds=60 * 60)
def scheduled_responses_chat_adapter_session_cleanup() -> dict[str, int]:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        if str(setting.responses_chat_adapter_storage_type or "").strip().lower() not in {"database", "postgresql", "postgres"}:
            return {"processed_count": 0, "success_count": 0, "failed_count": 0}
        processed_count = ResponsesChatAdapterService.cleanup_expired_database_sessions(db)
        return {
            "processed_count": processed_count,
            "success_count": processed_count,
            "failed_count": 0,
        }
    finally:
        db.close()


def configure_scheduler() -> None:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        interval = max(300, setting.health_check_interval_sec)
        adapter_cleanup_interval = max(300, setting.responses_chat_adapter_db_cleanup_interval_seconds)
        content_guard_probe_interval = max(
            300,
            int(getattr(setting, "content_guard_probe_interval_sec", 3600) or 3600),
        )
    finally:
        db.close()
    try:
        scheduler.remove_job("provider_health_check")
    except Exception:
        pass
    scheduler.add_job(
        scheduled_provider_l0_health_check,
        "interval",
        seconds=PROVIDER_L0_HEALTH_CHECK_INTERVAL_SEC,
        id="provider_l0_health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_model_l1_text_health_check,
        "interval",
        seconds=interval,
        id="model_l1_text_health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_model_l2_capability_health_check,
        "interval",
        seconds=max(MODEL_L2_CAPABILITY_CHECK_MIN_INTERVAL_SEC, interval * 3),
        id="model_l2_capability_health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_model_l3_content_integrity_health_check,
        "interval",
        seconds=content_guard_probe_interval,
        id="model_l3_content_integrity_health_check",
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
    scheduler.add_job(
        scheduled_responses_chat_adapter_session_cleanup,
        "interval",
        seconds=adapter_cleanup_interval,
        id="responses_chat_adapter_session_cleanup",
        replace_existing=True,
    )
