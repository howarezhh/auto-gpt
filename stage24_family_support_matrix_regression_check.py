from __future__ import annotations

from app.services.provider_service import ProviderService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_native_responses_support() -> None:
    native_cases = (
        "deepseek-chat",
        "glm-4.5",
        "mimo-v2.5",
        "kimi-k2-0711-preview",
        "qwen-plus",
        "qwen2.5-vl-72b-instruct",
        "doubao-1.5-pro-32k",
        "doubao-vision-pro-32k",
    )
    for model_name in native_cases:
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(inferred["supports_responses"] is True, f"{model_name} should default to native responses support: {inferred}")


def _assert_unknown_models_default_to_both_protocols() -> None:
    unknown_cases = ("新中转未探测模型", "vendor-new-model-2026")
    for model_name in unknown_cases:
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(
            inferred["supports_chat_completions"] is True,
            f"{model_name} should default to chat/completions until endpoint probing says otherwise: {inferred}",
        )
        _assert(
            inferred["supports_responses"] is True,
            f"{model_name} should default to responses until endpoint probing says otherwise: {inferred}",
        )


def _assert_native_responses_families_keep_tools_and_vision() -> None:
    tool_cases = ("qwen-plus", "doubao-1.5-pro-32k")
    for model_name in tool_cases:
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(inferred["supports_responses"] is True, f"{model_name} should support native responses: {inferred}")
        _assert(inferred["supports_tools"] is True, f"{model_name} should support tools natively: {inferred}")

    vision_cases = ("qwen2.5-vl-72b-instruct", "doubao-vision-pro-32k")
    for model_name in vision_cases:
        inferred = ProviderService._infer_model_capabilities(model_name)
        _assert(inferred["supports_responses"] is True, f"{model_name} should support native responses: {inferred}")
        _assert(inferred["supports_vision"] is True, f"{model_name} should support vision natively: {inferred}")


def main() -> None:
    _assert_native_responses_support()
    _assert_unknown_models_default_to_both_protocols()
    _assert_native_responses_families_keep_tools_and_vision()
    print("stage24 family support matrix regression check passed")


if __name__ == "__main__":
    main()
