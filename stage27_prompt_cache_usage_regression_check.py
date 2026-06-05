from __future__ import annotations

from decimal import Decimal

from app.models.model_catalog import ModelCatalog
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.billing_service import BillingService
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.services.token_usage_service import TokenUsageService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _FakeDb:
    def __init__(self, *, provider_model: ProviderModel, catalog: ModelCatalog | None = None) -> None:
        self.provider_model = provider_model
        self.catalog = catalog

    def get(self, model_class, model_id):
        if model_class is ProviderModel and model_id == self.provider_model.id:
            return self.provider_model
        return None

    def scalar(self, _statement):
        return self.catalog


def _check_usage_extraction() -> None:
    openai_chat = {"usage": {"prompt_tokens_details": {"cached_tokens": 123}}}
    _assert(LogService.extract_cache_tokens(openai_chat) == (123, None), "OpenAI chat cached_tokens should map to cache read only")

    openai_responses = {"usage": {"input_tokens_details": {"cached_tokens": 234}}}
    _assert(LogService.extract_cache_tokens(openai_responses) == (234, None), "Responses cached_tokens should map to cache read only")

    deepseek = {"usage": {"prompt_cache_hit_tokens": 345, "prompt_cache_miss_tokens": 999}}
    _assert(LogService.extract_cache_tokens(deepseek) == (345, None), "DeepSeek cache hit should map to read and miss must not become write")

    anthropic = {"usage": {"input_tokens": 50, "cache_read_input_tokens": 456, "cache_creation_input_tokens": 78}}
    _assert(LogService.extract_cache_tokens(anthropic) == (456, 78), "Anthropic read/create fields should map exactly")
    _assert(
        TokenUsageService._extract_usage_from_response(anthropic)["prompt_tokens"] == 584,
        "Anthropic-style input_tokens should be normalized with cache read/write tokens for billing",
    )

    qwen = {"usage": {"prompt_tokens_details": {"cached_tokens": 12, "cache_creation_input_tokens": 34}}}
    _assert(LogService.extract_cache_tokens(qwen) == (12, 34), "Qwen OpenAI-compatible cache creation field should map to write")

    proxy_usage = ProxyService._extract_usage_info(anthropic)
    _assert(proxy_usage["prompt_tokens"] == 584, "Proxy usage extraction should use the same normalized cache-aware prompt tokens")


def _check_cache_write_billing() -> None:
    provider_model = ProviderModel(
        id=1,
        provider_id=1,
        model_name="stage27-cache-model",
        input_price_per_1k=Decimal("0.010000"),
        output_price_per_1k=Decimal("0.020000"),
        cache_price_per_1k=Decimal("0.002000"),
        price_multiplier=Decimal("1.0000"),
    )
    catalog = ModelCatalog(
        model_name="stage27-cache-model",
        pricing_mode="tiered",
        pricing_json={
            "tiers": [
                {
                    "tier_key": "default",
                    "tier_name": "默认档",
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                    "cache_price_per_1k": 0.002,
                    "cache_write_price_per_1k": 0.015,
                }
            ]
        },
    )
    log = RequestLog(
        log_type="chat",
        success=True,
        api_client_key_id=1,
        request_path="/v1/chat/completions",
        resolved_provider_model_id=1,
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        cache_read_tokens=200,
        cache_write_tokens=100,
    )

    result = BillingService.compute_log_cost(_FakeDb(provider_model=provider_model, catalog=catalog), log)
    _assert(log.channel_price_cache_write_per_1k == Decimal("0.015000000000"), "cache write price should be persisted on the log")
    _assert(result["prompt_cost"] == Decimal("0.008900"), f"prompt cost should include read and write cache costs: {result}")
    _assert(result["completion_cost"] == Decimal("0.010000"), f"completion cost mismatch: {result}")
    _assert(result["total_cost"] == Decimal("0.018900"), f"total cost mismatch: {result}")


def main() -> None:
    _check_usage_extraction()
    _check_cache_write_billing()
    print("stage27 prompt cache usage regression check passed")


if __name__ == "__main__":
    main()
