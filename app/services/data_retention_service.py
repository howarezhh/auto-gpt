from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
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
        request_child_log_retention_days: int = 90,
        exception_log_retention_days: int = 180,
        health_log_retention_days: int = 7,
        billing_log_retention_days: int = 365,
        background_job_log_retention_days: int = 90,
        user_operation_log_retention_days: int = 180,
        asset_log_retention_days: int = 180,
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
        }
        changed = False

        health_check_cutoff = datetime.utcnow() - timedelta(hours=DataRetentionService.HEALTH_CHECK_LOG_RETENTION_HOURS)
        health_check_delete = db.execute(
            delete(RequestLog).where(
                RequestLog.created_at < health_check_cutoff,
                RequestLog.log_type.in_(DataRetentionService.HEALTH_CHECK_LOG_TYPES),
            )
        )
        result["health_check_logs_deleted"] = int(health_check_delete.rowcount or 0)
        changed = changed or result["health_check_logs_deleted"] > 0

        if request_log_retention_days > 0:
            request_cutoff = datetime.utcnow() - timedelta(days=request_log_retention_days)
            request_delete = db.execute(
                delete(RequestLog).where(
                    RequestLog.created_at < request_cutoff,
                    RequestLog.log_type.not_in(DataRetentionService.HEALTH_CHECK_LOG_TYPES),
                )
            )
            result["request_logs_deleted"] = int(request_delete.rowcount or 0)
            changed = changed or result["request_logs_deleted"] > 0

        if admin_audit_log_retention_days > 0:
            audit_cutoff = datetime.utcnow() - timedelta(days=admin_audit_log_retention_days)
            audit_delete = db.execute(
                delete(AdminAuditLog).where(AdminAuditLog.created_at < audit_cutoff)
            )
            result["admin_audit_logs_deleted"] = int(audit_delete.rowcount or 0)
            changed = changed or result["admin_audit_logs_deleted"] > 0

        if request_child_log_retention_days > 0:
            request_child_cutoff = datetime.utcnow() - timedelta(days=request_child_log_retention_days)
            for model in DataRetentionService.REQUEST_CHILD_EVENT_MODELS:
                deleted = db.execute(delete(model).where(model.created_at < request_child_cutoff))
                result["request_child_logs_deleted"] += int(deleted.rowcount or 0)
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
            health_cutoff = datetime.utcnow() - timedelta(days=health_log_retention_days)
            health_probe_delete = db.execute(delete(HealthProbeEvent).where(HealthProbeEvent.created_at < health_cutoff))
            health_run_delete = db.execute(delete(HealthCheckRun).where(HealthCheckRun.created_at < health_cutoff))
            result["health_probes_deleted"] = int(health_probe_delete.rowcount or 0)
            result["health_runs_deleted"] = int(health_run_delete.rowcount or 0)
            changed = changed or result["health_probes_deleted"] > 0 or result["health_runs_deleted"] > 0
        if billing_log_retention_days > 0:
            billing_cutoff = datetime.utcnow() - timedelta(days=billing_log_retention_days)
            token_delete = db.execute(delete(TokenFinalizeEvent).where(TokenFinalizeEvent.created_at < billing_cutoff))
            billing_delete = db.execute(delete(BillingProcessEvent).where(BillingProcessEvent.created_at < billing_cutoff))
            result["token_finalize_events_deleted"] = int(token_delete.rowcount or 0)
            result["billing_process_events_deleted"] = int(billing_delete.rowcount or 0)
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
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = db.execute(delete(model).where(model.created_at < cutoff))
        result[result_key] = int(deleted.rowcount or 0)
        return changed or result[result_key] > 0
