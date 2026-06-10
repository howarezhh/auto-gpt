from __future__ import annotations

from pathlib import Path

from app.models.request_log import RequestLog


def test_request_logs_have_token_finalize_pending_partial_index() -> None:
    index = next(
        item
        for item in RequestLog.__table__.indexes
        if item.name == "ix_request_logs_token_finalize_pending"
    )
    predicate = str(index.dialect_options["postgresql"]["where"])

    assert "billing_finalized_at IS NULL OR billing_status = 'pending_tokens'" in predicate
    assert "api_client_key_id IS NOT NULL" in predicate
    assert "token_finalize_attempt_count IS NULL OR token_finalize_attempt_count < 3" in predicate


def test_token_finalize_pending_index_migration_exists() -> None:
    migration = Path("migrations/2026-06-10_add_token_finalize_pending_index.sql").read_text(encoding="utf-8")

    assert "CREATE INDEX IF NOT EXISTS ix_request_logs_token_finalize_pending" in migration
    assert "ON request_logs (created_at DESC, id DESC)" in migration
    assert "billing_finalized_at IS NULL OR billing_status = 'pending_tokens'" in migration
