import asyncio
from types import SimpleNamespace

from app.services.health_service import HealthService
from app.services.cache_service import CacheService
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.fixed_success_response_service import FixedSuccessResponseService
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService


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


def test_fixed_success_response_detection_uses_six_normalized_chars_minimum() -> None:
    CacheService.invalidate_prefix(FixedSuccessResponseService.CACHE_PREFIX)

    FixedSuccessResponseService.inspect_and_record(
        provider_id=911,
        provider_model_id=912,
        request_payload={"messages": [{"role": "user", "content": "请求 A"}]},
        response_text="模型暂不可用",
    )
    detected = FixedSuccessResponseService.inspect_and_record(
        provider_id=911,
        provider_model_id=912,
        request_payload={"messages": [{"role": "user", "content": "请求 B"}]},
        response_text="模型暂不可用",
    )

    assert detected["normalized_length"] == 6
    assert detected["detected"] is True


def test_fixed_success_response_detection_skips_under_six_normalized_chars() -> None:
    CacheService.invalidate_prefix(FixedSuccessResponseService.CACHE_PREFIX)

    FixedSuccessResponseService.inspect_and_record(
        provider_id=913,
        provider_model_id=914,
        request_payload={"messages": [{"role": "user", "content": "请求 A"}]},
        response_text="模型不可用",
    )
    detected = FixedSuccessResponseService.inspect_and_record(
        provider_id=913,
        provider_model_id=914,
        request_payload={"messages": [{"role": "user", "content": "请求 B"}]},
        response_text="模型不可用",
    )

    assert detected["normalized_length"] == 5
    assert detected["detected"] is False


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


def test_fixed_success_confirmation_passed_probe_clears_samples(monkeypatch) -> None:
    provider = SimpleNamespace(id=921, name="固定文本确认提供商")
    provider_model = SimpleNamespace(id=922, model_name="确认模型")
    detection = {"detected": True}
    trace: list[dict] = []
    CacheService.set(
        FixedSuccessResponseService.cache_key(provider.id, provider_model.id),
        [{"normalized_text": "模型暂不可用", "request_fingerprint": "a"}],
        ttl_seconds=60,
    )

    async def fake_probe(*_args, **_kwargs):
        return {
            "success": True,
            "endpoint_path": "/chat/completions",
            "status_code": 200,
            "message": "固定答案一致",
            "support_label": "固定答案探针通过",
        }

    monkeypatch.setattr(ContentGuardProbeService, "content_probe_endpoint_path", staticmethod(lambda *_args: "/chat/completions"))
    monkeypatch.setattr(ContentGuardProbeService, "probe_fixed_answer", staticmethod(fake_probe))

    decision = asyncio.run(
        ProxyService._confirm_fixed_success_response_detection(
            provider,
            provider_model,
            detection,
            trace=trace,
        )
    )

    assert decision["confirmed_normal"] is True
    assert decision["should_mark_unhealthy"] is False
    assert decision["reason"] == "fixed_answer_probe_passed"
    assert CacheService.get(FixedSuccessResponseService.cache_key(provider.id, provider_model.id)) is None
    assert trace[-1]["result"] == "fixed_answer_probe_confirmed"


def test_fixed_success_confirmation_retries_rate_limit_then_passes(monkeypatch) -> None:
    provider = SimpleNamespace(id=923, name="限频后恢复提供商")
    provider_model = SimpleNamespace(id=924, model_name="限频后恢复模型")
    detection = {"detected": True}
    calls: list[int] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_probe(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {
                "success": False,
                "probe_rate_limited": True,
                "error_code": "probe_rate_limited",
                "status_code": 429,
                "message": "探针频率限制",
            }
        return {
            "success": True,
            "endpoint_path": "/chat/completions",
            "status_code": 200,
            "message": "固定答案一致",
        }

    monkeypatch.setattr(ContentGuardProbeService, "content_probe_endpoint_path", staticmethod(lambda *_args: "/chat/completions"))
    monkeypatch.setattr(ContentGuardProbeService, "probe_fixed_answer", staticmethod(fake_probe))
    monkeypatch.setattr("app.services.proxy_service.asyncio.sleep", fake_sleep)

    decision = asyncio.run(ProxyService._confirm_fixed_success_response_detection(provider, provider_model, detection))

    assert decision["confirmed_normal"] is True
    assert decision["should_mark_unhealthy"] is False
    assert sleeps == [2]
    assert len(calls) == 2
    assert [item["rate_limited"] for item in decision["attempts"]] == [True, False]


def test_fixed_success_confirmation_marks_unhealthy_when_all_probe_attempts_rate_limited(monkeypatch) -> None:
    provider = SimpleNamespace(id=925, name="全部限频提供商")
    provider_model = SimpleNamespace(id=926, model_name="全部限频模型")
    detection = {"detected": True}
    calls: list[int] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_probe(*_args, **_kwargs):
        calls.append(1)
        return {
            "success": False,
            "probe_rate_limited": True,
            "error_code": "probe_rate_limited",
            "status_code": 429,
            "message": "探针频率限制",
        }

    monkeypatch.setattr(ContentGuardProbeService, "content_probe_endpoint_path", staticmethod(lambda *_args: "/chat/completions"))
    monkeypatch.setattr(ContentGuardProbeService, "probe_fixed_answer", staticmethod(fake_probe))
    monkeypatch.setattr("app.services.proxy_service.asyncio.sleep", fake_sleep)

    decision = asyncio.run(ProxyService._confirm_fixed_success_response_detection(provider, provider_model, detection))

    assert decision["confirmed_normal"] is False
    assert decision["should_mark_unhealthy"] is True
    assert decision["reason"] == "fixed_answer_probe_rate_limited"
    assert sleeps == [2, 5, 10]
    assert len(calls) == 4


def test_fixed_success_confirmation_marks_unhealthy_on_non_rate_limited_probe_failure(monkeypatch) -> None:
    provider = SimpleNamespace(id=927, name="固定答案失败提供商")
    provider_model = SimpleNamespace(id=928, model_name="固定答案失败模型")
    detection = {"detected": True}
    calls: list[int] = []
    sleeps: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_probe(*_args, **_kwargs):
        calls.append(1)
        return {
            "success": False,
            "support_mode": "probe_failed",
            "status_code": 200,
            "message": "固定答案探针返回内容与指定字符串不一致",
        }

    monkeypatch.setattr(ContentGuardProbeService, "content_probe_endpoint_path", staticmethod(lambda *_args: "/chat/completions"))
    monkeypatch.setattr(ContentGuardProbeService, "probe_fixed_answer", staticmethod(fake_probe))
    monkeypatch.setattr("app.services.proxy_service.asyncio.sleep", fake_sleep)

    decision = asyncio.run(ProxyService._confirm_fixed_success_response_detection(provider, provider_model, detection))

    assert decision["confirmed_normal"] is False
    assert decision["should_mark_unhealthy"] is True
    assert decision["reason"] == "fixed_answer_probe_failed"
    assert sleeps == []
    assert len(calls) == 1


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
