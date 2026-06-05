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


def _tool_call_response_conversion_should_roundtrip() -> None:
    chat_response = {
        "id": "chatcmpl_stage23",
        "model": "deepseek-chat",
        "created": 1710000000,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_stage23",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }
    ProxyService._assert_chat_response_adapter_safe(chat_response)
    responses_payload = ProxyService._convert_chat_completion_to_responses_payload(
        chat_response,
        requested_model="deepseek-chat",
    )
    output = responses_payload["output"]
    _assert(isinstance(output, list) and output[-1]["type"] == "function_call", f"unexpected responses output: {output}")
    _assert(output[-1]["name"] == "get_weather", f"tool call name lost: {output}")
    _assert(output[-1]["arguments"] == '{"city":"Beijing"}', f"tool call args lost: {output}")

    ProxyService._assert_responses_response_adapter_safe(responses_payload)
    chat_payload = ProxyService._convert_responses_payload_to_chat_completion(
        responses_payload,
        requested_model="deepseek-chat",
    )
    message = chat_payload["choices"][0]["message"]
    _assert(message["tool_calls"][0]["function"]["name"] == "get_weather", f"chat tool call name lost: {chat_payload}")
    _assert(
        message["tool_calls"][0]["function"]["arguments"] == '{"city":"Beijing"}',
        f"chat tool call args lost: {chat_payload}",
    )
    _assert(chat_payload["choices"][0]["finish_reason"] == "tool_calls", f"finish reason lost: {chat_payload}")


def main() -> None:
    _kimi_remote_image_should_be_inlined()
    _generic_provider_should_keep_remote_image_url()
    _tool_call_response_conversion_should_roundtrip()
    print("stage23 provider payload adapter regression check passed")


if __name__ == "__main__":
    main()
