from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import stage33_content_guard_regression_check as stage33

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.content_guard import ContentGuardRunRequest
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_runtime_guard_service import ContentRuntimeGuardService
from app.services.content_guard_service import ContentGuardService
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.health_service import HealthService
from app.logging.adapters.health_adapter import HealthLogRecorder
from app.services.probe_rate_limit_service import ProbeRateLimitResult, ProbeRateLimitService
from app.services.provider_service import ProviderService


def test_stage33_content_guard_regression() -> None:
    stage33.main()


def test_content_trust_probe_execution_plan_reports_combined_request_count() -> None:
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_stream=True,
    )

    plan = ContentTrustProbeService.describe_probe_execution_plan(
        ["fixed_answer", "pollution_rules", "sse"],
        provider_model,
    )

    assert plan["logical_probe_count"] == 3
    assert plan["upstream_request_count"] == 2
    assert [group["request_key"] for group in plan["request_groups"]] == ["combined_text", "sse"]


def test_content_trust_probe_execution_plan_reports_stream_skip() -> None:
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_stream=False,
    )

    plan = ContentTrustProbeService.describe_probe_execution_plan(
        ["fixed_answer", "pollution_rules", "sse"],
        provider_model,
    )

    assert plan["logical_probe_count"] == 3
    assert plan["upstream_request_count"] == 1
    assert plan["skipped_probe_count"] == 1
    assert plan["skipped_probes"][0]["probe_key"] == "sse"


def test_content_probe_failure_reason_prefers_raw_upstream_error() -> None:
    reason = ContentGuardProbeService.content_probe_failure_reason(
        {
            "message": "Upstream request failed",
            "status_code": 502,
            "error_detail": {"message": "Upstream request failed", "code": "upstream_request_failed"},
            "raw_provider_response": {
                "status_code": 502,
                "body": {"error": {"message": "上游余额不足，请充值后重试"}},
                "normalized_error": {"message": "上游余额不足，请充值后重试", "code": "insufficient_balance"},
            },
        },
        fallback="文本内容完整性组合探针请求失败",
    )

    assert reason == "上游余额不足，请充值后重试"


def test_combined_content_probe_request_failure_uses_upstream_reason() -> None:
    async def fake_send(*args, **kwargs):
        return (
            None,
            1000,
            502,
            [{"result": "fake_trace"}],
            {
                "message": "Upstream request failed",
                "status_code": 502,
                "error_detail": {"message": "Upstream request failed", "code": "upstream_request_failed"},
                "raw_provider_response": {
                    "status_code": 502,
                    "body": {"error": {"message": "上游余额不足，请充值后重试"}},
                    "normalized_error": {"message": "上游余额不足，请充值后重试", "code": "insufficient_balance"},
                },
            },
        )

    async def run_case() -> None:
        original = ContentGuardProbeService.send_content_probe_json
        ContentGuardProbeService.send_content_probe_json = staticmethod(fake_send)
        try:
            provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="http://example.test")
            provider_model = SimpleNamespace(id=1, model_name="测试模型", custom_model_name="test-model")
            result = await ContentGuardProbeService.probe_fixed_answer_and_pollution_rules(
                provider,
                provider_model,
                endpoint_path="/responses",
            )
        finally:
            ContentGuardProbeService.send_content_probe_json = original
        assert result["fixed_answer"]["content_guard"]["content_guard_reason"] == "上游余额不足，请充值后重试"
        assert result["pollution_rules"]["content_guard"]["content_guard_reason"] == "上游余额不足，请充值后重试"

    asyncio.run(run_case())


def test_fixed_answer_match_requires_exact_value_after_limited_normalization() -> None:
    assert ContentGuardProbeService.fixed_answer_matches('"AOTU_CONTENT_GUARD_OK"') is True
    assert ContentGuardProbeService.fixed_answer_matches("AOTU_CONTENT_GUARD_OK\n") is True
    assert ContentGuardProbeService.fixed_answer_matches('{"marker": "AOTU_CONTENT_GUARD_OK"}') is True
    assert ContentGuardProbeService.fixed_answer_matches("“AOTU_CONTENT_GUARD_OK”。") is True
    assert ContentGuardProbeService.fixed_answer_matches("答案是 AOTU_CONTENT_GUARD_OK") is False
    assert ContentGuardProbeService.fixed_answer_matches("AOTU_CONTENT_GUARD_OK extra") is False
    assert ContentGuardProbeService.fixed_answer_matches('{"answer": "AOTU_CONTENT_GUARD_OK", "explanation": "ok"}') is False


