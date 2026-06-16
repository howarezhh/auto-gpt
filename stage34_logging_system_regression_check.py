from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.logging.dispatcher import LoggingDispatcher
from app.logging.adapters.alert_adapter import AlertLogRecorder
from app.logging.adapters.asset_adapter import AssetLogRecorder
from app.logging.adapters.background_job_adapter import BackgroundJobLogRecorder
from app.logging.adapters.billing_adapter import BillingLogRecorder
from app.logging.adapters.exception_adapter import ExceptionLogRecorder
from app.logging.adapters.health_adapter import HealthLogRecorder
from app.logging.adapters.request_adapter import RequestLogRecorder
from app.logging.adapters.user_operation_adapter import UserOperationLogRecorder
from app.models.logging_events import (
    AssetEvent,
    BackgroundJobEvent,
    BillingProcessEvent,
    ExceptionEvent,
    HealthCheckRun,
    HealthProbeEvent,
    RequestAuthEvent,
    RequestBillingEvent,
    RequestModelPermissionEvent,
    RequestProviderAttemptEvent,
    RequestRouteDecisionEvent,
    RequestValidationEvent,
    TokenFinalizeEvent,
    UserOperationAuditLog,
)
from app.models.alert_event import AlertEvent
from app.services.data_retention_service import DataRetentionService
from app.services.log_service import LogService


