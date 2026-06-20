import asyncio
from decimal import Decimal
from types import SimpleNamespace

from app.services.proxy_service import PreparedUpstreamRequest, ProxyService
from app.services.native_protocol_adapter import NativeProtocolAdapter


class _FakeSession:
    def __init__(self, opened):
        self.opened = opened

    def __enter__(self):
        self.opened.append(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def scalar(self, _statement):
        return None


def test_preflight_cost_estimation_opens_session_when_db_is_none(monkeypatch):
    opened_sessions = []

    monkeypatch.setattr("app.services.proxy_service.SessionLocal", lambda: _FakeSession(opened_sessions))
    monkeypatch.setattr(
        ProxyService,
        "_estimate_request_tokens_for_precheck",
        staticmethod(lambda *args, **kwargs: (10, None)),
    )

    provider_model = SimpleNamespace(
        model_name="测试模型",
        max_output_tokens=0,
        input_price_per_1k=Decimal("0.2"),
        output_price_per_1k=Decimal("0.4"),
        price_multiplier=Decimal("1"),
    )

    estimated_cost, input_tokens, output_tokens = ProxyService._estimate_preflight_request_cost(
        None,
        provider_model=provider_model,
        payload={"max_output_tokens": 5},
        request_path="/v1/responses",
        model_name="测试模型",
    )

    assert opened_sessions == [True]
    assert estimated_cost == Decimal("0.004")
    assert input_tokens == 10
    assert output_tokens == 5


def test_stream_endpoint_fallback_accepts_extra_headers(monkeypatch):
    captured_headers = {}

    class FakeStreamResponse:
        pass

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse(), SimpleNamespace(request_payload={})

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_stream_prepared_request(_provider, *, headers, **_kwargs):
        captured_headers.update(headers)
        return FakeStreamContext()

    async def fake_raise_for_status(_response):
        return None

    monkeypatch.setattr(ProxyService, "_stream_prepared_request", staticmethod(fake_stream_prepared_request))
    monkeypatch.setattr(ProxyService, "_raise_stream_response_for_status", staticmethod(fake_raise_for_status))

    provider = SimpleNamespace(api_key="sk-test", base_url="https://example.com/v1")
    provider_model = SimpleNamespace(model_name="测试模型")

    asyncio.run(
        ProxyService._open_stream_with_endpoint_fallback(
            provider,
            provider_model,
            "/responses",
            {"model": "测试模型", "input": "ping", "stream": True},
            started=0.0,
            extra_headers={"Accept-Encoding": "identity"},
        )
    )

    assert captured_headers["Authorization"] == "Bearer sk-test"
    assert captured_headers["Accept-Encoding"] == "identity"


def test_json_endpoint_fallback_accepts_extra_headers(monkeypatch):
    captured_headers = {}

    async def fake_send_json(_provider, *, headers, **_kwargs):
        captured_headers.update(headers)
        return {"id": "resp_1"}, "upstream-request-id"

    monkeypatch.setattr(ProxyService, "_send_prepared_json", staticmethod(fake_send_json))

    provider = SimpleNamespace(api_key="sk-test", base_url="https://example.com/v1")
    provider_model = SimpleNamespace(model_name="测试模型")

    response_json, upstream_request_id, trace = asyncio.run(
        ProxyService._forward_json_with_endpoint_fallback(
            provider,
            provider_model,
            "/responses",
            {"model": "测试模型", "input": "ping"},
            started=0.0,
            setting=SimpleNamespace(),
            extra_headers={"Accept-Encoding": "identity"},
        )
    )

    assert response_json == {"id": "resp_1"}
    assert upstream_request_id == "upstream-request-id"
    assert trace == []
    assert captured_headers["Authorization"] == "Bearer sk-test"
    assert captured_headers["Accept-Encoding"] == "identity"


def test_model_name_defaults_route_openai_domestic_gemini_and_claude_protocols():
    from app.services.provider_service import ProviderService

    assert ProviderService.default_supports_for_model_name("gpt-4.1") == ("both", True, True)
    assert ProviderService.default_supports_for_model_name("qwen-plus") == ("chat_completions", True, False)
    assert ProviderService.default_supports_for_model_name("deepseek-chat") == ("chat_completions", True, False)
    assert ProviderService.default_supports_for_model_name("gemini-2.5-pro") == ("gemini", False, False)
    assert ProviderService.default_supports_for_model_name("claude-3-5-sonnet-latest") == ("claude_messages", False, False)


def test_prepare_gemini_native_request_uses_generate_content_and_inline_data():
    provider = SimpleNamespace(
        protocol_type="gemini",
        api_key="gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    provider_model = SimpleNamespace(
        model_name="gemini-2.5-pro",
        protocol_type="gemini",
        supports_chat_completions=False,
        supports_responses=False,
    )

    prepared = ProxyService._prepare_upstream_request(
        provider,
        provider_model=provider_model,
        endpoint_path="/chat/completions",
        payload={
            "model": "gemini-2.5-pro",
            "messages": [
                {"role": "system", "content": "只返回简短答案"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "识别图片"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    ],
                },
            ],
            "max_tokens": 12,
        },
    )

    assert prepared.request_path == "/models/gemini-2.5-pro:generateContent"
    assert prepared.upstream_protocol_type == "gemini"
    assert prepared.public_endpoint_path == "/chat/completions"
    assert prepared.request_payload["generationConfig"]["maxOutputTokens"] == 12
    assert prepared.request_payload["systemInstruction"]["parts"][0]["text"] == "只返回简短答案"
    assert prepared.request_payload["contents"][0]["parts"][1]["inlineData"] == {
        "mimeType": "image/png",
        "data": "QUJD",
    }
    assert NativeProtocolAdapter.headers("gemini", "gemini-key") == {"x-goog-api-key": "gemini-key"}