def test_fixed_answer_prompt_reduces_wrapping_risk() -> None:
    prompt = ContentGuardProbeService.fixed_answer_prompt()

    assert "AOTU_CONTENT_GUARD_OK" in prompt
    assert "逐字符完全一致" in prompt
    assert "不要 Markdown、JSON、引号、标点、空格、换行、解释、前缀或后缀" in prompt


def test_content_trust_probe_execution_plan_supports_optional_vision_probe() -> None:
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试视觉模型",
        enabled=True,
        supports_stream=True,
        supports_vision=True,
    )

    plan = ContentTrustProbeService.describe_probe_execution_plan(
        ["fixed_answer", "pollution_rules", "sse", "vision"],
        provider_model,
    )

    assert plan["logical_probe_count"] == 4
    assert plan["upstream_request_count"] == 3
    assert [group["request_key"] for group in plan["request_groups"]] == ["combined_text", "sse", "vision"]
    assert "vision" not in plan["required_probe_keys"]


def test_content_trust_probe_execution_plan_skips_vision_when_model_disables_it() -> None:
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试文本模型",
        enabled=True,
        supports_stream=True,
        supports_vision=False,
    )

    plan = ContentTrustProbeService.describe_probe_execution_plan(
        ["vision"],
        provider_model,
    )

    assert plan["upstream_request_count"] == 0
    assert plan["skipped_probe_count"] == 1
    assert plan["skipped_probes"][0]["probe_key"] == "vision"


def test_external_content_guard_target_supports_vision_without_returning_api_key() -> None:
    payload = ContentGuardRunRequest(
        target_type="external",
        external={
            "base_url": "https://example.com/v1",
            "api_key": "sk-test-external",
            "model_name": "测试外部模型",
            "endpoint_path": "/responses",
        },
        probe_keys=["vision"],
    )

    provider, provider_model, target = ContentTrustProbeService.resolve_external_probe_target(payload.external)

    assert provider.api_key == "sk-test-external"
    assert provider_model.supports_vision is True
    assert provider_model.supports_responses is True
    assert "api_key" not in target


def test_content_guard_vision_payload_matches_selected_endpoint_protocol() -> None:
    provider_model = SimpleNamespace(model_name="测试视觉模型")

    chat_payload = ContentGuardProbeService.build_vision_payload(provider_model, endpoint_path="/chat/completions")
    responses_payload = ContentGuardProbeService.build_vision_payload(provider_model, endpoint_path="/responses")

    chat_content = chat_payload["messages"][0]["content"]
    responses_content = responses_payload["input"][0]["content"]
    assert chat_content[0]["type"] == "text"
    assert chat_content[1]["type"] == "image_url"
    assert chat_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert responses_content[0]["type"] == "input_text"
    assert responses_content[1]["type"] == "input_image"
    assert responses_content[1]["image_url"].startswith("data:image/png;base64,")


def test_content_guard_probe_uses_native_endpoint_for_native_protocol_mounts() -> None:
    provider = SimpleNamespace(protocol_type="gemini")
    provider_model = SimpleNamespace(
        model_name="gemini-2.5-pro",
        protocol_type="gemini",
        supports_chat_completions=False,
        supports_responses=False,
    )

    endpoint_path = ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model)

    assert endpoint_path == "/native/gemini"


