from app.routers.logs import _has_log_delete_scope
from app.routers.logging_api import _has_typed_log_delete_scope


def test_request_log_delete_requires_explicit_scope() -> None:
    assert not _has_log_delete_scope({
        "log_type": None,
        "model_query": "",
        "start_at": None,
        "end_at": None,
    })
    assert _has_log_delete_scope({"start_at": "2026-06-13T00:00"})
    assert _has_log_delete_scope({"model_query": "gemini"})


def test_typed_log_delete_scope_counts_request_path_filter() -> None:
    assert not _has_typed_log_delete_scope({
        "keyword": "",
        "request_path": "",
        "start_at": None,
        "end_at": None,
    })
    assert _has_typed_log_delete_scope({"request_path": "/v1/messages"})
    assert _has_typed_log_delete_scope("content-guard-events", {"request_path": "/v1/messages"})
    assert not _has_typed_log_delete_scope("health-runs", {"request_path": "/v1/messages"})
