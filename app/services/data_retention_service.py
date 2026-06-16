from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
from app.models.alert_event import AlertEvent
from app.models.logging_events import (
    AssetEvent,
    BackgroundJobEvent,
    BillingProcessEvent,
    ExceptionEvent,
    HealthCheckRun,
    HealthProbeEvent,
    RequestAuthEvent,
    RequestBillingEvent,
    RequestContentGuardEvent,
    RequestErrorResponseEvent,
    RequestModelPermissionEvent,
    RequestProviderAttemptEvent,
    RequestRouteDecisionEvent,
    RequestStreamEvent,
    RequestUpstreamResponseEvent,
    RequestValidationEvent,
    TokenFinalizeEvent,
    UserOperationAuditLog,
)
from app.models.request_log import RequestLog


class DataRetentionService:
    HEALTH_CHECK_LOG_RETENTION_HOURS = 6
    HEALTH_CHECK_LOG_TYPES = ("health_check", "health_check_provider", "health_check_model")
    CLEANUP_BATCH_SIZE = 5000
    CLEANUP_MAX_BATCHES_PER_MODEL = 100

    REQUEST_CHILD_EVENT_MODELS = (
        RequestAuthEvent,
        RequestValidationEvent,
        RequestModelPermissionEvent,
        RequestRouteDecisionEvent,
        RequestProviderAttemptEvent,
        RequestUpstreamResponseEvent,
        RequestStreamEvent,
        RequestErrorResponseEvent,
        RequestContentGuardEvent,
        RequestBillingEvent,
    )

    @staticmethod
    def cleanup(
        db: Session,
        *,
        request_log_retention_days: int,
        admin_audit_log_retention_days: int,
        request_child_log_retention_days: int = 7,
        exception_log_retention_days: int = 7,
        health_log_retention_days: int = 7,
        billing_log_retention_days: int = 7,
        background_job_log_retention_days: int = 7,
        user_operation_log_retention_days: int = 7,
        asset_log_retention_days: int = 7,
        alert_event_retention_days: int = 7,
    ) -> dict[str, int]:
        result = {
            "request_logs_deleted": 0,
            "health_check_logs_deleted": 0,
            "admin_audit_logs_deleted": 0,
            "request_child_logs_deleted": 0,
            "exception_events_deleted": 0,
            "health_runs_deleted": 0,
            "health_probes_deleted": 0,
            "token_finalize_events_deleted": 0,
            "billing_process_events_deleted": 0,
            "background_job_events_deleted": 0,
            "user_operation_audit_logs_deleted": 0,
            "asset_events_deleted": 0,
            "alert_events_deleted": 0,
        }
        changed = False

        health_check_cutoff = now_beijing() - timedelta(hours=DataRetentionService.HEALTH_CHECK_LOG_RETENTION_HOURS)
        result["health_check_logs_deleted"] = DataRetentionService._delete_batched(
            db,
            RequestLog,
            RequestLog.created_at < health_check_cutoff,
            RequestLog.log_type.in_(DataRetentionService.HEALTH_CHECK_LOG_TYPES),
        )
        changed = changed or result["health_check_logs_deleted"] > 0

        if request_log_retention_days > 0:
            request_cutoff = now_beijing() - timedelta(days=request_log_retention_days)
            result["request_logs_deleted"] = DataRetentionService._delete_batched(
                db,
                RequestLog,
                RequestLog.created_at < request_cutoff,
                RequestLog.log_type.not_in(DataRetentionService.HEALTH_CHECK_LOG_TYPES),
            )
            changed = changed or result["request_logs_deleted"] > 0

        if admin_audit_log_retention_days > 0:
            audit_cutoff = now_beijing() - timedelta(days=admin_audit_log_retention_days)
            result["admin_audit_logs_deleted"] = DataRetentionService._delete_batched(
                db,
                AdminAuditLog,
                AdminAuditLog.created_at < audit_cutoff,
            )
            changed = changed or result["admin_audit_logs_deleted"] > 0

        if request_child_log_retention_days > 0:
            request_child_cutoff = now_beijing() - timedelta(days=request_child_log_retention_days)
            for model in DataRetentionService.REQUEST_CHILD_EVENT_MODELS:
                result["request_child_logs_deleted"] += DataRetentionService._delete_batched(
                    db,
                    model,
                    model.created_at < request_child_cutoff,
                )
            changed = changed or result["request_child_logs_deleted"] > 0

        changed = DataRetentionService._delete_by_created_at(
            db,
            result=result,
            changed=changed,
            result_key="exception_events_deleted",
            model=ExceptionEvent,
            days=exception_log_retention_days,
        )
        if health_log_retention_days > 0:
            health_cutoff = now_beijing() - timedelta(days=health_log_retention_days)
            result["health_probes_deleted"] = DataRetentionService._delete_batched(
                db,
                HealthProbeEvent,
                HealthProbeEvent.created_at < health_cutoff,
            )
            result["health_runs_deleted"] = DataRetentionService._delete_batched(
                db,
                HealthCheckRun,
                HealthCheckRun.created_at < health_cutoff,
            )
            changed = changed or result["health_probes_deleted"] > 0 or result["health_runs_deleted"] > 0
        if billing_log_retention_days > 0:
            billing_cutoff = now_beijing() - timedelta(days=billing_log_retention_days)
            result["token_finalize_events_deleted"] = DataRetentionService._delete_batched(
                db,
                TokenFinalizeEvent,
                TokenFinalizeEvent.created_at < billing_cutoff,
            )
            result["billing_process_events_deleted"] = DataRetentionService._delete_batched(
                db,
                BillingProcessEvent,
                BillingProcessEvent.created_at < billing_cutoff,
            )
            changed = changed or result["token_finalize_events_deleted"] > 0 or result["billing_process_events_deleted"] > 0
        changed = DataRetentionService._delete_by_created_at(
            db,
            result=result,
            changed=changed,
            result_key="background_job_events_deleted",
            model=BackgroundJobEvent,
            days=background_job_log_retention_days,
        )
        changed = DataRetentionService._delete_by_created_at(
            db,
            result=result,
            changed=changed,
            result_key="user_operation_audit_logs_deleted",
            model=UserOperationAuditLog,
            days=user_operation_log_retention_days,
        )
        changed = DataRetentionService._delete_by_created_at(
            db,
            result=result,
            changed=changed,
            result_key="asset_events_deleted",
            model=AssetEvent,
            days=asset_log_retention_days,
        )
        changed = DataRetentionService._delete_by_created_at(
            db,
            result=result,
            changed=changed,
            result_key="alert_events_deleted",
            model=AlertEvent,
            days=alert_event_retention_days,
        )

        if changed:
            db.commit()
        else:
            db.rollback()
        return result

    @staticmethod
    def _delete_by_created_at(
        db: Session,
        *,
        result: dict[str, int],
        changed: bool,
        result_key: str,
        model,
        days: int,
    ) -> bool:
        if days <= 0:
            return changed
        cutoff = now_beijing() - timedelta(days=days)
        result[result_key] = DataRetentionService._delete_batched(db, model, model.created_at < cutoff)
        return changed or result[result_key] > 0

    @staticmethod
    def _delete_batched(db: Session, model, *conditions) -> int:
        total_deleted = 0
        batch_size = DataRetentionService.CLEANUP_BATCH_SIZE
        batch_count = 0
        while True:
            if batch_count >= DataRetentionService.CLEANUP_MAX_BATCHES_PER_MODEL:
                break
            ids = list(
                db.scalars(
                    select(model.id)
                    .where(*conditions)
                    .order_by(model.id.asc())
                    .limit(batch_size)
                )
            )
            if not ids:
                break
            deleted = db.execute(delete(model).where(model.id.in_(ids)))
            deleted_count = int(deleted.rowcount or 0)
            total_deleted += deleted_count
            db.commit()
            batch_count += 1
            if len(ids) < batch_size or deleted_count <= 0:
                break
        return total_deleted

from app.utils.timezone import now_beijing
