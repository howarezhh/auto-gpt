from __future__ import annotations

from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    capability_cases = {
        "deepseek-chat": {"tools": True, "vision": False, "responses": True},
        "qwen-plus": {"tools": True, "vision": False, "responses": True},
        "qwen2.5-vl-72b-instruct": {"tools": True, "vision": True, "responses": True},
        "glm-4.5": {"tools": True, "vision": False, "responses": True},
        "glm-4v": {"tools": True, "vision": True, "responses": True},
        "kimi-k2-0711-preview": {"tools": True, "vision": True, "responses": True},
        "doubao-1.5-pro-32k": {"tools": True, "vision": False, "responses": True},
        "doubao-vision-pro-32k": {"tools": True, "vision": True, "responses": True},
        "mimo-v2.5": {"tools": True, "vision": True, "responses": True},
    }
    for model_name, expected in capability_cases.items():
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(
            inferred["supports_tools"] is expected["tools"],
            f"{model_name} tools inference mismatch: {inferred}",
        )
        _assert(
            inferred["supports_vision"] is expected["vision"],
            f"{model_name} vision inference mismatch: {inferred}",
        )
        _assert(
            inferred["supports_responses"] is expected["responses"],
            f"{model_name} responses inference mismatch: {inferred}",
        )

    simple_responses_tool_payload = {
        "model": "deepseek-chat",
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
    require_chat, require_responses = ProxyService._route_endpoint_requirements("/responses", simple_responses_tool_payload)
    _assert(require_chat is False, f"responses route should not require chat candidates: {require_chat}")
    _assert(require_responses is True, f"responses route should require native responses: {require_responses}")

    simple_chat_tool_payload = {
        "model": "doubao-1.5-pro-32k",
        "messages": [{"role": "user", "content": "调用 get_weather 工具。"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }
    require_chat, require_responses = ProxyService._route_endpoint_requirements("/chat/completions", simple_chat_tool_payload)
    _assert(require_chat is True, f"chat route should require native chat/completions: {require_chat}")
    _assert(require_responses is False, f"chat route should not require responses candidates: {require_responses}")

    print("stage22 model family compatibility regression check passed")


if __name__ == "__main__":
    main()
