from __future__ import annotations

from datetime import datetime
import os
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.app_setting import AppSetting
from app.models.logging_events import RequestContentGuardEvent
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.routers.content_guard import content_guard_runtime_events
from app.routers.logging_api import list_content_guard_events
from app.services.log_service import LogService
from app.services.token_usage_service import TokenUsageService


def _make_session(*tables):
    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
    )
    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        for table in reversed(tables):
            conn.execute(text(f'DROP TABLE IF EXISTS "{table.__table__.name}" CASCADE'))
    for table in tables:
        table.__table__.create(bind=engine, checkfirst=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_token_backfill_skips_finalized_logs_with_missing_token_fields(monkeypatch) -> None:
    session_factory = _make_session(Provider, RequestLog)
    with session_factory() as db:
        db.add_all(
            [
                RequestLog(
                    log_type="chat",
                    request_path="/v1/chat/completions",
                    success=True,
                    api_client_key_id=1,
                    billing_status="billed",
                    billing_finalized_at=now_beijing(),
                    prompt_tokens=12,
                    completion_tokens=None,
                    total_tokens=12,
                ),
                RequestLog(
                    log_type="chat",
                    request_path="/v1/chat/completions",
                    success=True,
                    api_client_key_id=1,
                    billing_status="pending_tokens",
                    billing_finalized_at=None,
                    token_finalize_attempt_count=LogService.TOKEN_FINALIZE_MAX_ATTEMPTS,
                ),
                RequestLog(
                    log_type="chat",
                    request_path="/v1/chat/completions",
                    success=True,
                    api_client_key_id=1,
                    billing_status="pending_tokens",
                    billing_finalized_at=None,
                    token_finalize_attempt_count=0,
                    model_name="测试模型",
                ),
            ]
        )
        db.commit()

    enqueued_ids: list[int] = []
    monkeypatch.setattr("app.services.token_usage_service.SessionLocal", session_factory)
    monkeypatch.setattr(
        TokenUsageService,
        "enqueue_log_finalize",
        staticmethod(lambda **kwargs: enqueued_ids.append(int(kwargs["log_id"]))),
    )

    assert TokenUsageService.backfill_missing_usage(limit=20) == 1
    assert len(enqueued_ids) == 1


def test_content_guard_runtime_events_use_event_provider_and_model_without_request_log() -> None:
    session_factory = _make_session(Provider, RequestLog, RequestContentGuardEvent)
    with session_factory() as db:
        db.add(
            RequestContentGuardEvent(
                trace_id="trace-content-guard",
                provider_id=7,
                provider_name="测试提供商",
                provider_model_id=9,
                model_name="测试模型",
                requested_model="用户请求模型",
                request_path="/v1/responses",
                guard_stage="runtime",
                guard_result="block",
                risk_level="high",
                reason="命中高风险内容",
                action="block",
            )
        )
        db.commit()

        result = content_guard_runtime_events(page=1, page_size=20, db=db)

    assert result["total"] == 1
    event = result["events"][0]
    assert event["provider_name"] == "测试提供商"
    assert event["model_name"] == "测试模型"
    assert event["requested_model"] == "用户请求模型"
    assert event["request_path"] == "/v1/responses"


def test_typed_content_guard_events_fallback_to_request_log_context() -> None:
    session_factory = _make_session(Provider, AppSetting, RequestLog, RequestContentGuardEvent)
    with session_factory() as db:
        db.add(
            Provider(
                id=11,
                name="回填提供商",
                base_url="https://example.com/v1",
                api_key="sk-test",
            )
        )
        db.flush()
        request_log = RequestLog(
            log_type="chat",
            trace_id="trace-content-guard-fallback",
            provider_id=11,
            provider_name="回填提供商",
            resolved_provider_model_id=22,
            model_name="实际模型",
            requested_model="请求模型",
            request_path="/v1/chat/completions",
            is_stream=True,
            success=True,
        )
        db.add(request_log)
        db.flush()
        db.add(
            RequestContentGuardEvent(
                request_log_id=request_log.id,
                trace_id="trace-content-guard-fallback",
                guard_stage="stream_buffer",
                guard_result="pass",
                risk_level="low",
                reason="测试回填",
                action="allow",
            )
        )
        db.commit()

        result = list_content_guard_events(page=1, page_size=20, db=db)

    assert result["total"] == 1
    event = result["items"][0]
    assert event["provider_id"] == 11
    assert event["provider_name"] == "回填提供商"
    assert event["provider_model_id"] == 22
    assert event["model_name"] == "实际模型"
    assert event["requested_model"] == "请求模型"
    assert event["request_path"] == "/v1/chat/completions"
    assert event["is_stream"] is True


def test_configure_scheduler_removes_health_jobs_when_auto_health_check_disabled(monkeypatch) -> None:
    import app.tasks as tasks

    class FakeScheduler:
        def __init__(self) -> None:
            self.added: list[str] = []
            self.removed: list[str] = []

        def add_job(self, _func, _trigger, **kwargs) -> None:
            self.added.append(kwargs["id"])

        def remove_job(self, job_id: str) -> None:
            self.removed.append(job_id)

    fake_scheduler = FakeScheduler()
    fake_db = SimpleNamespace(close=lambda: None)
    setting = SimpleNamespace(
        auto_health_check=False,
        health_check_interval_sec=300,
        responses_chat_adapter_db_cleanup_interval_seconds=300,
        content_guard_enabled=False,
        content_guard_precheck_auto_enabled=False,
        content_guard_probe_interval_sec=3600,
        request_log_retention_days=90,
        admin_audit_log_retention_days=365,
        request_child_log_retention_days=90,
        exception_log_retention_days=180,
        health_log_retention_days=90,
        billing_log_retention_days=365,
        background_job_log_retention_days=90,
        user_operation_log_retention_days=180,
        asset_log_retention_days=90,
    )

    monkeypatch.setattr(tasks, "scheduler", fake_scheduler)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(tasks.SettingService, "get_or_create", staticmethod(lambda _db: setting))
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(token_usage_backfill_interval_seconds=15))

    tasks.configure_scheduler()

    assert "provider_l0_health_check" in fake_scheduler.removed
    assert "model_l1_text_health_check" in fake_scheduler.removed
    assert "model_l2_capability_health_check" in fake_scheduler.removed
    assert "provider_l0_health_check" not in fake_scheduler.added
    assert "model_l1_text_health_check" not in fake_scheduler.added


