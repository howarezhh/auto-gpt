from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.services import health_service as health_module
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.health_service import HealthService
from app.services.probe_rate_limit_service import ProbeRateLimitResult, ProbeRateLimitService
from app.services.provider_service import ProviderService
from app.services.proxy_service import PreparedUpstreamRequest, ProxyService


@pytest.fixture(autouse=True)
def allow_endpoint_protocol_probe_rate_limit(monkeypatch):
    async def fake_claim(provider, provider_model, *, probe_type, limit_per_minute=None, window_seconds=None):
        return ProbeRateLimitResult(
            allowed=True,
            provider_id=provider.id,
            provider_model_id=provider_model.id,
            probe_type=probe_type,
            limit=4,
            window_seconds=60,
            retry_after_seconds=0,
            current_count=1,
            reason="",
        )

    monkeypatch.setattr(ProbeRateLimitService, "claim", staticmethod(fake_claim))


def _target() -> dict:
    return {
        "provider_id": 1,
        "provider_name": "测试提供商",
        "provider": {
            "id": 1,
            "name": "测试提供商",
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "provider_type": "openai_compatible",
            "protocol_type": "both",
            "timeout_ms": 30000,
            "max_retries": 0,
            "first_token_timeout_sec": 60,
        },
        "provider_model_id": 11,
        "model_name": "测试模型",
        "previous_supports_chat_completions": False,
        "previous_supports_responses": True,
        "previous_protocol_type": "responses",
    }


def test_endpoint_protocol_detection_uses_exact_standard_endpoints(monkeypatch) -> None:
    requested_paths: list[str] = []

    async def fake_setting():
        return SimpleNamespace(max_non_stream_response_body_bytes=1024 * 1024)

    def fake_prepare(provider, *, endpoint_path, payload):
        requested_paths.append(endpoint_path)
        if endpoint_path == "/chat/completions":
            assert payload["max_tokens"] == 16
            assert "max_completion_tokens" not in payload
            assert payload["messages"][0]["content"] == "只回复 pong"
        if endpoint_path == "/responses":
            assert payload["max_output_tokens"] == 16
            assert payload["input"] == "只回复 pong"
        return PreparedUpstreamRequest(request_path=endpoint_path, request_payload=payload)

    async def fake_send(provider, *, prepared, headers, requested_payload, setting, request_timeout_seconds=None):
        if prepared.request_path == "/chat/completions":
            return {"object": "chat.completion", "choices": [{"message": {"content": "pong"}}]}, None
        return {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "pong"}],
                }
            ],
        }, None

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(_target()))

    assert requested_paths == ["/chat/completions", "/responses"]
    assert result["update_allowed"] is True
    assert result["supports_chat_completions"] is True
    assert result["supports_responses"] is True
    assert result["protocol_type"] == "both"


def test_endpoint_protocol_detection_keeps_gemini_native_protocol(monkeypatch) -> None:
    async def fail_setting():
        raise AssertionError("原生协议模型不应执行端点协议检测或占用探针请求")

    target = _target()
    target["provider"] = {**target["provider"], "protocol_type": "gemini"}
    target["model_name"] = "gemini-2.5-pro"
    target["previous_supports_chat_completions"] = False
    target["previous_supports_responses"] = False
    target["previous_protocol_type"] = "gemini"

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fail_setting))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(target))

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "native_protocol_without_endpoint_protocol_detection"
    assert result["update_allowed"] is False
    assert result["supports_chat_completions"] is False
    assert result["supports_responses"] is False
    assert result["protocol_type"] == "gemini"
    assert result["endpoint_results"] == []


def test_endpoint_protocol_detection_uses_provider_level_timeout(monkeypatch) -> None:
    captured_timeouts: list[float | None] = []

    async def fake_setting():
        return SimpleNamespace(max_non_stream_response_body_bytes=1024 * 1024)

    def fake_prepare(provider, *, endpoint_path, payload):
        return PreparedUpstreamRequest(request_path=endpoint_path, request_payload=payload)

    async def fake_send(provider, *, prepared, headers, requested_payload, setting, request_timeout_seconds=None):
        captured_timeouts.append(request_timeout_seconds)
        if prepared.request_path == "/chat/completions":
            return {"object": "chat.completion", "choices": [{"message": {"content": "pong"}}]}, None
        return {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
        }, None

    target = _target()
    target["provider"] = {**target["provider"], "timeout_ms": 65000}
    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(target))

    assert result["protocol_type"] == "both"
    assert captured_timeouts == [65.0, 65.0]


