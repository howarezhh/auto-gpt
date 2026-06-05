from __future__ import annotations

from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _responses_tool_payload(model_name: str) -> dict:
    return {
        "model": model_name,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "调用 get_weather 工具。"}],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {"type": "function", "name": "get_weather"},
    }


def _responses_vision_payload(model_name: str) -> dict:
    return {
        "model": model_name,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请描述这张图片。"},
                    {"type": "input_image", "image_url": "https://example.com/cat.png"},
                ],
            }
        ],
    }


def _assert_native_responses_support() -> None:
    native_cases = (
        "qwen-plus",
        "qwen2.5-vl-72b-instruct",
        "doubao-1.5-pro-32k",
        "doubao-vision-pro-32k",
    )
    for model_name in native_cases:
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(inferred["supports_responses"] is True, f"{model_name} should default to native responses support: {inferred}")


def _assert_chat_only_family_fallbacks() -> None:
    tool_only_cases = ("deepseek-chat", "glm-4.5", "mimo-v2.5", "kimi-k2-0711-preview")
    for model_name in tool_only_cases:
        prepared = ProxyService._build_endpoint_fallback_request(
            requested_endpoint_path="/responses",
            failed_request_path="/responses",
            payload=_responses_tool_payload(model_name),
        )
        _assert(prepared.request_path == "/chat/completions", f"{model_name} should fallback responses->chat: {prepared}")
        _assert(
            prepared.request_payload["tools"][0]["function"]["name"] == "get_weather",
            f"{model_name} tool definition should stay lossless: {prepared.request_payload}",
        )
        _assert(
            prepared.request_payload["tool_choice"]["function"]["name"] == "get_weather",
            f"{model_name} tool_choice should stay lossless: {prepared.request_payload}",
        )

    vision_cases = ("glm-4v", "mimo-v2.5", "kimi-k2-0711-preview")
    for model_name in vision_cases:
        prepared = ProxyService._build_endpoint_fallback_request(
            requested_endpoint_path="/responses",
            failed_request_path="/responses",
            payload=_responses_vision_payload(model_name),
        )
        _assert(prepared.request_path == "/chat/completions", f"{model_name} vision payload should fallback responses->chat: {prepared}")
        content = prepared.request_payload["messages"][0]["content"]
        _assert(isinstance(content, list) and len(content) == 2, f"{model_name} vision content shape mismatch: {prepared.request_payload}")
        _assert(content[1]["type"] == "image_url", f"{model_name} vision content type mismatch: {prepared.request_payload}")


def _assert_unsafe_responses_payloads_stay_blocked() -> None:
    unsafe_payload = {
        "model": "deepseek-chat",
        "input": "hello",
        "previous_response_id": "resp_123",
        "reasoning": {"effort": "high"},
    }
    safety = ProxyService._assess_responses_to_chat_conversion_safety(unsafe_payload)
    _assert(safety.safe is False, f"complex responses payload should stay blocked: {safety}")
    _assert(
        "previous_response_id" in (safety.unsafe_fields or []) and "reasoning" in (safety.unsafe_fields or []),
        f"unsafe fields should be surfaced clearly: {safety}",
    )


def main() -> None:
    _assert_native_responses_support()
    _assert_chat_only_family_fallbacks()
    _assert_unsafe_responses_payloads_stay_blocked()
    print("stage24 family support matrix regression check passed")


if __name__ == "__main__":
    main()