def test_gemini_native_request_url_adds_default_v1beta_only_when_base_url_has_no_version():
    prepared = PreparedUpstreamRequest(
        request_path="/models/gemini-2.5-pro:generateContent",
        request_payload={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
        upstream_protocol_type="gemini",
    )

    root_provider = SimpleNamespace(base_url="https://generativelanguage.googleapis.com")
    versioned_provider = SimpleNamespace(base_url="https://generativelanguage.googleapis.com/v1beta")
    gateway_provider = SimpleNamespace(base_url="https://gateway.example.com/google")

    assert (
        ProxyService._prepared_request_url(root_provider, prepared)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    )
    assert (
        ProxyService._prepared_request_url(versioned_provider, prepared)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    )
    assert (
        ProxyService._prepared_request_url(gateway_provider, prepared)
        == "https://gateway.example.com/google/v1beta/models/gemini-2.5-pro:generateContent"
    )


def test_prepare_external_gemini_native_request_preserves_payload_and_response():
    provider = SimpleNamespace(
        protocol_type="gemini",
        api_key="gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    provider_model = SimpleNamespace(
        model_name="平台Gemini模型ID",
        upstream_model_name="gemini-2.5-pro",
        protocol_type="gemini",
        supports_chat_completions=False,
        supports_responses=False,
    )
    native_payload = {
        "model": "平台Gemini模型ID",
        "contents": [{"role": "user", "parts": [{"text": "你好"}]}],
        "generationConfig": {"temperature": 0.2},
        "stream": True,
    }

    prepared = ProxyService._prepare_upstream_request(
        provider,
        provider_model=provider_model,
        endpoint_path="/native/gemini",
        payload=native_payload,
        preserve_native_payload=True,
        preserve_native_response=True,
    )

    assert prepared.request_path == "/models/gemini-2.5-pro:streamGenerateContent?alt=sse"
    assert prepared.upstream_protocol_type == "gemini"
    assert prepared.preserve_native_response is True
    assert prepared.request_payload == {
        "contents": [{"role": "user", "parts": [{"text": "你好"}]}],
        "generationConfig": {"temperature": 0.2},
    }


def test_prepare_external_claude_native_request_preserves_payload_and_response():
    provider = SimpleNamespace(
        protocol_type="claude_messages",
        api_key="claude-key",
        base_url="https://api.anthropic.com",
    )
    provider_model = SimpleNamespace(
        model_name="平台Claude模型ID",
        upstream_model_name="claude-3-5-sonnet-latest",
        protocol_type="claude_messages",
        supports_chat_completions=False,
        supports_responses=False,
    )
    native_payload = {
        "model": "平台Claude模型ID",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 32,
        "stream": False,
    }

    prepared = ProxyService._prepare_upstream_request(
        provider,
        provider_model=provider_model,
        endpoint_path="/native/claude_messages",
        payload=native_payload,
        preserve_native_payload=True,
        preserve_native_response=True,
    )

    assert prepared.request_path == "/v1/messages"
    assert prepared.upstream_protocol_type == "claude_messages"
    assert prepared.preserve_native_response is True
    assert prepared.request_payload == {
        "model": "claude-3-5-sonnet-latest",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 32,
    }


def test_native_usage_and_display_text_are_extracted_from_original_responses():
    gemini_usage = ProxyService._extract_usage_info(
        {
            "candidates": [{"content": {"parts": [{"text": "Gemini 原生响应"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5, "totalTokenCount": 8},
        }
    )
    claude_usage = ProxyService._extract_usage_info(
        {
            "content": [{"type": "text", "text": "Claude 原生响应"}],
            "usage": {"input_tokens": 4, "output_tokens": 6},
        }
    )

    assert gemini_usage["prompt_tokens"] == 3
    assert gemini_usage["completion_tokens"] == 5
    assert gemini_usage["total_tokens"] == 8
    assert claude_usage["prompt_tokens"] == 4
    assert claude_usage["completion_tokens"] == 6
    assert ProxyService._extract_response_display_text(
        {"candidates": [{"content": {"parts": [{"text": "Gemini 原生响应"}]}}]},
        limit_bytes=1024,
    ) == "Gemini 原生响应"
    assert ProxyService._extract_response_display_text(
        {"content": [{"type": "text", "text": "Claude 原生响应"}]},
        limit_bytes=1024,
    ) == "Claude 原生响应"


def test_prepare_claude_native_request_uses_messages_api_headers_and_body():
    provider = SimpleNamespace(
        protocol_type="claude_messages",
        api_key="claude-key",
        base_url="https://api.anthropic.com",
    )
    provider_model = SimpleNamespace(
        model_name="claude-3-5-sonnet-latest",
        protocol_type="claude_messages",
        supports_chat_completions=False,
        supports_responses=False,
    )

    prepared = ProxyService._prepare_upstream_request(
        provider,
        provider_model=provider_model,
        endpoint_path="/responses",
        payload={
            "model": "claude-3-5-sonnet-latest",
            "input": [
                {"role": "system", "content": "只返回简短答案"},
                {"role": "user", "content": "说 pong"},
            ],
            "max_output_tokens": 8,
        },
    )

    assert prepared.request_path == "/v1/messages"
    assert prepared.upstream_protocol_type == "claude_messages"
    assert prepared.public_endpoint_path == "/responses"
    assert prepared.request_payload["model"] == "claude-3-5-sonnet-latest"
    assert prepared.request_payload["max_tokens"] == 8
    assert prepared.request_payload["system"] == "只返回简短答案"
    assert prepared.request_payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "说 pong"}]}]
    assert NativeProtocolAdapter.headers("claude_messages", "claude-key") == {
        "x-api-key": "claude-key",
        "anthropic-version": "2023-06-01",
    }


def test_claude_native_request_uses_provider_model_id_and_native_auth_headers():
    provider = SimpleNamespace(
        protocol_type="both",
        api_key="claude-key",
        base_url="https://api.anthropic.com",
    )
    provider_model = SimpleNamespace(
        model_name="claude-public",
        provider_model_id="claude-3-5-sonnet-latest",
        protocol_type="claude_messages",
        supports_chat_completions=False,
        supports_responses=False,
    )

    prepared = ProxyService._prepare_upstream_request(
        provider,
        provider_model=provider_model,
        endpoint_path="/chat/completions",
        payload={
            "model": "claude-public",
            "messages": [{"role": "user", "content": "说 pong"}],
            "max_tokens": 8,
        },
    )
    headers = ProxyService._build_upstream_headers(
        provider,
        prepared=prepared,
        extra_headers={"Authorization": "Bearer should-not-leak", "Accept-Encoding": "identity"},
    )

    assert prepared.request_payload["model"] == "claude-3-5-sonnet-latest"
    assert headers == {
        "x-api-key": "claude-key",
        "anthropic-version": "2023-06-01",
        "Accept-Encoding": "identity",
    }


def test_native_health_and_content_guard_payloads_use_provider_model_id():
    from app.services.content_guard_probe_service import ContentGuardProbeService
    from app.services.health_service import HealthService

    provider_model = SimpleNamespace(
        model_name="公开模型ID",
        provider_model_id="gemini-2.5-pro",
    )
    provider = SimpleNamespace(native_endpoint_path=None)

    prepared = HealthService._native_health_prepared_request(
        provider,
        provider_model,
        protocol_type="gemini",
        prompt="ping",
        max_tokens=8,
    )
    fixed_payload = ContentGuardProbeService.build_fixed_answer_payload(
        provider_model,
        endpoint_path="/native/gemini",
    )
    vision_payload = ContentGuardProbeService.build_vision_payload(
        provider_model,
        endpoint_path="/native/gemini",
    )

    assert prepared.request_path == "/models/gemini-2.5-pro:generateContent"
    assert prepared.request_payload["contents"][0]["parts"][0]["text"] == "ping"
    assert fixed_payload["contents"][0]["parts"][0]["text"]
    assert "model" not in fixed_payload
    assert vision_payload["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/png"


def test_native_health_log_protocol_type_is_recorded():
    from app.services.health_service import HealthService

    captured = []

    class FakeHealthLogRecorder:
        @staticmethod
        def record_probe(_db, **kwargs):
            captured.append(kwargs)

    monkeypatch = __import__("pytest").MonkeyPatch()
    try:
        monkeypatch.setattr("app.services.health_service.HealthLogRecorder", FakeHealthLogRecorder)
        HealthService._record_run_results(
            SimpleNamespace(commit=lambda: None),
            run_id="run-native",
            provider_results=[
                {
                    "provider_id": 1,
                    "model_results": [
                        {
                            "model_name": "gemini-alias",
                            "endpoint_results": [
                                {
                                    "provider_model_id": 2,
                                    "endpoint_path": "/models/gemini-2.5-pro:generateContent",
                                    "protocol_type": "gemini",
                                    "endpoint_label": "Gemini generateContent",
                                    "success": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    finally:
        monkeypatch.undo()

    assert captured[0]["protocol_type"] == "gemini"


def test_gemini_native_auth_headers_ignore_openai_bearer_overlay():
    provider = SimpleNamespace(
        protocol_type="both",
        api_key="gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    prepared = PreparedUpstreamRequest(
        request_path="/models/gemini-2.5-pro:generateContent",
        request_payload={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
        upstream_protocol_type="gemini",
    )

    headers = ProxyService._build_upstream_headers(
        provider,
        prepared=prepared,
        extra_headers={"Authorization": "Bearer should-not-leak", "Accept-Encoding": "identity"},
    )

    assert headers == {"x-goog-api-key": "gemini-key", "Accept-Encoding": "identity"}


def test_native_stream_chunks_are_converted_to_openai_chat_sse():
    state = {}
    gemini_chunks = NativeProtocolAdapter.native_stream_chunk_to_chat_chunks(
        "gemini",
        b'data: {"candidates":[{"content":{"parts":[{"text":"pong"}]},"finishReason":"STOP"}]}\n\n',
        requested_model="gemini-2.5-pro",
        state=state,
    )
    claude_chunks = NativeProtocolAdapter.native_stream_chunk_to_chat_chunks(
        "claude_messages",
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"pong"}}\n\n',
        requested_model="claude-3-5-sonnet-latest",
        state={},
    )

    assert b'"object":"chat.completion.chunk"' in gemini_chunks[0]
    assert b'"content":"pong"' in gemini_chunks[0]
    assert b'"finish_reason":"stop"' in gemini_chunks[0]
    assert b'"object":"chat.completion.chunk"' in claude_chunks[0]
    assert b'"content":"pong"' in claude_chunks[0]


def test_native_usage_mapping_preserves_gemini_and_claude_token_fields():
    from app.services.log_service import LogService

    gemini_payload = NativeProtocolAdapter.native_response_to_openai(
        "gemini",
        "/chat/completions",
        {
            "candidates": [{"content": {"parts": [{"text": "pong"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 442,
                "cachedContentTokenCount": 30,
                "candidatesTokenCount": 212,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 689,
            },
        },
        requested_model="gemini-2.5-pro",
    )
    claude_payload = NativeProtocolAdapter.native_response_to_openai(
        "claude_messages",
        "/chat/completions",
        {
            "content": [{"type": "text", "text": "pong"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 442,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "output_tokens": 212,
            },
        },
        requested_model="claude-3-5-sonnet-latest",
    )

    gemini_usage = gemini_payload["usage"]
    claude_usage = claude_payload["usage"]
    assert gemini_usage["usage_schema"] == "gemini_usage_metadata"
    assert gemini_usage["prompt_tokens"] == 442
    assert gemini_usage["completion_tokens"] == 212
    assert gemini_usage["cache_read_tokens"] == 30
    assert gemini_usage["completion_tokens_details"]["reasoning_tokens"] == 5
    assert gemini_usage["native_usage"]["usageMetadata"]["promptTokenCount"] == 442
    assert LogService.extract_usage_token_counts(gemini_usage) == {
        "prompt_tokens": 442,
        "completion_tokens": 212,
        "total_tokens": 689,
    }
    assert claude_usage["usage_schema"] == "claude_messages_usage"
    assert claude_usage["prompt_tokens"] == 472
    assert claude_usage["completion_tokens"] == 212
    assert claude_usage["cache_read_tokens"] == 10
    assert claude_usage["cache_write_tokens"] == 20
    assert claude_usage["native_usage"]["usage"]["cache_creation_input_tokens"] == 20
    assert LogService.extract_cache_tokens({"usage": claude_usage}) == (10, 20)


def test_claude_native_payload_preserves_explicit_cache_control():
    payload = NativeProtocolAdapter.openai_to_native_payload(
        "claude_messages",
        "/chat/completions",
        {
            "model": "claude-3-5-sonnet-latest",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "稳定系统提示",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "稳定上下文",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": "本轮问题"},
                    ],
                },
            ],
        },
    )

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload


def test_claude_native_payload_adds_default_automatic_cache_control():
    payload = NativeProtocolAdapter.openai_to_native_payload(
        "claude_messages",
        "/chat/completions",
        {
            "model": "claude-3-5-sonnet-latest",
            "messages": [{"role": "user", "content": "长上下文问题"}],
        },
    )

    assert payload["cache_control"] == {"type": "ephemeral"}


def test_gemini_native_payload_preserves_cached_content_reference():
    payload = NativeProtocolAdapter.openai_to_native_payload(
        "gemini",
        "/chat/completions",
        {
            "model": "gemini-2.5-pro",
            "cached_content": "cachedContents/abc123",
            "messages": [{"role": "user", "content": "继续基于缓存上下文回答"}],
        },
    )

    assert payload["cachedContent"] == "cachedContents/abc123"


def test_log_service_extracts_openai_compatible_domestic_cache_fields():
    from app.services.log_service import LogService

    qwen_usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "input_tokens_details": {
            "cached_tokens": 800,
            "cache_creation_tokens": 200,
        },
    }
    deepseek_usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 100,
        "total_tokens": 1300,
        "prompt_cache_hit_tokens": 900,
    }
    glm_usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 100,
        "total_tokens": 1300,
        "cached_tokens": 850,
    }

    assert LogService.extract_cache_tokens({"usage": qwen_usage}) == (800, 200)
    assert LogService.extract_cache_tokens({"usage": deepseek_usage}) == (900, None)
    assert LogService.extract_cache_tokens({"usage": glm_usage}) == (850, None)


def test_log_service_extracts_raw_gemini_usage_metadata():
    from app.services.log_service import LogService

    usage = LogService.extract_usage_payload(
        {
            "usageMetadata": {
                "promptTokenCount": 442,
                "cachedContentTokenCount": 30,
                "candidatesTokenCount": 212,
                "totalTokenCount": 684,
            }
        }
    )

    assert LogService.extract_usage_token_counts(usage) == {
        "prompt_tokens": 442,
        "completion_tokens": 212,
        "total_tokens": 684,
    }
    assert LogService.extract_cache_tokens({"usage": usage}) == (30, None)


def test_claude_native_stream_usage_is_merged_for_logging():
    state = {}
    NativeProtocolAdapter.native_stream_chunk_to_chat_chunks(
        "claude_messages",
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":442,"cache_read_input_tokens":10,"cache_creation_input_tokens":20,"output_tokens":0}}}\n\n',
        requested_model="claude-3-5-sonnet-latest",
        state=state,
    )
    chunks = NativeProtocolAdapter.native_stream_chunk_to_chat_chunks(
        "claude_messages",
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":212}}\n\n',
        requested_model="claude-3-5-sonnet-latest",
        state=state,
    )

    assert b'"prompt_tokens":472' in chunks[0]
    assert b'"completion_tokens":212' in chunks[0]
    assert b'"total_tokens":684' in chunks[0]
    assert b'"cache_read_tokens":10' in chunks[0]
    assert b'"cache_write_tokens":20' in chunks[0]
    assert b'"usage_schema":"claude_messages_usage"' in chunks[0]


def test_native_request_path_supports_custom_gateway_templates():
    assert NativeProtocolAdapter.request_path(
        "gemini",
        "gemini-2.5-pro",
        endpoint_path_template="/proxy/google/{model}:{action}",
    ) == "/proxy/google/gemini-2.5-pro:generateContent"
    assert NativeProtocolAdapter.request_path(
        "gemini",
        "gemini-2.5-pro",
        stream=True,
        endpoint_path_template="/proxy/google/{model}:{action}",
    ) == "/proxy/google/gemini-2.5-pro:streamGenerateContent?alt=sse"
    assert NativeProtocolAdapter.request_path(
        "claude_messages",
        "claude-3-5-sonnet-latest",
        endpoint_path_template="anthropic/messages",
    ) == "/anthropic/messages"


def test_responses_to_chat_fallback_maps_text_format_and_stream_usage():
    payload = ProxyService._build_chat_payload_from_responses_payload(
        {
            "model": "gpt-4o",
            "input": "return json",
            "stream": True,
            "stream_options": {"existing": "value"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    "strict": True,
                }
            },
        }
    )

    assert payload["stream_options"] == {"existing": "value", "include_usage": True}
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "strict": True,
        },
    }


def test_responses_to_chat_fallback_rejects_unmapped_text_options():
    safety = ProxyService._assess_responses_to_chat_conversion_safety(
        {
            "model": "gpt-4o",
            "input": "ping",
            "text": {"format": {"type": "text"}, "verbosity": "high"},
        }
    )

    assert safety.safe is False
    assert "text" in (safety.unsafe_fields or [])