def test_interactive_probe_timeout_respects_provider_first_token_timeout() -> None:
    provider = SimpleNamespace(timeout_ms=30000, first_token_timeout_sec=60)

    timeout_seconds = HealthService._interactive_probe_timeout_seconds(provider)

    assert timeout_seconds >= 66
    assert timeout_seconds <= HealthService.INTERACTIVE_PROBE_TIMEOUT_CAP_SECONDS


def test_interactive_health_endpoint_prefers_chat_when_both_protocols_are_enabled(monkeypatch) -> None:
    monkeypatch.setattr(ProviderService, "provider_supports_chat_completions", staticmethod(lambda _provider: True))
    monkeypatch.setattr(ProviderService, "provider_supports_responses", staticmethod(lambda _provider: True))

    provider = SimpleNamespace(id=1, name="测试提供商")
    provider_model = SimpleNamespace(
        id=1,
        model_name="测试模型",
        supports_chat_completions=True,
        supports_responses=True,
    )

    assert HealthService._interactive_endpoint_path(provider, provider_model) == "/chat/completions"


def test_interactive_probe_timeout_does_not_cancel_before_provider_window(monkeypatch) -> None:
    provider = SimpleNamespace(timeout_ms=30000, first_token_timeout_sec=1)
    provider_model = SimpleNamespace(id=1, model_name="测试模型")

    monkeypatch.setattr(HealthService, "INTERACTIVE_PROBE_TIMEOUT_SECONDS", 0.01)

    async def fake_probe(_model):
        await asyncio.sleep(0.03)
        return {"success": True, "message": "ok"}

    result = asyncio.run(
        HealthService._run_probe_with_interactive_timeout(
            provider,
            provider_model,
            fake_probe,
            interactive_mode=True,
        )
    )

    assert result["success"] is True
    assert result["message"] == "ok"


def test_stream_probe_503_keeps_endpoint_support_unknown(monkeypatch) -> None:
    async def fake_setting():
        return SimpleNamespace(stream_connect_timeout_seconds=4)

    async def fake_open_stream(*args, **kwargs):
        request = httpx.Request("POST", "https://example.com/v1/responses")
        response = httpx.Response(
            503,
            request=request,
            json={"error": {"message": "当前服务暂不可用，请稍后重试", "code": "service_unavailable"}},
        )
        raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_open_stream_with_endpoint_fallback", staticmethod(fake_open_stream))

    provider = SimpleNamespace(id=1, name="测试提供商", timeout_ms=30000, first_token_timeout_sec=60)
    provider_model = SimpleNamespace(id=1, model_name="测试模型")

    result = asyncio.run(
        HealthService._probe_formal_stream_endpoint(
            provider,
            provider_model,
            endpoint_path="/responses",
            payload={"model": "测试模型", "input": "ping"},
            interactive_mode=True,
        )
    )

    assert result["status_code"] == 503
    assert result["support_mode"] == "unknown"
    assert result["support_label"] == "responses stream 上游暂不可用，支持状态待确认"
    assert result["retryable"] is True


def test_endpoint_protocol_detection_preserves_previous_support_on_transient_endpoint_error(monkeypatch) -> None:
    async def fake_setting():
        return SimpleNamespace(max_non_stream_response_body_bytes=1024 * 1024)

    def fake_prepare(provider, *, endpoint_path, payload):
        return PreparedUpstreamRequest(request_path=endpoint_path, request_payload=payload)

    async def fake_send(provider, *, prepared, headers, requested_payload, setting, request_timeout_seconds=None):
        if prepared.request_path == "/chat/completions":
            return {"object": "chat.completion", "choices": [{"message": {"content": "pong"}}]}, None
        request = httpx.Request("POST", "https://example.com/v1/responses")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": "rate limit", "code": "rate_limit_exceeded"}},
        )
        raise httpx.HTTPStatusError("rate limit", request=request, response=response)

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(_target()))

    assert result["update_allowed"] is True
    assert result["supports_chat_completions"] is True
    assert result["supports_responses"] is True
    assert result["protocol_type"] == "both"
    assert result["endpoint_results"][1]["support_state"] == "unknown"


def test_endpoint_protocol_detection_marks_only_explicit_endpoint_unsupported(monkeypatch) -> None:
    async def fake_setting():
        return SimpleNamespace(max_non_stream_response_body_bytes=1024 * 1024)

    def fake_prepare(provider, *, endpoint_path, payload):
        return PreparedUpstreamRequest(request_path=endpoint_path, request_payload=payload)

    async def fake_send(provider, *, prepared, headers, requested_payload, setting, request_timeout_seconds=None):
        if prepared.request_path == "/chat/completions":
            return {"object": "chat.completion", "choices": [{"message": {"content": "pong"}}]}, None
        request = httpx.Request("POST", "https://example.com/v1/responses")
        response = httpx.Response(404, request=request, content=b"Cannot POST /v1/responses")
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(_target()))

    assert result["update_allowed"] is True
    assert result["supports_chat_completions"] is True
    assert result["supports_responses"] is False
    assert result["protocol_type"] == "chat_completions"
    assert result["endpoint_results"][1]["support_state"] == "unsupported"