def main() -> None:
    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
    )
    engine = create_engine(database_url, future=True)
    if True:
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = SessionLocal()
        try:
            request_log = LogService.create_log(
                db,
                log_type="chat",
                trace_id="stage34-trace",
                request_path="/v1/chat/completions",
                http_method="POST",
                requested_model="测试模型",
                model_name="测试模型",
                success=True,
                status_code=200,
                api_client_key_id=1,
                api_client_key_name="测试密钥",
                api_client_key_prefix="sk-test",
                user_account_id=1,
                user_account_name="测试用户",
                api_client_auth_result="authenticated",
                request_body_json='{"structure":{"type":"object"}}',
                prompt_tokens=12,
                completion_tokens=8,
                total_tokens=20,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            assert request_log.id is not None
            assert db.scalar(select(RequestAuthEvent).where(RequestAuthEvent.request_log_id == request_log.id)) is not None
            assert db.scalar(select(RequestValidationEvent).where(RequestValidationEvent.request_log_id == request_log.id)) is not None
            assert db.scalar(select(RequestModelPermissionEvent).where(RequestModelPermissionEvent.request_log_id == request_log.id)) is not None

            native_log = LogService.create_log(
                db,
                log_type="chat",
                trace_id="stage34-native-trace",
                request_path="/v1/chat/completions",
                http_method="POST",
                requested_model="原生事件模型",
                model_name="原生事件模型",
                success=True,
                status_code=200,
                request_body_json='{"structure":{"type":"object"}}',
                trace=[
                    {
                        "typed_event": "request_validation",
                        "module": "proxy",
                        "event_result": "success",
                        "payload": {
                            "validation_stage": "native_schema",
                            "passed": True,
                            "request_body_summary_json": '{"native":true}',
                        },
                    },
                    {
                        "typed_event": "request_route_decision",
                        "module": "proxy",
                        "event_result": "success",
                        "payload": {
                            "route_round": 1,
                            "route_policy": "可用性优先",
                            "candidate_count": 1,
                            "selected_provider_id": 1,
                        },
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "module": "proxy",
                        "event_result": "success",
                        "payload": {
                            "provider_id": 1,
                            "provider_name": "测试提供商",
                            "actual_model": "原生事件模型",
                            "result": "success",
                            "status_code": 200,
                        },
                    },
                ],
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            native_events = RequestLogRecorder.build_events_from_summary(native_log)
            native_validation_events = [
                event for event in native_events
                if event.envelope.event_name == "request_validation"
            ]
            assert len(native_validation_events) == 1
            assert native_validation_events[0].payload["validation_stage"] == "native_schema"
            assert any(event.envelope.event_name == "request_route_decision" for event in native_events)
            assert any(event.envelope.event_name == "request_provider_attempt" for event in native_events)
            LoggingDispatcher.record_many(native_events, db=db, enqueue=False, auto_commit=True)
            assert db.scalar(select(RequestRouteDecisionEvent).where(RequestRouteDecisionEvent.request_log_id == native_log.id)) is not None
            assert db.scalar(select(RequestProviderAttemptEvent).where(RequestProviderAttemptEvent.request_log_id == native_log.id)) is not None

            RequestLogRecorder.record_billing(
                db,
                request_log_id=request_log.id,
                trace_id=request_log.trace_id,
                billing_stage="finalized",
                billing_event_id="stage34-billing",
                total_cost=0.01,
            )
            assert db.scalar(select(RequestBillingEvent).where(RequestBillingEvent.request_log_id == request_log.id)) is not None

            ExceptionLogRecorder.record_exception(
                db,
                exc=RuntimeError("stage34"),
                handler_name="stage34",
                request_path="/api/stage34",
                method="GET",
                trace_id="stage34-exception",
                status_code=500,
                error_code="stage34_error",
                message="stage34 error",
            )
            assert db.scalar(select(ExceptionEvent).where(ExceptionEvent.trace_id == "stage34-exception")) is not None

            run = HealthLogRecorder.start_run(db, trigger_type="manual_single", scope_type="provider", scope_id=1)
            HealthLogRecorder.record_probe(
                db,
                run_id=run.run_id,
                provider_id=1,
                probe_type="connectivity",
                success=True,
                latency_ms=10,
            )
            HealthLogRecorder.finish_run(db, run_id=run.run_id, results=[{"success": True}])
            assert db.scalar(select(HealthCheckRun).where(HealthCheckRun.run_id == run.run_id)).overall_result == "healthy"
            assert db.scalar(select(HealthProbeEvent).where(HealthProbeEvent.run_id == run.run_id)) is not None

            BillingLogRecorder.record_token_finalize(
                db,
                request_log_id=request_log.id,
                queue_source="regression",
                result="filled",
            )
            BillingLogRecorder.record_billing_process(
                db,
                request_log_id=request_log.id,
                api_client_key_id=1,
                user_account_id=1,
                billing_status="billed",
            )
            assert db.scalar(select(TokenFinalizeEvent).where(TokenFinalizeEvent.request_log_id == request_log.id)) is not None
            assert db.scalar(select(BillingProcessEvent).where(BillingProcessEvent.request_log_id == request_log.id)) is not None

            BackgroundJobLogRecorder.record_job_event(
                db,
                job_run_id="stage34-job",
                job_name="stage34",
                status="success",
            )
            assert db.scalar(select(BackgroundJobEvent).where(BackgroundJobEvent.job_run_id == "stage34-job")) is not None

            UserOperationLogRecorder.record_user_action(
                db,
                user_account_id=1,
                username="测试用户",
                action="run_self_test",
                entity_type="self_test",
                summary="运行接入自检",
            )
            assert db.scalar(select(UserOperationAuditLog).where(UserOperationAuditLog.action == "run_self_test")) is not None

            AssetLogRecorder.record_asset_event(
                db,
                asset_id=1,
                asset_event_type="upload",
                actor_type="user",
                actor_id="1",
                filename="测试图片.png",
                content_type="image/png",
                file_size_bytes=128,
                sha256_hex="abcdef1234567890",
                storage_scope="user_self_test",
            )
            assert db.scalar(select(AssetEvent).where(AssetEvent.filename == "测试图片.png")) is not None

            AlertLogRecorder.upsert_alert(
                db,
                alert_key="stage34:alert",
                alert_type="queue",
                severity="warning",
                title="日志队列告警",
                message="日志队列存在积压",
                payload={"queued": 3},
            )
            assert db.scalar(select(AlertEvent).where(AlertEvent.alert_key == "stage34:alert")) is not None

            from app.main import app
            from app.services.user_auth_service import require_admin_api_user

            def override_get_db():
                yield db

            app.dependency_overrides[get_db] = override_get_db
            app.dependency_overrides[require_admin_api_user] = lambda: {"id": 1, "username": "测试管理员"}
            try:
                with TestClient(app) as client:
                    request_logs_response = client.get("/api/logging/request-logs?keyword=stage34&page_size=10")
                    assert request_logs_response.status_code == 200, request_logs_response.text
                    assert request_logs_response.json()["total"] >= 2

                    timeline_response = client.get(f"/api/logging/request-logs/{native_log.id}/timeline")
                    assert timeline_response.status_code == 200, timeline_response.text
                    timeline_events = timeline_response.json()["events"]
                    assert any(item["event_type"] == "request_validation_events" for item in timeline_events)
                    assert any(item["event_type"] == "request_provider_attempt_events" for item in timeline_events)

                    health_runs_response = client.get("/api/logging/health-runs?page_size=10")
                    assert health_runs_response.status_code == 200, health_runs_response.text
                    assert health_runs_response.json()["total"] >= 1

                    health_detail_response = client.get(f"/api/logging/health-runs/{run.run_id}")
                    assert health_detail_response.status_code == 200, health_detail_response.text
                    assert len(health_detail_response.json()["probes"]) == 1

                    assert client.get("/api/logging/exceptions?page_size=10").status_code == 200
                    assert client.get("/api/logging/billing-events?page_size=10").status_code == 200
                    assert client.get("/api/logging/background-jobs?page_size=10").status_code == 200
                    assert client.get("/api/logging/user-operations?page_size=10").status_code == 200
                    assert client.get("/api/logging/admin-audits?page_size=10").status_code == 200
                    assert client.get("/api/logging/alert-events?page_size=10").status_code == 200
                    assert client.get("/api/logging/asset-events?page_size=10").status_code == 200

                    alert_export_response = client.get("/api/logging/export?typed_log_type=alert-events&limit=10")
                    assert alert_export_response.status_code == 200, alert_export_response.text
            finally:
                app.dependency_overrides.pop(get_db, None)
                app.dependency_overrides.pop(require_admin_api_user, None)

            cleanup_result = DataRetentionService.cleanup(
                db,
                request_log_retention_days=0,
                admin_audit_log_retention_days=0,
                request_child_log_retention_days=0,
                exception_log_retention_days=0,
                health_log_retention_days=0,
                billing_log_retention_days=0,
                background_job_log_retention_days=0,
                user_operation_log_retention_days=0,
                asset_log_retention_days=0,
            )
            assert "request_child_logs_deleted" in cleanup_result
        finally:
            db.close()
            engine.dispose()
    print("stage34 logging system regression check passed")


if __name__ == "__main__":
    main()