def test_runtime_stream_guard_scans_native_gemini_and_claude_sse_events() -> None:
    setting = SimpleNamespace(
        content_guard_enabled=True,
        content_guard_stream_mode="pass_through_scan",
        content_guard_rules_json="",
        content_guard_url_allowlist_json="[]",
        content_guard_url_check_enabled=True,
    )
    request_payload = {"messages": [{"role": "user", "content": "不要输出广告或外链"}]}

    gemini_result = ContentRuntimeGuardService.inspect_stream_chunk(
        event_buffer=bytearray(),
        chunk='data: {"candidates":[{"content":{"parts":[{"text":"正常回答 https://ad.example.com"}]},"finishReason":"STOP"}]}\n\n'.encode("utf-8"),
        setting=setting,
        endpoint_path="/chat/completions",
        request_payload=request_payload,
    )
    claude_result = ContentRuntimeGuardService.inspect_stream_chunk(
        event_buffer=bytearray(),
        chunk='event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"加入社群领取免费key"}}\n\n'.encode("utf-8"),
        setting=setting,
        endpoint_path="/chat/completions",
        request_payload=request_payload,
    )

    assert gemini_result.result == ContentGuardService.RESULT_BLOCK
    assert "unexpected_link" in gemini_result.categories
    assert claude_result.result == ContentGuardService.RESULT_BLOCK
    assert any(rule.get("id") == "api_key_community_promotion" for rule in claude_result.matched_rules)


def test_native_stream_probe_terminal_events_follow_official_stream_shapes() -> None:
    assert ContentGuardProbeService.extract_probe_sse_text_delta(
        '{"candidates":[{"content":{"parts":[{"text":"AOTU_CONTENT_GUARD_OK"}]},"finishReason":"STOP"}]}'
    ) == "AOTU_CONTENT_GUARD_OK"
    assert ContentGuardProbeService.native_stream_event_is_terminal(
        "gemini",
        {"candidates": [{"finishReason": "STOP"}]},
    )
    assert ContentGuardProbeService.extract_probe_sse_text_delta(
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"AOTU_CONTENT_GUARD_OK"}}'
    ) == "AOTU_CONTENT_GUARD_OK"
    assert ContentGuardProbeService.native_stream_event_is_terminal(
        "claude_messages",
        {"type": "message_stop"},
    )


def test_content_guard_vision_probe_passes_when_upstream_reads_image(monkeypatch) -> None:
    async def fake_send(provider, provider_model, *, endpoint_path, payload, endpoint_label):
        assert payload["messages"][0]["content"][1]["type"] == "image_url"
        return (
            {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "红色"},
                        "finish_reason": "stop",
                    }
                ],
            },
            100,
            200,
            [{"result": "fake_trace"}],
            None,
        )

    monkeypatch.setattr(ContentGuardProbeService, "send_content_probe_json", staticmethod(fake_send))
    provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="https://example.com/v1")
    provider_model = SimpleNamespace(id=1, model_name="测试视觉模型", supports_vision=True)

    result = asyncio.run(
        ContentGuardProbeService.probe_vision(provider, provider_model, endpoint_path="/chat/completions")
    )

    assert result["success"] is True
    assert result["raw_provider_response"]["output_text"] == "红色"


def test_stream_guard_buffering_defaults_on_for_buffer_mode_even_with_legacy_provider_flag() -> None:
    setting = SimpleNamespace(content_guard_enabled=True, content_guard_stream_mode="buffer_300ms")
    provider = SimpleNamespace(
        content_guard_enabled=True,
        trust_level="standard",
        content_integrity_status="healthy",
        health_status="healthy",
        buffer_stream_for_guard=False,
    )

    assert ContentRuntimeGuardService.stream_should_buffer(
        setting=setting,
        provider=provider,
        route_context=None,
    )


def test_stream_guard_buffering_respects_global_pass_through_mode() -> None:
    setting = SimpleNamespace(content_guard_enabled=True, content_guard_stream_mode="pass_through_scan")
    provider = SimpleNamespace(
        content_guard_enabled=True,
        trust_level="standard",
        content_integrity_status="unknown",
        health_status="unknown",
        buffer_stream_for_guard=True,
    )

    assert not ContentRuntimeGuardService.stream_should_buffer(
        setting=setting,
        provider=provider,
        route_context=None,
    )