def test_endpoint_protocol_detection_rejects_failed_responses_object() -> None:
    assert HealthService._endpoint_protocol_response_is_valid(
        "responses",
        {
            "id": "resp_failed",
            "object": "response",
            "status": "failed",
            "error": {"message": "upstream failed"},
            "output": [],
        },
    ) is False
    assert HealthService._endpoint_protocol_response_is_valid("responses", {"output_text": "pong"}) is False


def test_endpoint_protocol_detection_keeps_previous_protocol_when_both_endpoints_fail(monkeypatch) -> None:
    async def fake_setting():
        return SimpleNamespace(max_non_stream_response_body_bytes=1024 * 1024)

    def fake_prepare(provider, *, endpoint_path, payload):
        return PreparedUpstreamRequest(request_path=endpoint_path, request_payload=payload)

    async def fake_send(provider, *, prepared, headers, requested_payload, setting, request_timeout_seconds=None):
        return {"unexpected": True}, None

    monkeypatch.setattr(ProxyService, "_get_setting_async", staticmethod(fake_setting))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(_target()))

    assert result["update_allowed"] is False
    assert result["protocol_type"] == "responses"
    assert "保留原协议配置" in result["message"]


def test_endpoint_protocol_detection_updates_provider_model_fields(monkeypatch) -> None:
    provider_model = SimpleNamespace(
        id=11,
        supports_chat_completions=False,
        supports_responses=True,
        protocol_type="responses",
    )

    class FakeDb:
        committed = False
        rolled_back = False

        def get(self, model, item_id):
            return provider_model if item_id == 11 else None

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    async def fake_detect(target):
        return {
            "provider_id": 1,
            "provider_name": "测试提供商",
            "provider_model_id": 11,
            "model_name": "测试模型",
            "status": "passed",
            "message": "检测完成",
            "updated": False,
            "update_allowed": True,
            "supports_chat_completions": True,
            "supports_responses": False,
            "protocol_type": "chat_completions",
            "protocol_label": "Chat Completions API",
            "endpoint_results": [],
            "latency_ms": 1,
        }

    monkeypatch.setattr(HealthService, "_detect_endpoint_protocol_for_target", staticmethod(fake_detect))
    monkeypatch.setattr(ProviderService, "invalidate_provider_runtime_cache", staticmethod(lambda: None))
    db = FakeDb()

    result = asyncio.run(
        HealthService._run_endpoint_protocol_detection_targets(
            db,
            [_target()],
            trigger_type="manual_mount_matrix",
        )
    )

    assert db.committed is True
    assert db.rolled_back is False
    assert provider_model.supports_chat_completions is True
    assert provider_model.supports_responses is False
    assert provider_model.protocol_type == "chat_completions"
    assert result["updated_count"] == 1
    assert result["provider_results"][0]["updated_count"] == 1


def test_endpoint_protocol_detection_staggers_models_within_same_provider(monkeypatch) -> None:
    active = 0
    max_active = 0
    calls: list[int] = []

    async def fake_detect(target):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(target["provider_model_id"])
        await asyncio.sleep(0.05)
        active -= 1
        return {
            "provider_id": target["provider_id"],
            "provider_name": target["provider_name"],
            "provider_model_id": target["provider_model_id"],
            "model_name": target["model_name"],
            "status": "failed",
            "message": "保留原协议配置",
            "updated": False,
            "update_allowed": False,
            "supports_chat_completions": target["previous_supports_chat_completions"],
            "supports_responses": target["previous_supports_responses"],
            "protocol_type": target["previous_protocol_type"],
            "protocol_label": "Responses API",
            "endpoint_results": [],
            "latency_ms": 1,
        }

    class FakeDb:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    target_a = _target()
    target_b = {**_target(), "provider_model_id": 12, "model_name": "测试模型二"}
    monkeypatch.setattr(HealthService, "_detect_endpoint_protocol_for_target", staticmethod(fake_detect))
    monkeypatch.setattr(HealthService, "PROVIDER_MODEL_PROBE_STAGGER_SECONDS", 0.01)

    result = asyncio.run(
        HealthService._run_endpoint_protocol_detection_targets(
            FakeDb(),
            [target_a, target_b],
            trigger_type="manual_mount_matrix",
        )
    )

    assert calls == [11, 12]
    assert max_active == 2
    assert result["total"] == 2


