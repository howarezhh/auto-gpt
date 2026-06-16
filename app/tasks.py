from app.utils.timezone import now_beijing
import inspect
import logging
import asyncio
from datetime import datetime
from typing import Any
from collections.abc import Callable
from functools import wraps
from uuid import uuid4

from app.config import get_settings
from app.database import SessionLocal
from app.logging.sanitizers import dumps_sanitized
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.logging_events import BackgroundJobEvent
from app.scheduler import scheduler
from app.services.data_retention_service import DataRetentionService
from app.services.health_service import HealthService
from app.services.ip_management_event_service import IpManagementEventService
from app.services.ip_management_service import IpManagementService
from app.logging.adapters.background_job_adapter import BackgroundJobLogRecorder
from app.services.log_service import LogService
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.redis_service import RedisService
from app.services.responses_chat_adapter_service import ResponsesChatAdapterService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService


logger = logging.getLogger(__name__)
PROVIDER_L0_HEALTH_CHECK_INTERVAL_SEC = 120
MODEL_L2_CAPABILITY_CHECK_MIN_INTERVAL_SEC = 60 * 30
BACKGROUND_JOB_RESULT_SUMMARY_MAX_BYTES = 8192
RECENT_RUNTIME_HEALTH_REFRESH_INTERVAL_SEC = 5
RECENT_RUNTIME_HEALTH_WINDOW_SEC = 5

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _duration_ms(started_at: datetime, finished_at: datetime | None = None) -> int:
    finished = finished_at or now_beijing()
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
    trigger_type: str = "scheduler",
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
            trigger_type=trigger_type,
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
    trigger_type: str = "scheduler",
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
            trigger_type=trigger_type,
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
                trigger_type=trigger_type,
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
        item.result_summary_json = dumps_sanitized(
            result_summary,
            max_string_length=500,
            max_bytes=BACKGROUND_JOB_RESULT_SUMMARY_MAX_BYTES,
            max_list_items=50,
        )
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
            trigger_type = str(kwargs.pop("trigger_type", "scheduler") or "scheduler")
            token = uuid4().hex
            lock_key = f"scheduler:lock:{job_name}"
            state_key = f"scheduler:job:{job_name}:state"
            job_run_id = BackgroundJobLogRecorder.new_run_id()
            started_at = now_beijing()
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
                    trigger_type=trigger_type,
                    lock_key=lock_key,
                    lock_status="unavailable_skipped",
                    status="skipped_lock_unavailable",
                    started_at=started_at,
                    finished_at=now_beijing(),
                    error=str(exc)[:1000],
                )
                return None
            if not acquired:
                try:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "skipped_locked",
                            "job_run_id": job_run_id,
                            "trigger_type": trigger_type,
                            "lock_status": "skipped_locked",
                            "updated_at": now_beijing().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(lock_ttl_seconds, 300))
                except Exception:
                    pass
                _safe_record_job_event(
                    job_run_id=job_run_id,
                    job_name=job_name,
                    trigger_type=trigger_type,
                    lock_key=lock_key,
                    lock_status="skipped_locked",
                    status="skipped_locked",
                    started_at=started_at,
                    finished_at=now_beijing(),
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
                            "job_run_id": job_run_id,
                            "trigger_type": trigger_type,
                            "lock_status": lock_status,
                            "started_at": now_beijing().isoformat(),
                            "updated_at": now_beijing().isoformat(),
                        },
                    )
                    await client.expire(state_key, max(lock_ttl_seconds, 300))
                job_event_id = _safe_record_job_event(
                    job_run_id=job_run_id,
                    job_name=job_name,
                    trigger_type=trigger_type,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="running",
                    started_at=started_at,
                )
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                finished_at = now_beijing()
                if client is not None and lock_acquired:
                    await client.hset(
                        state_key,
                        mapping={
                            "status": "success",
                            "job_run_id": job_run_id,
                            "trigger_type": trigger_type,
                            "lock_status": lock_status,
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
                    trigger_type=trigger_type,
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
                finished_at = now_beijing()
                try:
                    if client is not None and lock_acquired:
                        await client.hset(
                            state_key,
                            mapping={
                                "status": "cancelled",
                                "job_run_id": job_run_id,
                                "trigger_type": trigger_type,
                                "lock_status": lock_status,
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
                    trigger_type=trigger_type,
                    lock_key=lock_key,
                    lock_status=lock_status,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=finished_at,
                )
                raise
            except Exception as exc:
                finished_at = now_beijing()
                try:
                    if client is not None and lock_acquired:
                        await client.hset(
                            state_key,
                            mapping={
                                "status": "failed",
                                "job_run_id": job_run_id,
                                "trigger_type": trigger_type,
                                "lock_status": lock_status,
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
                    trigger_type=trigger_type,
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


def _runtime_metric_priority(metrics: dict[str, Any]) -> tuple[int, int, int]:
    decision = str(metrics.get("decision") or "")
    return (
        1 if decision == "probe_required" else 0,
        int(metrics.get("upstream_failure_requests") or 0),
        int(metrics.get("total_requests") or 0),
    )


def _record_runtime_health_state_event(
    db,
    *,
    provider: Provider,
    provider_model: ProviderModel,
    metrics: dict[str, Any],
    success: bool,
    message: str,
    status_code: int | None = None,
) -> None:
    LogService.create_log(
        db,
        log_type="health_check_model",
        provider_id=provider.id,
        provider_name=provider.name,
        model_name=provider_model.model_name,
        resolved_provider_model_id=provider_model.id,
        request_path="/runtime-health-state",
        success=success,
        status_code=status_code,
        latency_ms=int(float(metrics.get("avg_latency_ms") or 0)),
        ttfb_ms=int(float(metrics.get("avg_ttfb_ms") or 0)) if metrics.get("avg_ttfb_ms") is not None else None,
        message=message,
        capability_result={
            "runtime_metrics": {
                "provider_id": provider.id,
                "provider_model_id": provider_model.id,
                "model_name": provider_model.model_name,
                "requested_model": metrics.get("requested_model"),
                "window_seconds": metrics.get("window_seconds"),
                "total_requests": metrics.get("total_requests"),
                "success_requests": metrics.get("success_requests"),
                "failed_requests": metrics.get("failed_requests"),
                "upstream_failure_requests": metrics.get("upstream_failure_requests"),
                "ignored_failure_requests": metrics.get("ignored_failure_requests"),
                "success_rate": metrics.get("success_rate"),
                "upstream_failure_rate": metrics.get("upstream_failure_rate"),
                "decision": metrics.get("decision"),
                "confidence": metrics.get("confidence"),
                "latest_error_code": metrics.get("latest_error_code"),
                "latest_error_category": metrics.get("latest_error_category"),
                "latest_trace_id": metrics.get("latest_trace_id"),
            }
        },
        schedule_token_fill=False,
        auto_commit=False,
    )


def _apply_runtime_healthy_signal(
    db,
    *,
    provider: Provider,
    provider_model: ProviderModel,
    metrics: dict[str, Any],
) -> bool:
    previous_status = str(provider_model.health_status or "unknown")
    previous_circuit = str(provider_model.circuit_state or "closed")
    status_changed = previous_status != "healthy" or previous_circuit != "closed" or bool(provider_model.last_error)
    ProviderHealthStateService.record_runtime_metrics(
        provider,
        provider_model,
        metrics,
        health_status="healthy",
        circuit_state="closed",
        status_update_reason="runtime_healthy_signal",
    )
    if not status_changed:
        return False
    now = now_beijing()
    provider_model.health_status = "healthy"
    provider_model.circuit_state = "closed"
    provider_model.circuit_opened_at = None
    provider_model.failure_count = 0
    provider_model.last_error = None
    provider_model.last_check_at = now
    if metrics.get("avg_latency_ms") is not None:
        provider_model.last_latency_ms = int(float(metrics.get("avg_latency_ms") or 0))
    provider.last_check_at = now
    if provider_model.last_latency_ms is not None:
        provider.last_latency_ms = provider_model.last_latency_ms
    ProviderService.refresh_provider_state(provider)
    _record_runtime_health_state_event(
        db,
        provider=provider,
        provider_model=provider_model,
        metrics=metrics,
        success=True,
        message=(
            "短窗口正式请求成功信号已恢复模型可用状态；"
            f"模型唯一ID={provider_model.id}，模型名={provider_model.model_name}"
        ),
        status_code=200,
    )
    return True


def _apply_runtime_probe_unavailable_unhealthy_signal(
    db,
    *,
    provider: Provider,
    provider_model: ProviderModel,
    metrics: dict[str, Any],
    reason: str,
) -> bool:
    previous_status = str(provider_model.health_status or "unknown")
    previous_circuit = str(provider_model.circuit_state or "closed")
    now = now_beijing()
    message = (
        "短窗口正式请求异常信号需要可用性探针确认，但探针未能触发或执行失败；"
        "为避免继续路由到疑似异常上游，已默认更新为异常状态。"
        f"模型唯一ID={provider_model.id}，模型名={provider_model.model_name}；原因={reason[:300]}"
    )
    ProviderHealthStateService.record_runtime_metrics(
        provider,
        provider_model,
        metrics,
        health_status="unhealthy",
        circuit_state="open",
        status_update_reason="runtime_probe_unavailable_default_unhealthy",
    )
    provider_model.health_status = "unhealthy"
    provider_model.circuit_state = "open"
    provider_model.circuit_opened_at = now
    provider_model.failure_count = max(1, int(provider_model.failure_count or 0) + 1)
    provider_model.last_error = reason[:500]
    provider_model.last_check_at = now
    provider.last_check_at = now
    ProviderService.refresh_provider_state(provider)
    _record_runtime_health_state_event(
        db,
        provider=provider,
        provider_model=provider_model,
        metrics=metrics,
        success=False,
        status_code=503,
        message=message,
    )
    return previous_status != "unhealthy" or previous_circuit != "open"


@distributed_job_lock("recent_runtime_health_state_refresh", ttl_seconds=60)
async def scheduled_recent_runtime_health_state_refresh() -> dict[str, int]:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        if not setting.auto_health_check:
            return {
                "processed_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_auto_health_disabled": 1,
            }
        metrics_by_key = LogService.provider_model_recent_runtime_metrics_batch(
            db,
            window_seconds=RECENT_RUNTIME_HEALTH_WINDOW_SEC,
            max_rows=0,
        )
        metrics_items = sorted(metrics_by_key.values(), key=_runtime_metric_priority, reverse=True)
        processed_count = 0
        success_count = 0
        failed_count = 0
        status_update_count = 0
        probe_count = 0
        skipped_disabled_count = 0
        for metrics in metrics_items:
            provider_id = int(metrics.get("provider_id") or 0)
            provider_model_id = int(metrics.get("provider_model_id") or 0)
            if provider_id <= 0 or provider_model_id <= 0:
                continue
            provider = db.get(Provider, provider_id)
            provider_model = db.get(ProviderModel, provider_model_id)
            if provider is None or provider_model is None or int(provider_model.provider_id) != provider_id:
                continue
            if not provider.enabled or not provider_model.enabled:
                skipped_disabled_count += 1
                continue
            processed_count += 1
            ProviderHealthStateService.record_runtime_metrics(provider, provider_model, metrics)
            decision = str(metrics.get("decision") or "")
            if decision == "healthy_signal":
                if _apply_runtime_healthy_signal(
                    db,
                    provider=provider,
                    provider_model=provider_model,
                    metrics=metrics,
                ):
                    status_update_count += 1
                success_count += 1
                continue
            if decision != "probe_required":
                success_count += 1
                continue
            probe_count += 1
            try:
                result = await HealthService.check_provider_model(
                    db,
                    provider,
                    provider_model,
                    stream_probe=False,
                    vision_probe=False,
                    phase_keys=HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
                    text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
                    parallel_phases=False,
                    single_endpoint_mode=True,
                )
                ProviderHealthStateService.record_runtime_metrics(
                    provider,
                    provider_model,
                    metrics,
                    health_status=str(provider_model.health_status or result.get("health_status") or "unknown"),
                    circuit_state=str(provider_model.circuit_state or "closed"),
                    status_update_reason="runtime_probe_confirmed",
                )
                _record_runtime_health_state_event(
                    db,
                    provider=provider,
                    provider_model=provider_model,
                    metrics=metrics,
                    success=bool(result.get("success")),
                    status_code=result.get("status_code"),
                    message=(
                        "短窗口正式请求异常信号已触发可用性探针并完成状态确认；"
                        f"模型唯一ID={provider_model.id}，模型名={provider_model.model_name}；"
                        f"探针结果={result.get('health_status') or provider_model.health_status}"
                    ),
                )
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
                    status_update_count += 1
                    if result.get("probe_rate_limited"):
                        if _apply_runtime_probe_unavailable_unhealthy_signal(
                            db,
                            provider=provider,
                            provider_model=provider_model,
                            metrics=metrics,
                            reason=str(result.get("message") or "自动可用性探针被频率限制"),
                        ):
                            status_update_count += 1
            except Exception as exc:
                failed_count += 1
                logger.warning(
                    "Runtime health probe failed for provider %s provider_model %s: %s",
                    provider_id,
                    provider_model_id,
                    exc,
                )
                _record_runtime_health_state_event(
                    db,
                    provider=provider,
                    provider_model=provider_model,
                    metrics=metrics,
                    success=False,
                    message=(
                        "短窗口正式请求异常信号触发可用性探针，但探针执行失败；"
                        f"模型唯一ID={provider_model.id}，模型名={provider_model.model_name}；错误={str(exc)[:300]}"
                    ),
                )
                if _apply_runtime_probe_unavailable_unhealthy_signal(
                    db,
                    provider=provider,
                    provider_model=provider_model,
                    metrics=metrics,
                    reason=str(exc),
                ):
                    status_update_count += 1
        db.commit()
        return {
            "processed_count": processed_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "status_update_count": status_update_count,
            "probe_count": probe_count,
            "skipped_disabled_count": skipped_disabled_count,
        }
    except Exception:
        db.rollback()
        raise
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


def _should_suppress_empty_success_log(
    result: dict[str, int | bool] | None,
    *,
    processed_count: int,
    success_count: int,
    failed_count: int,
) -> bool:
    """兼容后台任务日志判断：显式要求且本轮无处理、无成功、无失败时抑制成功日志。"""
    if not isinstance(result, dict) or not bool(result.get("suppress_empty_success_log")):
        return False
    return int(processed_count or 0) == 0 and int(success_count or 0) == 0 and int(failed_count or 0) == 0


def _content_integrity_job_summary(results: list[dict] | None) -> dict[str, int]:
    provider_count = 0
    provider_success = 0
    provider_with_successful_probe = 0
    model_count = 0
    model_with_successful_probe = 0
    processed_count = 0
    success_count = 0
    failed_count = 0
    for provider_result in results or []:
        if not isinstance(provider_result, dict):
            continue
        provider_count += 1
        if bool(provider_result.get("success")):
            provider_success += 1
        provider_has_success = False
        for model_result in provider_result.get("model_results") or []:
            if not isinstance(model_result, dict):
                continue
            model_count += 1
            model_has_success = False
            endpoint_results = model_result.get("endpoint_results") or []
            for endpoint_result in endpoint_results:
                if not isinstance(endpoint_result, dict):
                    continue
                processed_count += 1
                if bool(endpoint_result.get("success")):
                    success_count += 1
                    model_has_success = True
                    provider_has_success = True
                else:
                    failed_count += 1
            if model_has_success:
                model_with_successful_probe += 1
        if provider_has_success:
            provider_with_successful_probe += 1
    return {
        "processed_count": processed_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "provider_count": provider_count,
        "provider_success": provider_success,
        "provider_with_successful_probe": provider_with_successful_probe,
        "model_count": model_count,
        "model_with_successful_probe": model_with_successful_probe,
    }


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
            alert_event_retention_days=setting.alert_event_retention_days,
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


@distributed_job_lock("ip_management_event_cleanup", ttl_seconds=60 * 60 * 8)
def scheduled_ip_management_event_cleanup() -> dict[str, int]:
    db = SessionLocal()
    try:
        setting = IpManagementService.get_or_create_setting(db)
        processed_count = IpManagementEventService.cleanup_old_events(db, retention_days=setting.event_retention_days)
        return {
            "processed_count": processed_count,
            "success_count": processed_count,
            "failed_count": 0,
            "deleted_count": processed_count,
        }
    finally:
        db.close()


def configure_scheduler() -> None:
    db = SessionLocal()
    try:
        setting = SettingService.get_or_create(db)
        health_check_enabled = bool(setting.auto_health_check)
        interval = max(300, setting.health_check_interval_sec)
        adapter_cleanup_interval = max(300, setting.responses_chat_adapter_db_cleanup_interval_seconds)
        content_guard_probe_interval = max(
            300,
            int(getattr(setting, "content_guard_probe_interval_sec", 3600) or 3600),
        )
        content_guard_precheck_enabled = bool(
            getattr(setting, "content_guard_enabled", True)
            and getattr(setting, "content_guard_precheck_auto_enabled", False)
        )
    finally:
        db.close()
    try:
        scheduler.remove_job("provider_health_check")
    except Exception:
        pass
    if health_check_enabled:
        scheduler.add_job(
            scheduled_recent_runtime_health_state_refresh,
            "interval",
            seconds=RECENT_RUNTIME_HEALTH_REFRESH_INTERVAL_SEC,
            id="recent_runtime_health_state_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=5,
        )
        scheduler.add_job(
            scheduled_provider_l0_health_check,
            "interval",
            seconds=PROVIDER_L0_HEALTH_CHECK_INTERVAL_SEC,
            id="provider_l0_health_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        scheduler.add_job(
            scheduled_model_l1_text_health_check,
            "interval",
            seconds=interval,
            id="model_l1_text_health_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            scheduled_model_l2_capability_health_check,
            "interval",
            seconds=max(MODEL_L2_CAPABILITY_CHECK_MIN_INTERVAL_SEC, interval * 3),
            id="model_l2_capability_health_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
    else:
        for job_id in (
            "recent_runtime_health_state_refresh",
            "provider_l0_health_check",
            "model_l1_text_health_check",
            "model_l2_capability_health_check",
        ):
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
    if content_guard_precheck_enabled:
        scheduler.add_job(
            scheduled_model_l3_content_integrity_health_check,
            "interval",
            seconds=content_guard_probe_interval,
            id="model_l3_content_integrity_health_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
    else:
        try:
            scheduler.remove_job("model_l3_content_integrity_health_check")
        except Exception:
            pass
    scheduler.add_job(
        scheduled_token_usage_backfill,
        "interval",
        seconds=max(15, int(get_settings().token_usage_backfill_interval_seconds or 15)),
        id="token_usage_backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15,
    )
    scheduler.add_job(
        scheduled_data_retention_cleanup,
        "interval",
        hours=6,
        id="data_retention_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        scheduled_responses_chat_adapter_session_cleanup,
        "interval",
        seconds=adapter_cleanup_interval,
        id="responses_chat_adapter_session_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        scheduled_ip_management_event_cleanup,
        "interval",
        hours=6,
        id="ip_management_event_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