def test_health_log_probe_count_flattens_child_probes_without_duplicate_model_rows() -> None:
    flattened = HealthLogRecorder._flatten_probe_results(
        [
            {
                "provider_id": 1,
                "success": False,
                "model_results": [
                    {
                        "provider_model_id": 11,
                        "model_name": "测试模型",
                        "success": False,
                        "endpoint_results": [
                            {"success": True, "capability_key": "content_fixed_answer"},
                            {"success": False, "capability_key": "content_pollution_rules"},
                        ],
                    }
                ],
            },
            {
                "provider_id": 1,
                "provider_model_id": 11,
                "model_name": "测试模型",
                "scope": "model",
                "success": False,
                "endpoint_results": [
                    {"success": True, "capability_key": "content_fixed_answer"},
                    {"success": False, "capability_key": "content_pollution_rules"},
                ],
            },
        ]
    )

    assert len(flattened) == 2
    assert sum(1 for item in flattened if item["success"]) == 1


def test_content_probe_endpoint_prefers_chat_when_both_protocols_are_available(monkeypatch) -> None:
    monkeypatch.setattr(ContentGuardProbeService, "_content_guard_probe_protocol_type", staticmethod(lambda: "chat_completions"))
    provider = Provider(id=1, name="测试提供商", protocol_type="both", base_url="https://example.com/v1", api_key="sk-test")
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_chat_completions=True,
        supports_responses=True,
    )

    assert ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model) == "/chat/completions"


def test_content_probe_endpoint_uses_responses_when_chat_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(ContentGuardProbeService, "_content_guard_probe_protocol_type", staticmethod(lambda: "chat_completions"))
    provider = Provider(id=1, name="测试提供商", protocol_type="responses", base_url="https://example.com/v1", api_key="sk-test")
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_chat_completions=False,
        supports_responses=True,
    )

    assert ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model) == "/responses"


def test_content_probe_endpoint_uses_configured_responses_when_supported(monkeypatch) -> None:
    monkeypatch.setattr(ContentGuardProbeService, "_content_guard_probe_protocol_type", staticmethod(lambda: "responses"))
    provider = Provider(id=1, name="测试提供商", protocol_type="both", base_url="https://example.com/v1", api_key="sk-test")
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_chat_completions=True,
        supports_responses=True,
    )

    assert ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model) == "/responses"


def test_content_probe_endpoint_falls_back_to_chat_when_configured_responses_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(ContentGuardProbeService, "_content_guard_probe_protocol_type", staticmethod(lambda: "responses"))
    provider = Provider(id=1, name="测试提供商", protocol_type="both", base_url="https://example.com/v1", api_key="sk-test")
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_chat_completions=True,
        supports_responses=False,
    )

    assert ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model) == "/chat/completions"


def test_content_trust_probe_rate_limit_does_not_persist_trust_status(monkeypatch) -> None:
    provider = Provider(
        id=1,
        name="测试提供商",
        protocol_type="chat_completions",
        base_url="https://example.com/v1",
        api_key="sk-test",
        enabled=True,
        content_guard_enabled=True,
    )
    provider_model = ProviderModel(
        id=11,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        supports_stream=True,
        supports_chat_completions=True,
        supports_responses=False,
        content_integrity_status="unknown",
        content_probe_failure_count=0,
    )
    provider.provider_models = [provider_model]
    provider_model.provider = provider

    class FakeDb:
        def get(self, model, item_id):
            if model is Provider and item_id == 1:
                return provider
            if model is ProviderModel and item_id == 11:
                return provider_model
            return None

    async def fake_claim(provider_arg, provider_model_arg, *, probe_type, limit_per_minute=None, window_seconds=None):
        return ProbeRateLimitResult(
            allowed=False,
            provider_id=provider_arg.id,
            provider_model_id=provider_model_arg.id,
            probe_type=probe_type,
            limit=4,
            window_seconds=60,
            retry_after_seconds=20,
            current_count=4,
            reason="探针频率限制：测试内容防护限频",
        )

    def fail_update(*args, **kwargs):
        raise AssertionError("限频时不应更新可信或内容完整性状态")

    monkeypatch.setattr(ProbeRateLimitService, "claim", staticmethod(fake_claim))
    monkeypatch.setattr(ContentTrustProbeService, "update_provider_model_trust_status", staticmethod(fail_update))

    result = asyncio.run(
        ContentTrustProbeService.run_trust_probe(
            FakeDb(),
            ContentGuardRunRequest(
                target_type="internal",
                provider_id=1,
                provider_model_id=11,
                probe_keys=list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS),
                persist_internal_result=True,
            ),
        )
    )

    assert result["summary"]["status"] == "rate_limited"
    assert result["summary"]["error_code"] == "probe_rate_limited"
    assert "测试内容防护限频" in result["summary"]["content_guard_reason"]
    assert provider_model.content_integrity_status == "unknown"
    assert provider_model.content_probe_failure_count == 0


