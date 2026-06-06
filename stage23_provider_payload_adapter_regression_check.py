from __future__ import annotations

from unittest.mock import patch

from app.models.provider import Provider
from app.services.proxy_service import ProxyService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _provider(*, name: str, base_url: str) -> Provider:
    return Provider(
        id=23,
        name=name,
        provider_type="openai_compatible",
        base_url=base_url,
        api_key="upstream-secret",
        enabled=True,
        priority=10,
        weight=100,
        timeout_ms=30000,
        max_retries=1,
    )


class _FakeImageResponse:
    def __init__(self, *, content: bytes, content_type: str) -> None:
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def get(self, url: str, timeout: int):
        self.calls.append((url, timeout))
        return _FakeImageResponse(content=b"abc", content_type="image/png")


def _kimi_remote_image_should_be_inlined() -> None:
    provider = _provider(name="stage23-kimi", base_url="https://api.moonshot.ai/v1")
    payload = {
        "model": "kimi-k2.5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请看图回答"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png", "detail": "high"}},
                ],
            }
        ],
    }
    fake_session = _FakeSession()
    with patch.object(ProxyService, "_get_thread_local_requests_session", return_value=fake_session):
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path="/chat/completions", payload=payload)
    converted_url = prepared.request_payload["messages"][0]["content"][1]["image_url"]["url"]
    _assert(
        converted_url == "data:image/png;base64,YWJj",
        f"kimi remote image url should be converted to data url: {prepared.request_payload}",
    )
    _assert(fake_session.calls == [("https://example.com/cat.png", 30)], f"unexpected download calls: {fake_session.calls}")


def _generic_provider_should_keep_remote_image_url() -> None:
    provider = _provider(name="stage23-qwen", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    payload = {
        "model": "qwen2.5-vl-72b-instruct",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": "https://example.com/dog.png"},
                ],
            }
        ],
    }
    prepared = ProxyService._prepare_upstream_request(provider, endpoint_path="/responses", payload=payload)
    converted_url = prepared.request_payload["input"][0]["content"][1]["image_url"]
    _assert(
        converted_url == "https://example.com/dog.png",
        f"non-kimi provider should keep remote image url: {prepared.request_payload}",
    )


def _mimo_chat_should_use_official_output_token_field() -> None:
    provider = _provider(name="stage23-mimo", base_url="https://platform.xiaomimimo.com/v1")
    payload = {
        "model": "mimo-v2.5-pro",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    prepared = ProxyService._prepare_upstream_request(provider, endpoint_path="/chat/completions", payload=payload)
    _assert("max_tokens" not in prepared.request_payload, f"mimo payload should not keep max_tokens: {prepared.request_payload}")
    _assert(
        prepared.request_payload.get("max_completion_tokens") == 8,
        f"mimo payload should map max_tokens to max_completion_tokens: {prepared.request_payload}",
    )


def _generic_chat_should_keep_max_tokens() -> None:
    provider = _provider(name="stage23-qwen", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    payload = {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    prepared = ProxyService._prepare_upstream_request(provider, endpoint_path="/chat/completions", payload=payload)
    _assert(prepared.request_payload.get("max_tokens") == 8, f"generic chat payload should keep max_tokens: {prepared.request_payload}")
    _assert(
        "max_completion_tokens" not in prepared.request_payload,
        f"generic chat payload should not add max_completion_tokens: {prepared.request_payload}",
    )


def _route_endpoint_requirements_should_be_native_only() -> None:
    responses_payload = {
        "model": "glm-5.1",
        "input": "ping",
        "stream": True,
        "tools": [{"type": "function", "name": "noop", "parameters": {"type": "object", "properties": {}}}],
    }
    chat_payload = {
        "model": "glm-5.1",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
        "tools": [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}],
    }
    _assert(
        ProxyService._route_endpoint_requirements("/responses", responses_payload) == (False, True),
        "responses requests must require native responses support",
    )
    _assert(
        ProxyService._route_endpoint_requirements("/chat/completions", chat_payload) == (True, False),
        "chat requests must require native chat/completions support",
    )


def _native_endpoint_payloads_should_not_enable_adapter_flags() -> None:
    provider = _provider(name="stage23-qwen", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    chat_payload = {
        "model": "qwen-plus",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请看图并调用工具"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "stream": True,
    }
    responses_payload = {
        "model": "qwen-plus",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请看图并调用工具"},
                    {"type": "input_image", "image_url": "https://example.com/cat.png"},
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "stream": True,
    }
    prepared_chat = ProxyService._prepare_upstream_request(provider, endpoint_path="/chat/completions", payload=chat_payload)
    prepared_responses = ProxyService._prepare_upstream_request(provider, endpoint_path="/responses", payload=responses_payload)
    _assert(prepared_chat.request_path == "/chat/completions", f"chat endpoint should stay native: {prepared_chat}")
    _assert(prepared_responses.request_path == "/responses", f"responses endpoint should stay native: {prepared_responses}")
    _assert(not prepared_chat.adapt_responses_response_to_chat, f"chat should not enable responses adapter: {prepared_chat}")
    _assert(not prepared_responses.adapt_chat_response_to_responses, f"responses should not enable chat adapter: {prepared_responses}")
    _assert(prepared_chat.request_payload["tools"][0]["function"]["name"] == "get_weather", f"chat tools changed: {prepared_chat.request_payload}")
    _assert(prepared_responses.request_payload["tools"][0]["name"] == "get_weather", f"responses tools changed: {prepared_responses.request_payload}")


def main() -> None:
    _kimi_remote_image_should_be_inlined()
    _generic_provider_should_keep_remote_image_url()
    _mimo_chat_should_use_official_output_token_field()
    _generic_chat_should_keep_max_tokens()
    _route_endpoint_requirements_should_be_native_only()
    _native_endpoint_payloads_should_not_enable_adapter_flags()
    print("stage23 provider payload adapter regression check passed")


if __name__ == "__main__":
    main()