def test_health_phase_probe_staggers_models_within_same_provider(monkeypatch) -> None:
    provider = SimpleNamespace(id=1, name="测试提供商", max_active_requests=100)
    model_a = SimpleNamespace(id=11, model_name="测试模型一")
    model_b = SimpleNamespace(id=12, model_name="测试模型二")
    active = 0
    max_active = 0
    calls: list[int] = []

    async def fake_probe(provider_model):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(provider_model.id)
        await asyncio.sleep(0.05)
        active -= 1
        return {
            "endpoint_path": "/responses",
            "endpoint_label": "responses stream",
            "success": True,
            "native_success": True,
            "adapted_success": False,
            "support_mode": "native",
            "support_label": "原生支持 responses stream",
            "latency_ms": 1,
            "status_code": 200,
            "message": "ok",
            "trace": [],
        }

    monkeypatch.setattr(HealthService, "PROVIDER_MODEL_PROBE_STAGGER_SECONDS", 0.01)
    results = asyncio.run(
        HealthService._run_phase_probe_specs_with_model_stagger(
            provider,
            [model_a, model_b],
            [{"probe": fake_probe}],
            interactive_mode=False,
        )
    )

    assert [item[0].id for item in results] == [11, 12]
    assert calls == [11, 12]
    assert max_active == 2


def test_interactive_model_health_does_not_run_trust_probe(monkeypatch) -> None:
    provider = SimpleNamespace(id=1, name="测试提供商")
    provider_model = SimpleNamespace(id=11, provider_id=1, model_name="测试模型")

    async def fake_model_checks(*args, **kwargs):
        return [
            {
                "model_name": "测试模型",
                "success": False,
                "provider_success": False,
                "health_status": "unhealthy",
                "latency_ms": 2697,
                "status_code": 503,
                "message": "上游失败",
                "endpoint_results": [
                    {
                        "endpoint_path": "/responses",
                        "endpoint_label": "responses stream",
                        "success": False,
                        "latency_ms": 2697,
                        "message": "上游失败",
                    }
                ],
            }
        ]

    async def fail_if_trust_probe_runs(*args, **kwargs):
        raise AssertionError("健康检测不应隐式触发可信检测")

    monkeypatch.setattr(HealthService, "_run_provider_model_checks", staticmethod(fake_model_checks))
    monkeypatch.setattr(HealthService, "_persist_model_health_result", staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(HealthService, "_record_run_results", staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(ContentTrustProbeService, "run_trust_probe", staticmethod(fail_if_trust_probe_runs))
    monkeypatch.setattr(health_module.HealthLogRecorder, "start_run", staticmethod(lambda *args, **kwargs: SimpleNamespace(run_id="run")))
    monkeypatch.setattr(health_module.HealthLogRecorder, "finish_run", staticmethod(lambda *args, **kwargs: None))

    result = asyncio.run(
        HealthService.check_provider_model(
            SimpleNamespace(),
            provider,
            provider_model,
            phase_keys=HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
            interactive_mode=True,
            parallel_phases=True,
            single_endpoint_mode=True,
        )
    )

    assert result["latency_ms"] == 2697
    assert "content_probe_results" not in result
    assert "trust_status" not in result


def test_endpoint_protocol_detection_rate_limit_does_not_send_or_update(monkeypatch) -> None:
    sent = False

    async def fake_claim(provider, provider_model, *, probe_type, limit_per_minute=None, window_seconds=None):
        return ProbeRateLimitResult(
            allowed=False,
            provider_id=provider.id,
            provider_model_id=provider_model.id,
            probe_type=probe_type,
            limit=4,
            window_seconds=60,
            retry_after_seconds=30,
            current_count=4,
            reason="探针频率限制：测试原因",
        )

    def fake_prepare(provider, *, endpoint_path, payload):
        raise AssertionError("限频时不应准备上游请求")

    async def fake_send(*args, **kwargs):
        nonlocal sent
        sent = True
        raise AssertionError("限频时不应发送上游请求")

    monkeypatch.setattr(ProbeRateLimitService, "claim", staticmethod(fake_claim))
    monkeypatch.setattr(ProxyService, "_prepare_upstream_request", staticmethod(fake_prepare))
    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send))

    result = asyncio.run(HealthService._detect_endpoint_protocol_for_target(_target()))

    assert sent is False
    assert result["status"] == "rate_limited"
    assert result["error_code"] == "probe_rate_limited"
    assert result["update_allowed"] is False
    assert result["protocol_type"] == "responses"
    assert "测试原因" in result["message"]