def test_content_trust_probe_transient_upstream_failure_does_not_persist_trust_status(monkeypatch) -> None:
    provider = Provider(
        id=2,
        name="上游临时不可用提供商",
        protocol_type="chat_completions",
        base_url="https://example.com/v1",
        api_key="sk-test",
        enabled=True,
        content_guard_enabled=True,
    )
    provider_model = ProviderModel(
        id=22,
        provider_id=2,
        model_name="测试模型",
        enabled=True,
        supports_stream=True,
        supports_chat_completions=True,
        supports_responses=False,
        content_integrity_status="unknown",
        content_probe_failure_count=0,
    )
    provider.provider_models = [provider_model]
    provider_model.provider = provider

    class FakeDb:
        def get(self, model, item_id):
            if model is Provider and item_id == 2:
                return provider
            if model is ProviderModel and item_id == 22:
                return provider_model
            return None

    def transient_result(phase_key: str) -> dict:
        return {
            "phase_key": phase_key,
            "capability_key": phase_key,
            "success": False,
            "status_code": 503,
            "retryable": True,
            "message": "全部渠道不可提供当前模型，请稍后重试",
            "content_guard_result": "review",
            "content_guard_reason": "全部渠道不可提供当前模型，请稍后重试",
        }

    async def fake_combined(provider_arg, provider_model_arg, endpoint_path, *, order_indexes):
        return [
            (order_indexes["fixed_answer"], transient_result("content_fixed_answer")),
            (order_indexes["pollution_rules"], transient_result("content_pollution_rules")),
        ]

    async def fake_single(provider_arg, provider_model_arg, endpoint_path, probe_key, *, order_index):
        return (order_index, transient_result("content_sse"))

    def fail_update(*args, **kwargs):
        raise AssertionError("上游临时不可用时不应更新可信或内容完整性状态")

    monkeypatch.setattr(ContentTrustProbeService, "run_combined_text_probe_with_boundary", staticmethod(fake_combined))
    monkeypatch.setattr(ContentTrustProbeService, "run_single_probe_with_boundary", staticmethod(fake_single))
    monkeypatch.setattr(ContentTrustProbeService, "update_provider_model_trust_status", staticmethod(fail_update))

    result = asyncio.run(
        ContentTrustProbeService.run_trust_probe(
            FakeDb(),
            ContentGuardRunRequest(
                target_type="internal",
                provider_id=2,
                provider_model_id=22,
                probe_keys=list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS),
                persist_internal_result=True,
            ),
        )
    )

    assert result["summary"]["status"] == "upstream_unavailable"
    assert result["summary"]["content_guard_result"] == "upstream_unavailable"
    assert "全部渠道不可提供当前模型" in result["summary"]["content_guard_reason"]
    assert provider_model.content_integrity_status == "unknown"
    assert provider_model.content_probe_failure_count == 0