def test_token_backfill_empty_result_can_suppress_success_log() -> None:
    import app.tasks as tasks

    assert tasks._should_suppress_empty_success_log(
        {
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "enqueued_count": 0,
            "suppress_empty_success_log": True,
        },
        processed_count=0,
        success_count=0,
        failed_count=0,
    )


def test_content_integrity_job_summary_counts_child_probes() -> None:
    import app.tasks as tasks

    summary = tasks._content_integrity_job_summary(
        [
            {
                "provider_id": 1,
                "success": False,
                "model_results": [
                    {
                        "provider_model_id": 11,
                        "success": False,
                        "endpoint_results": [
                            {"success": True, "capability_key": "content_fixed_answer"},
                            {"success": False, "capability_key": "content_pollution_rules"},
                        ],
                    }
                ],
            },
            {
                "provider_id": 2,
                "success": True,
                "model_results": [
                    {
                        "provider_model_id": 22,
                        "success": True,
                        "endpoint_results": [
                            {"success": True, "capability_key": "content_fixed_answer"},
                        ],
                    }
                ],
            },
        ]
    )

    assert summary["processed_count"] == 3
    assert summary["success_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["provider_count"] == 2
    assert summary["provider_success"] == 1
    assert summary["provider_with_successful_probe"] == 2
    assert summary["model_count"] == 2
    assert summary["model_with_successful_probe"] == 2
    assert not tasks._should_suppress_empty_success_log(
        {
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 0,
            "enqueued_count": 1,
            "suppress_empty_success_log": True,
        },
        processed_count=1,
        success_count=0,
        failed_count=0,
    )

from app.utils.timezone import now_beijing
