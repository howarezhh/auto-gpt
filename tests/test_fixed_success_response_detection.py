from types import SimpleNamespace

from app.services.health_service import HealthService
from app.services.cache_service import CacheService
from app.services.fixed_success_response_service import FixedSuccessResponseService
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.log_service import LogService


def test_fixed_success_response_detection_compares_latest_two_success_texts() -> None:
    CacheService.invalidate_prefix(FixedSuccessResponseService.CACHE_PREFIX)

    first = FixedSuccessResponseService.inspect_and_record(
        provider_id=901,
        provider_model_id=902,
        request_payload={"messages": [{"role": "user", "content": "写一首短诗"}]},
        response_text="当前模型暂不可用，请稍后再试。",
    )
    second = FixedSuccessResponseService.inspect_and_record(
        provider_id=901,
        provider_model_id=902,
        request_payload={"messages": [{"role": "user", "content": "解释一下 HTTP 代理"}]},
        response_text="当前模型暂不可用，请稍后再试。",
    )

    assert first["detected"] is False
    assert second["detected"] is True
    assert second["similarity"] >= FixedSuccessResponseService.SIMILARITY_THRESHOLD
    assert second["request_fingerprint"] != second["previous_request_fingerprint"]


def test_fixed_success_response_detection_ignores_same_request_replay() -> None:
    CacheService.invalidate_prefix(FixedSuccessResponseService.CACHE_PREFIX)
    payload = {"messages": [{"role": "user", "content": "固定问题"}]}

    FixedSuccessResponseService.inspect_and_record(
        provider_id=903,
        provider_model_id=904,
        request_payload=payload,
        response_text="当前模型暂不可用，请稍后再试。",
    )
    repeated = FixedSuccessResponseService.inspect_and_record(
        provider_id=903,
        provider_model_id=904,
        request_payload=payload,
        response_text="当前模型暂不可用，请稍后再试。",
    )

    assert repeated["detected"] is False


def test_health_payload_exposes_availability_aliases() -> None:
    payload = ProviderHealthStateService._effective_health_payload(
        db_health="unhealthy",
        db_updated_at=None,
        runtime_state={"runtime_health_status": "healthy", "updated_at": "2026-06-15T00:00:00"},
    )

    assert payload["db_availability"] == "unavailable"
    assert payload["runtime_availability"] == "available"
    assert payload["effective_availability"] == "available"
    assert payload["availability_state_updated_at"] == "2026-06-15T00:00:00"


def test_fixed_success_recovery_requires_fixed_answer_probe(monkeypatch) -> None:
    provider = SimpleNamespace(
        id=905,
        name="测试提供商",
        health_status="unknown",
        circuit_state="open",
        last_check_at=None,
        last_latency_ms=None,
        failure_count=0,
        success_count=0,
        provider_models=[],
        enabled=True,
    )
    provider_model = SimpleNamespace(
        id=906,
        provider_id=905,
        model_name="测试模型",
        last_error=ProviderHealthStateService.FIXED_SUCCESS_RESPONSE_ERROR_CODE,
        health_status="unhealthy",
        circuit_state="open",
        failure_count=1,
        success_count=0,
        last_check_at=None,
        last_latency_ms=None,
        circuit_opened_at=None,
        enabled=True,
        provider=provider,
    )
    calls: list[dict] = []

    def fake_record_runtime_metrics(*args, **kwargs):
        calls.append(dict(kwargs))
        return {}

    monkeypatch.setattr(ProviderHealthStateService, "record_runtime_metrics", staticmethod(fake_record_runtime_metrics))
    monkeypatch.setattr(ProviderService, "refresh_provider_state", staticmethod(lambda *_args, **_kwargs: None))

    updated = HealthService._apply_model_health(
        None,
        provider,
        provider_model,
        health_status="healthy",
        latency_ms=17,
        error_message=None,
        endpoint_results=[{"probe_key": "json", "success": True}],
    )

    assert updated is False
    assert calls
    assert calls[0]["status_update_reason"] == "fixed_success_recovery_probe_required"
    assert provider_model.health_status == "unhealthy"
    assert provider_model.circuit_state == "open"


def test_fixed_success_recovery_guard_prevents_probe_runtime_state_overwrite(monkeypatch) -> None:
    provider = SimpleNamespace(
        id=907,
        name="测试提供商",
        health_status="unknown",
        circuit_state="open",
        last_check_at=None,
        last_latency_ms=None,
        failure_count=0,
        success_count=0,
        provider_models=[],
        enabled=True,
    )
    provider_model = SimpleNamespace(
        id=908,
        provider_id=907,
        model_name="测试模型",
        last_error=ProviderHealthStateService.FIXED_SUCCESS_RESPONSE_ERROR_CODE,
        health_status="unhealthy",
        circuit_state="open",
        failure_count=1,
        success_count=0,
        last_check_at=None,
        last_latency_ms=None,
        circuit_opened_at=None,
        enabled=True,
        provider=provider,
    )
    runtime_updates: list[dict] = []
    model_probe_records: list[dict] = []

    def fake_record_runtime_metrics(*args, **kwargs):
        runtime_updates.append(dict(kwargs))
        return {}

    def fake_record_model_probe(*args, **kwargs):
        model_probe_records.append(dict(kwargs))

    monkeypatch.setattr(ProviderHealthStateService, "record_runtime_metrics", staticmethod(fake_record_runtime_metrics))
    monkeypatch.setattr(ProviderHealthStateService, "record_model_probe", staticmethod(fake_record_model_probe))
    monkeypatch.setattr(ProviderService, "refresh_provider_state", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(LogService, "create_log", staticmethod(lambda *_args, **_kwargs: None))

    HealthService._persist_model_health_result(
        SimpleNamespace(),
        provider,
        provider_model,
        {
            "success": True,
            "health_status": "healthy",
            "latency_ms": 21,
            "message": "普通 JSON 探针成功",
            "endpoint_results": [{"probe_key": "json", "success": True, "latency_ms": 21}],
        },
    )

    assert runtime_updates
    assert runtime_updates[0]["status_update_reason"] == "fixed_success_recovery_probe_required"
    assert model_probe_records == []
    assert provider_model.health_status == "unhealthy"
    assert provider_model.circuit_state == "open"