def test_stream_health_probe_decompression_error_is_not_endpoint_unsupported(monkeypatch) -> None:
    provider = Provider(
        id=31,
        name="压缩头异常提供商",
        base_url="https://example.com/v1",
        api_key="sk-test",
        enabled=True,
        protocol_type="both",
        timeout_ms=30000,
        first_token_timeout_sec=1,
    )
    provider_model = ProviderModel(
        id=311,
        provider_id=31,
        model_name="测试模型",
        enabled=True,
        supports_stream=True,
        supports_chat_completions=True,
        supports_responses=True,
    )

    class BadStreamResponse:
        def aiter_bytes(self):
            async def iterator():
                raise RuntimeError("Error -3 while decompressing data: incorrect header check")
                yield b""

            return iterator()

    class FakeStreamContext:
        async def __aexit__(self, exc_type, exc_value, exc_traceback):
            return None

    async def fake_open_stream(provider_arg, provider_model_arg, endpoint_path, payload, **kwargs):
        assert kwargs.get("extra_headers", {}).get("Accept-Encoding") == "identity"
        return BadStreamResponse(), None, FakeStreamContext(), []

    async def fake_setting():
        return SimpleNamespace(stream_connect_timeout_seconds=1, stream_idle_timeout_seconds=1, stream_max_duration_seconds=1)

    async def fake_claim(*args, **kwargs):
        return None

    monkeypatch.setattr(HealthService, "_claim_probe_rate_limit_result", staticmethod(fake_claim))
    monkeypatch.setattr(HealthService, "_interactive_stream_connect_timeout_seconds", staticmethod(lambda provider_arg: 1))
    monkeypatch.setattr(HealthService, "_interactive_stream_first_token_timeout_seconds", staticmethod(lambda provider_arg: 1))
    monkeypatch.setattr(
        "app.services.health_service.ProxyService._get_setting_async",
        staticmethod(fake_setting),
    )
    monkeypatch.setattr(
        "app.services.health_service.ProxyService._open_stream_with_endpoint_fallback",
        staticmethod(fake_open_stream),
    )

    result = asyncio.run(
        HealthService._probe_formal_stream_endpoint(
            provider,
            provider_model,
            endpoint_path="/responses",
            payload=HealthService._build_responses_probe_payload(provider_model, vision_probe=False),
            interactive_mode=True,
        )
    )

    assert result["support_mode"] == "unknown"
    assert "压缩响应异常" in result["support_label"]
    assert "incorrect header check" in result["message"]


def test_health_probe_does_not_clear_content_integrity_state() -> None:
    provider = Provider(
        id=21,
        name="可用性内容隔离拆分提供商",
        base_url="https://example.com/v1",
        api_key="sk-test",
        enabled=True,
        health_status="unknown",
        circuit_state="closed",
        content_integrity_status="unknown",
    )
    provider_model = ProviderModel(
        id=211,
        provider_id=21,
        model_name="可用性内容隔离拆分模型",
        enabled=True,
        health_status="unhealthy",
        circuit_state="open",
        success_count=0,
        failure_count=0,
        content_integrity_status="blocked",
        content_probe_failure_count=3,
        content_probe_last_failed_at=now_beijing(),
    )
    provider.provider_models = [provider_model]
    provider_model.provider = provider

    class FakeDb:
        def commit(self):
            return None

    HealthService._apply_model_health(
        FakeDb(),
        provider,
        provider_model,
        health_status="healthy",
        latency_ms=18,
        error_message=None,
    )

    assert provider_model.health_status == "healthy"
    assert provider_model.circuit_state == "closed"
    assert provider_model.content_integrity_status == "blocked"
    assert provider_model.content_probe_failure_count == 3
    assert provider.content_integrity_status == "blocked"
    assert provider.circuit_state == "closed"


def test_content_probe_isolation_does_not_modify_circuit_state() -> None:
    provider = Provider(
        id=22,
        name="内容隔离不熔断提供商",
        base_url="https://example.com/v1",
        api_key="sk-test",
        enabled=True,
        health_status="healthy",
        circuit_state="closed",
        success_count=0,
        failure_count=0,
        content_integrity_status="unknown",
        content_integrity_score=80,
    )
    provider_model = ProviderModel(
        id=221,
        provider_id=22,
        model_name="内容隔离不熔断模型",
        enabled=True,
        health_status="healthy",
        circuit_state="closed",
        content_integrity_status="unknown",
        content_probe_failure_count=2,
        content_probe_last_failed_at=now_beijing(),
    )
    provider.provider_models = [provider_model]
    provider_model.provider = provider

    class FakeDb:
        def commit(self):
            return None

    ContentGuardProbeService.apply_content_probe_health(
        FakeDb(),
        provider,
        provider_model,
        content_guard_result={
            "content_guard_result": ContentGuardService.RESULT_BLOCK,
            "content_guard_reason": "测试内容风险",
            "content_guard_categories_json": '["content_trust_probe_incomplete"]',
        },
        endpoint_results=None,
        detection_source="automatic_trust_probe",
    )

    assert provider_model.content_integrity_status == "blocked"
    assert provider.content_integrity_status == "blocked"
    assert provider_model.circuit_state == "closed"
    assert provider.circuit_state == "closed"


