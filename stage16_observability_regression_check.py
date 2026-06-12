from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text


TEMP_DB_PATH = Path("data/stage16-observability.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["REDIS_URL"] = ""

from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.models.request_log import RequestLog
from app.models.provider import Provider
from app.models.user_account import UserAccount
from app.services.system_metrics_service import SystemMetricsService
from app.utils.timezone import now_beijing


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        now = now_beijing()
        db.add(
            Provider(
                id=1,
                name="观测提供商",
                base_url="https://example.com/v1",
                api_key="upstream-secret",
                provider_type="openai_compatible",
                enabled=True,
                health_status="healthy",
                max_active_requests=1000,
                max_active_streams=1000,
                max_qps=1000,
            )
        )
        db.add(
            UserAccount(
                id=1,
                username="stage16-user",
                email="stage16-user@example.com",
                password_hash="stage16-only",
                role="user",
                enabled=True,
            )
        )
        db.flush()
        db.add_all(
            [
                RequestLog(
                    log_type="chat",
                    provider_id=1,
                    provider_name="观测提供商",
                    requested_model="stage16-model",
                    request_path="/v1/chat/completions",
                    is_stream=True,
                    success=True,
                    status_code=200,
                    latency_ms=80,
                    first_token_latency_ms=25,
                    ttfb_ms=25,
                    api_client_key_id=1,
                    api_client_key_prefix="sk-aotu-stage16",
                    user_account_id=1,
                    user_account_name="stage16-user",
                    billing_finalized_at=now,
                    created_at=now - timedelta(minutes=2),
                ),
                RequestLog(
                    log_type="responses",
                    provider_id=1,
                    provider_name="观测提供商",
                    requested_model="stage16-model",
                    request_path="/v1/responses",
                    is_stream=False,
                    success=False,
                    status_code=503,
                    latency_ms=160,
                    first_token_latency_ms=None,
                    ttfb_ms=None,
                    api_client_key_id=1,
                    api_client_key_prefix="sk-aotu-stage16",
                    user_account_id=1,
                    user_account_name="stage16-user",
                    billing_finalized_at=None,
                    created_at=now - timedelta(minutes=1),
                ),
                RequestLog(
                    log_type="api_client_auth",
                    request_path="/v1/chat/completions",
                    success=False,
                    status_code=401,
                    api_client_key_prefix="sk-aotu-stage16",
                    billing_finalized_at=None,
                    created_at=now,
                ),
            ]
        )
        db.commit()

        metrics = SystemMetricsService.collect(db, window_minutes=5, refresh_alerts=False)

    _assert(metrics["status"] in {"ready", "degraded"}, "system status should be present")
    _assert(metrics["redis"]["status"] == "disabled", "empty Redis URL should be reported as disabled")
    _assert(metrics["redis"]["request_log_queue"]["total"] is None, "disabled Redis should expose stable queue shape")
    _assert(metrics["redis"]["max_active_requests"] > 0, "global request limit should be exposed")
    _assert(metrics["bucket_minutes"] == 1, "5 minute window should use 1 minute buckets")
    _assert(metrics["traffic"]["total_requests"] == 2, "traffic should only include formal route logs")
    _assert(metrics["traffic"]["failed_requests"] == 1, "failed route count mismatch")
    _assert(metrics["traffic"]["status_5xx"] == 1, "5xx count mismatch")
    _assert(metrics["traffic"]["qps"] > 0, "qps should be derived from window seconds")
    _assert(metrics["traffic"]["stream_qps"] > 0, "stream qps should be derived from stream requests")
    _assert(metrics["traffic"]["latency_by_mode"]["stream"]["request_count"] == 1, "stream latency bucket mismatch")
    _assert(metrics["traffic"]["latency_by_mode"]["non_stream"]["request_count"] == 1, "non-stream latency bucket mismatch")
    _assert(metrics["traffic"]["terminal_breakdown"]["upstream_failed"] == 0, "terminal breakdown should not invent upstream failures")
    _assert("event_loop" in metrics["runtime"], "runtime should expose event loop delay snapshot")
    _assert("configuration_effectiveness" in metrics, "configuration effectiveness metadata should be exposed")
    _assert(isinstance(metrics["timeseries"], list), "timeseries should be embedded in system metrics")
    _assert(metrics["background"]["pending_finalize_logs"] == 1, "formal API-key billing backlog count mismatch")

    with SessionLocal() as db:
        try:
            db.execute(text("SELECT * FROM definitely_missing_stage16_table"))
        except Exception:
            pass
        try:
            SystemMetricsService._limits_snapshot(db)
        except Exception as exc:
            raise AssertionError(f"limits snapshot should swallow and rollback DB errors: {exc}") from exc
        db.execute(text("SELECT 1")).scalar()

    print("stage16 observability regression passed")


if __name__ == "__main__":
    main()
