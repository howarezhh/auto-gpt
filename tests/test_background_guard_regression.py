from __future__ import annotations

from datetime import datetime
import os
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.logging_events import RequestContentGuardEvent
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.routers.content_guard import content_guard_runtime_events
from app.services.log_service import LogService
from app.services.token_usage_service import TokenUsageService


def _make_session(*tables):
    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
    )
    engine = create_engine(database_url, future=True)
    for table in reversed(tables):
        table.__table__.drop(bind=engine, checkfirst=True)
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
                    billing_finalized_at=datetime.utcnow(),
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