def test_provider_model_serialization_returns_effective_health_fields() -> None:
    provider_model = ProviderModel(
        id=23,
        provider_id=2,
        model_name="有效可用状态模型",
        enabled=True,
        health_status="healthy",
        circuit_state="closed",
        content_integrity_status="unknown",
        created_at=now_beijing(),
        updated_at=now_beijing(),
    )

    payload = ProviderService.provider_model_to_dict(provider_model)

    assert payload["db_health"] == "healthy"
    assert payload["runtime_health"] is None
    assert payload["effective_health"] == "healthy"
    assert payload["state_source"] == "db"
    assert "health_state_updated_at" in payload


def test_responses_success_error_null_is_not_upstream_error_payload() -> None:
    result = ContentGuardService._inspect_response_structure(
        {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "error": None,
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "AOTU_CONTENT_GUARD_OK",
                        }
                    ],
                }
            ],
        },
        endpoint_path="/responses",
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_responses_meaningful_error_payload_is_blocked() -> None:
    result = ContentGuardService._inspect_response_structure(
        {
            "id": "resp_test",
            "object": "response",
            "status": "failed",
            "error": {"message": "model unavailable", "code": "model_not_found"},
            "output": [],
        },
        endpoint_path="/responses",
    )

    assert result.result == ContentGuardService.RESULT_BLOCK
    assert "upstream_error_payload" in result.categories


def test_enhanced_detection_catches_obfuscated_illegal_redirect() -> None:
    result = ContentGuardService.inspect_response_text(
        "赌 博 开 户 充 值 找 客 服",
        request_payload={"messages": [{"content": "不要广告和非法导流"}]},
        rules=[],
    )

    assert result.result == ContentGuardService.RESULT_BLOCK
    assert "illegal_content" in result.categories


def test_enhanced_detection_can_be_disabled() -> None:
    result = ContentGuardService.inspect_response_text(
        "赌 博 开 户 充 值 找 客 服",
        rules=[],
        enhanced_detection_enabled=False,
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_negated_ad_phrase_is_not_flagged() -> None:
    result = ContentGuardService.inspect_response_text(
        "这是正常的技术说明，没有推广链接。",
        request_payload={"messages": [{"content": "不要广告和非法导流"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_public_safety_anti_fraud_context_is_not_flagged() -> None:
    result = ContentGuardService.inspect_response_text(
        "防范诈骗需要核验来源、拒绝陌生转账，并保留沟通证据。",
        request_payload={"messages": [{"role": "user", "content": "如何防范诈骗"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_redirect_safety_context_does_not_trigger_ad_rules() -> None:
    result = ContentGuardService.inspect_response_text(
        "识别优惠码骗局时，不要添加微信客服，不要扫码进群，也不要点击推广链接。",
        request_payload={"messages": [{"role": "user", "content": "说明如何防范广告导流"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_chinese_sentence_period_does_not_create_fake_domain() -> None:
    assert ContentGuardService.extract_domains("不要把 API Key 写入前端代码。定期轮换密钥。") == set()


def test_positive_redirect_intent_still_triggers_ad_rules() -> None:
    result = ContentGuardService.inspect_response_text(
        "立即注册并扫码领取优惠，添加客服加入社群即可获得返佣。",
        request_payload={"messages": [{"role": "user", "content": "请回答接口使用建议，不要广告"}]},
    )

    assert result.result in {ContentGuardService.RESULT_REVIEW, ContentGuardService.RESULT_BLOCK}
    assert any(
        category in result.categories
        for category in {"advertising_or_promotion", "contact_or_offplatform_redirect", "qr_code_redirect"}
    )


def test_positive_custom_score_is_not_duplicated_by_enhanced_detection() -> None:
    result = ContentGuardService.inspect_response_text(
        "低风险证据",
        rules=[
            {
                "id": "positive_score",
                "name": "正向评分",
                "category": "custom",
                "score_delta": 20,
                "patterns": ["低风险证据"],
            }
        ],
    )

    assert result.score_delta == 20


def test_pollution_scenarios_use_single_upstream_request() -> None:
    calls = 0

    async def fake_send(provider, provider_model, *, endpoint_path, payload, endpoint_label):
        nonlocal calls
        calls += 1
        return (
            {
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "[[long_context_answer]]\n"
                                    "请求进入代理后先校验密钥。\n"
                                    "再选择可用提供商。\n"
                                    "随后转发上游模型。\n"
                                    "最后记录日志和用量。\n\n"
                                    "[[tool_context_answer]]\n"
                                    "healthy 表示该检查对象当前可正常响应。\n\n"
                                    "[[markdown_answer]]\n"
                                    "- 不要把 API Key 写入前端代码。\n"
                                    "- 定期轮换并保留最小权限。\n"
                                    "- 泄露后立即禁用并重建。\n\n"
                                    "[[citation_answer]]\n"
                                    "内容完整性检测用于确认模型输出没有被异常外链、广告或引流内容污染。参考：内部策略"
                                ),
                            }
                        ],
                    }
                ],
            },
            1000,
            200,
            [{"result": "fake_trace"}],
            None,
        )

    async def run_case() -> None:
        original = ContentGuardProbeService.send_content_probe_json
        ContentGuardProbeService.send_content_probe_json = staticmethod(fake_send)
        try:
            provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="http://example.test")
            provider_model = SimpleNamespace(id=1, model_name="测试模型", custom_model_name="test-model")
            result = await ContentGuardProbeService.probe_pollution_rules(provider, provider_model, endpoint_path="/responses")
        finally:
            ContentGuardProbeService.send_content_probe_json = original
        assert calls == 1
        assert result["success"] is True
        assert len(result.get("detections") or []) == 4

    asyncio.run(run_case())


def test_fixed_answer_and_pollution_rules_use_single_upstream_request() -> None:
    calls = 0

    async def fake_send(provider, provider_model, *, endpoint_path, payload, endpoint_label):
        nonlocal calls
        calls += 1
        return (
            {
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "[[fixed_answer]]\n"
                                    "AOTU_CONTENT_GUARD_OK\n\n"
                                    "[[long_context_answer]]\n"
                                    "请求进入代理后先校验密钥。\n"
                                    "再选择可用提供商。\n"
                                    "随后转发上游模型。\n"
                                    "最后记录日志和用量。\n\n"
                                    "[[tool_context_answer]]\n"
                                    "healthy 表示该检查对象当前可正常响应。\n\n"
                                    "[[markdown_answer]]\n"
                                    "- 不要把 API Key 写入前端代码。\n"
                                    "- 定期轮换并保留最小权限。\n"
                                    "- 泄露后立即禁用并重建。\n\n"
                                    "[[citation_answer]]\n"
                                    "内容完整性检测用于确认模型输出没有被异常外链、广告或引流内容污染。参考：内部策略"
                                ),
                            }
                        ],
                    }
                ],
            },
            1000,
            200,
            [{"result": "fake_trace"}],
            None,
        )

    async def run_case() -> None:
        original = ContentGuardProbeService.send_content_probe_json
        ContentGuardProbeService.send_content_probe_json = staticmethod(fake_send)
        try:
            provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="http://example.test")
            provider_model = SimpleNamespace(id=1, model_name="测试模型", custom_model_name="test-model")
            result = await ContentGuardProbeService.probe_fixed_answer_and_pollution_rules(
                provider,
                provider_model,
                endpoint_path="/responses",
            )
        finally:
            ContentGuardProbeService.send_content_probe_json = original
        assert calls == 1
        assert result["fixed_answer"]["success"] is True
        assert result["pollution_rules"]["success"] is True
        assert result["fixed_answer"].get("raw_provider_response")
        assert result["pollution_rules"].get("raw_provider_response")

    asyncio.run(run_case())

from app.utils.timezone import now_beijing
