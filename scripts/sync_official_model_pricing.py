from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.main import init_database  # noqa: E402
from app.models.model_catalog import ModelCatalog  # noqa: E402
from app.services.model_catalog_service import ModelCatalogService  # noqa: E402
from app.services.model_pricing_service import ModelPricingService  # noqa: E402
from app.services.provider_service import ProviderService  # noqa: E402
from app.utils.decimal_utils import PRICE_QUANT, to_price_decimal  # noqa: E402
from app.utils.timezone import now_beijing  # noqa: E402


DEFAULT_BILLING_CURRENCY = "USD"
CNY_EXCHANGE_SNAPSHOT = {
    "exchange_rate_to_usd": "0.1471713577725329730343978813",
    "exchange_rate_source": "official_pricing_import_snapshot",
    "exchange_rate_at": "2026-06-01T00:00:00+08:00",
    "exchange_rate_version": "official_pricing_2026_06_01_usd_cny_6_7948",
    "rounding_strategy": "ROUND_HALF_UP",
}
PRICE_FIELDS = {
    "pricing_mode",
    "pricing_json",
    "input_price_per_1k",
    "output_price_per_1k",
    "cache_price_per_1k",
    "cache_write_price_per_1k",
    "source_currency",
    "billing_currency",
    "source_input_price_per_1k",
    "source_output_price_per_1k",
    "source_cache_price_per_1k",
    "source_cache_write_price_per_1k",
    "exchange_rate_to_billing_currency",
    "exchange_rate_source",
    "exchange_rate_at",
    "exchange_rate_version",
    "rounding_strategy",
}
UPDATED_AT = "2026-06-05T12:30:00+08:00"
CHINA_PRICE_NOTE = (
    "官方原价单位为人民币；系统保存人民币原价和 2026-06-01 汇率快照，"
    "当前默认账户币种为 USD 时按快照换算扣费。"
)


def usd_per_1m(value: str | int | float | Decimal | None) -> Decimal | None:
    if value in (None, ""):
        return None
    return to_price_decimal(Decimal(str(value)) / Decimal("1000"))


def cny_per_1m_to_source_per_1k(value: str | int | float | Decimal | None) -> Decimal | None:
    if value in (None, ""):
        return None
    return to_price_decimal(Decimal(str(value)) / Decimal("1000"))


def build_fixed_payload(
    *,
    input_usd_per_1m: str | int | float | Decimal | None,
    output_usd_per_1m: str | int | float | Decimal | None,
    cache_usd_per_1m: str | int | float | Decimal | None,
    source_label: str,
    source_url: str,
    note: str | None = None,
) -> dict:
    return {
        "pricing_mode": ModelPricingService.PRICING_MODE_FIXED,
        "pricing_json": {
            "source_label": source_label,
            "source_url": source_url,
            "note": note,
            "updated_at": UPDATED_AT,
            "source_currency": "USD",
            "billing_currency": DEFAULT_BILLING_CURRENCY,
            "rounding_strategy": "ROUND_HALF_UP",
            "source_input_price_per_1k": usd_per_1m(input_usd_per_1m),
            "source_output_price_per_1k": usd_per_1m(output_usd_per_1m),
            "source_cache_price_per_1k": usd_per_1m(cache_usd_per_1m),
            "tiers": [],
        },
        "input_price_per_1k": usd_per_1m(input_usd_per_1m),
        "output_price_per_1k": usd_per_1m(output_usd_per_1m),
        "cache_price_per_1k": usd_per_1m(cache_usd_per_1m),
        "cache_write_price_per_1k": None,
    }


def build_unpriced_payload(
    *,
    source_label: str,
    source_url: str,
    note: str,
) -> dict:
    return {
        "pricing_mode": ModelPricingService.PRICING_MODE_UNPRICED,
        "pricing_json": {
            "source_label": source_label,
            "source_url": source_url,
            "note": note,
            "updated_at": UPDATED_AT,
            "source_currency": "USD",
            "billing_currency": DEFAULT_BILLING_CURRENCY,
            "rounding_strategy": "ROUND_HALF_UP",
            "tiers": [],
        },
        "input_price_per_1k": None,
        "output_price_per_1k": None,
        "cache_price_per_1k": None,
        "cache_write_price_per_1k": None,
    }


def build_cny_tier(
    *,
    tier_key: str,
    tier_name: str,
    input_cny_per_1m: str | int | float | Decimal | None,
    output_cny_per_1m: str | int | float | Decimal | None,
    cache_cny_per_1m: str | int | float | Decimal | None,
    cache_write_cny_per_1m: str | int | float | Decimal | None = None,
    cache_storage_cny_per_1m: str | int | float | Decimal | None = None,
    min_prompt_tokens: int | None = None,
    max_prompt_tokens: int | None = None,
    min_completion_tokens: int | None = None,
    max_completion_tokens: int | None = None,
    source_note: str | None = None,
) -> dict:
    return {
        "tier_key": tier_key,
        "tier_name": tier_name,
        "min_prompt_tokens": min_prompt_tokens,
        "max_prompt_tokens": max_prompt_tokens,
        "min_completion_tokens": min_completion_tokens,
        "max_completion_tokens": max_completion_tokens,
        "source_input_price_per_1k": cny_per_1m_to_source_per_1k(input_cny_per_1m),
        "source_output_price_per_1k": cny_per_1m_to_source_per_1k(output_cny_per_1m),
        "source_cache_price_per_1k": cny_per_1m_to_source_per_1k(cache_cny_per_1m),
        "source_cache_write_price_per_1k": cny_per_1m_to_source_per_1k(cache_write_cny_per_1m),
        "source_cache_storage_price_per_1k": cny_per_1m_to_source_per_1k(cache_storage_cny_per_1m),
        "source_note": source_note,
    }


def build_tiered_payload(
    *,
    source_label: str,
    source_url: str,
    tiers: list[dict],
    note: str | None = None,
) -> dict:
    normalized = ModelPricingService.normalize_catalog_pricing(
        pricing_mode=ModelPricingService.PRICING_MODE_TIERED,
        pricing_json={
            "source_label": source_label,
            "source_url": source_url,
            "note": note,
            "updated_at": UPDATED_AT,
            "source_currency": "CNY",
            "billing_currency": DEFAULT_BILLING_CURRENCY,
            **CNY_EXCHANGE_SNAPSHOT,
            "tiers": tiers,
        },
        input_price_per_1k=None,
        output_price_per_1k=None,
        cache_price_per_1k=None,
        cache_write_price_per_1k=None,
    )
    return normalized


MODEL_SPECS = [
    {
        "model_name": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.14",
            cache_usd_per_1m="0.0028",
            output_usd_per_1m="0.28",
            source_label="DeepSeek 官方 API 价格",
            source_url="https://api-docs.deepseek.com/quick_start/pricing",
            note="官方价格单位为每百万 token；当前为 DeepSeek 2026-04-24 发布的最新公开 API 模型之一。",
        ),
        "remark": "DeepSeek 官方最新公开 API 模型，支持工具调用，当前不属于官方 Responses 原生模型。",
    },
    {
        "model_name": "deepseek-v4-pro",
        "display_name": "DeepSeek V4 Pro",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.435",
            cache_usd_per_1m="0.003625",
            output_usd_per_1m="0.87",
            source_label="DeepSeek 官方 API 价格",
            source_url="https://api-docs.deepseek.com/quick_start/pricing",
            note="DeepSeek 官方价格页注明：75% 优惠于北京时间 2026-05-31 23:59 结束后，API 价格正式调整为原价的 1/4；当前写入为 2026-06-01 后继续生效的官方价格。",
        ),
        "remark": "DeepSeek 官方最新公开 API 模型，支持工具调用，当前不属于官方 Responses 原生模型。",
    },
    {
        "model_name": "deepseek-chat",
        "display_name": "DeepSeek Chat（兼容别名）",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.14",
            cache_usd_per_1m="0.0028",
            output_usd_per_1m="0.28",
            source_label="DeepSeek 官方 API 价格",
            source_url="https://api-docs.deepseek.com/quick_start/pricing",
            note="DeepSeek 官方说明该模型名为兼容别名，对应 deepseek-v4-flash 的非思考模式，并将于 2026-07-24 停用。",
        ),
        "remark": "DeepSeek 官方兼容别名，对应 deepseek-v4-flash 非思考模式，将于 2026-07-24 停用。",
    },
    {
        "model_name": "deepseek-reasoner",
        "display_name": "DeepSeek Reasoner（兼容别名）",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.14",
            cache_usd_per_1m="0.0028",
            output_usd_per_1m="0.28",
            source_label="DeepSeek 官方 API 价格",
            source_url="https://api-docs.deepseek.com/quick_start/pricing",
            note="DeepSeek 官方说明该模型名为兼容别名，对应 deepseek-v4-flash 的思考模式，并将于 2026-07-24 停用。",
        ),
        "remark": "DeepSeek 官方兼容别名，对应 deepseek-v4-flash 思考模式，将于 2026-07-24 停用。",
    },
    {
        "model_name": "gpt-4o-mini",
        "display_name": "GPT-4o Mini",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 128_000,
        "max_output_tokens": 16_384,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.15",
            cache_usd_per_1m="0.075",
            output_usd_per_1m="0.60",
            source_label="OpenAI 官方 API 价格",
            source_url="https://platform.openai.com/docs/pricing",
        ),
        "remark": "OpenAI 官方美元价格。",
    },
    {
        "model_name": "gpt-5.2",
        "display_name": "GPT-5.2",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="1.75",
            cache_usd_per_1m="0.175",
            output_usd_per_1m="14.00",
            source_label="OpenAI 官方 API 价格",
            source_url="https://platform.openai.com/docs/pricing",
        ),
        "remark": "OpenAI 官方美元价格。",
    },
    {
        "model_name": "gpt-5.3-codex",
        "display_name": "GPT-5.3 Codex",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="1.75",
            cache_usd_per_1m="0.175",
            output_usd_per_1m="14.00",
            source_label="OpenAI 官方 API 价格",
            source_url="https://developers.openai.com/api/docs/models/gpt-5.3-codex",
            note="OpenAI 官方 GPT-5.3-Codex 模型页价格。",
        ),
        "remark": "OpenAI 官方 GPT-5.3-Codex 价格与能力。",
    },
    {
        "model_name": "gpt-5.3-codex-spark",
        "display_name": "GPT-5.3 Codex Spark",
        "supports_vision": False,
        "supports_tools": False,
        "supports_chat_completions": False,
        "supports_responses": False,
        "context_window_tokens": 128_000,
        "max_input_tokens": 128_000,
        "max_output_tokens": 128_000,
        "pricing": build_unpriced_payload(
            source_label="OpenAI 官方发布页",
            source_url="https://openai.com/index/introducing-gpt-5-3-codex-spark/",
            note="OpenAI 官方发布页公开 GPT-5.3-Codex-Spark 为 128k 上下文且 text-only；截至 2026-06-05，官方未公开独立 API 价格。",
        ),
        "remark": "OpenAI 官方发布页公开 128k 上下文且 text-only；官方未公开独立 API 价格，保持未定价。",
    },
    {
        "model_name": "gpt-5.4",
        "display_name": "GPT-5.4",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="2.50",
            cache_usd_per_1m="0.25",
            output_usd_per_1m="15.00",
            source_label="OpenAI 官方 API 价格",
            source_url="https://platform.openai.com/docs/pricing",
        ),
        "remark": "OpenAI 官方美元价格。",
    },
    {
        "model_name": "gpt-5.4-mini",
        "display_name": "GPT-5.4 Mini",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="0.75",
            cache_usd_per_1m="0.075",
            output_usd_per_1m="4.50",
            source_label="OpenAI 官方 API 价格",
            source_url="https://platform.openai.com/docs/pricing",
        ),
        "remark": "OpenAI 官方美元价格。",
    },
    {
        "model_name": "gpt-5.5",
        "display_name": "GPT-5.5",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "pricing": build_fixed_payload(
            input_usd_per_1m="5.00",
            cache_usd_per_1m="0.50",
            output_usd_per_1m="30.00",
            source_label="OpenAI 官方 API 价格",
            source_url="https://platform.openai.com/docs/pricing",
        ),
        "remark": "OpenAI 官方美元价格。",
    },
    {
        "model_name": "qwen3.7-max",
        "display_name": "通义千问 3.7 Max",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "pricing": build_tiered_payload(
            source_label="阿里云百炼官方价格（人民币原价）",
            source_url="https://help.aliyun.com/zh/model-studio/model-pricing",
            note=f"{CHINA_PRICE_NOTE} 默认缓存价按显式缓存命中口径保存；显式缓存写入按输入价 125% 记录。",
            tiers=[
                build_cny_tier(
                    tier_key="0_1m",
                    tier_name="0-1M 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=1_000_000,
                    input_cny_per_1m="12",
                    output_cny_per_1m="36",
                    cache_cny_per_1m="1.2",
                    cache_write_cny_per_1m="15",
                    source_note="阿里云百炼价格页注明上下文缓存享有折扣；显式缓存命中按输入价 10%，写入按输入价 125%。",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "qwen3.6-plus",
        "display_name": "通义千问 3.6 Plus",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "pricing": build_tiered_payload(
            source_label="阿里云百炼官方价格（人民币原价）",
            source_url="https://help.aliyun.com/zh/model-studio/model-pricing",
            note=f"{CHINA_PRICE_NOTE} 默认缓存价按显式缓存命中口径保存；显式缓存写入按输入价 125% 记录。",
            tiers=[
                build_cny_tier(
                    tier_key="0_256k",
                    tier_name="0-256K 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=256_000,
                    input_cny_per_1m="2",
                    output_cny_per_1m="12",
                    cache_cny_per_1m="0.2",
                    cache_write_cny_per_1m="2.5",
                    source_note="显式缓存命中按输入价 10%，写入按输入价 125%。",
                ),
                build_cny_tier(
                    tier_key="256k_1m",
                    tier_name="256K-1M 输入",
                    min_prompt_tokens=256_001,
                    max_prompt_tokens=1_000_000,
                    input_cny_per_1m="8",
                    output_cny_per_1m="48",
                    cache_cny_per_1m="0.8",
                    cache_write_cny_per_1m="10",
                    source_note="显式缓存命中按输入价 10%，写入按输入价 125%。",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "qwen3.6-flash",
        "display_name": "通义千问 3.6 Flash",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": True,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "pricing": build_tiered_payload(
            source_label="阿里云百炼官方价格（人民币原价）",
            source_url="https://help.aliyun.com/zh/model-studio/model-pricing",
            note=f"{CHINA_PRICE_NOTE} 默认缓存价按显式缓存命中口径保存；显式缓存写入按输入价 125% 记录。",
            tiers=[
                build_cny_tier(
                    tier_key="0_256k",
                    tier_name="0-256K 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=256_000,
                    input_cny_per_1m="1.2",
                    output_cny_per_1m="7.2",
                    cache_cny_per_1m="0.12",
                    cache_write_cny_per_1m="1.5",
                    source_note="显式缓存命中按输入价 10%，写入按输入价 125%。",
                ),
                build_cny_tier(
                    tier_key="256k_1m",
                    tier_name="256K-1M 输入",
                    min_prompt_tokens=256_001,
                    max_prompt_tokens=1_000_000,
                    input_cny_per_1m="4.8",
                    output_cny_per_1m="28.8",
                    cache_cny_per_1m="0.48",
                    cache_write_cny_per_1m="6",
                    source_note="显式缓存命中按输入价 10%，写入按输入价 125%。",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "glm-5.1",
        "display_name": "智谱 GLM-5.1",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 200_000,
        "max_output_tokens": 128_000,
        "pricing": build_tiered_payload(
            source_label="智谱官方价格（人民币原价）",
            source_url="https://bigmodel.cn/pricing",
            note=CHINA_PRICE_NOTE,
            tiers=[
                build_cny_tier(
                    tier_key="0_32k",
                    tier_name="0-32K 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    input_cny_per_1m="6",
                    output_cny_per_1m="24",
                    cache_cny_per_1m="1.3",
                    cache_storage_cny_per_1m="0",
                ),
                build_cny_tier(
                    tier_key="32k_plus",
                    tier_name="32K+ 输入",
                    min_prompt_tokens=32_001,
                    input_cny_per_1m="8",
                    output_cny_per_1m="28",
                    cache_cny_per_1m="2",
                    cache_storage_cny_per_1m="0",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "glm-5-turbo",
        "display_name": "智谱 GLM-5 Turbo",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 200_000,
        "max_output_tokens": 128_000,
        "pricing": build_tiered_payload(
            source_label="智谱官方价格（人民币原价）",
            source_url="https://bigmodel.cn/pricing",
            note=CHINA_PRICE_NOTE,
            tiers=[
                build_cny_tier(
                    tier_key="0_32k",
                    tier_name="0-32K 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    input_cny_per_1m="5",
                    output_cny_per_1m="22",
                    cache_cny_per_1m="1.2",
                    cache_storage_cny_per_1m="0",
                ),
                build_cny_tier(
                    tier_key="32k_plus",
                    tier_name="32K+ 输入",
                    min_prompt_tokens=32_001,
                    input_cny_per_1m="7",
                    output_cny_per_1m="26",
                    cache_cny_per_1m="1.8",
                    cache_storage_cny_per_1m="0",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "glm-5",
        "display_name": "智谱 GLM-5",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 200_000,
        "max_output_tokens": 128_000,
        "pricing": build_tiered_payload(
            source_label="智谱官方价格（人民币原价）",
            source_url="https://bigmodel.cn/pricing",
            note=CHINA_PRICE_NOTE,
            tiers=[
                build_cny_tier(
                    tier_key="0_32k",
                    tier_name="0-32K 输入",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    input_cny_per_1m="4",
                    output_cny_per_1m="18",
                    cache_cny_per_1m="1",
                    cache_storage_cny_per_1m="0",
                ),
                build_cny_tier(
                    tier_key="32k_plus",
                    tier_name="32K+ 输入",
                    min_prompt_tokens=32_001,
                    input_cny_per_1m="6",
                    output_cny_per_1m="22",
                    cache_cny_per_1m="1.5",
                    cache_storage_cny_per_1m="0",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "glm-4.7",
        "display_name": "智谱 GLM-4.7",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 200_000,
        "max_output_tokens": 128_000,
        "pricing": build_tiered_payload(
            source_label="智谱官方价格（人民币原价）",
            source_url="https://bigmodel.cn/pricing",
            note=CHINA_PRICE_NOTE,
            tiers=[
                build_cny_tier(
                    tier_key="lt32k_out_lt200",
                    tier_name="输入 0-32K，输出 0-200",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    min_completion_tokens=0,
                    max_completion_tokens=200,
                    input_cny_per_1m="2",
                    output_cny_per_1m="8",
                    cache_cny_per_1m="0.4",
                    cache_storage_cny_per_1m="0",
                    source_note="智谱官方同时按输入区间与输出区间阶梯计价。",
                ),
                build_cny_tier(
                    tier_key="lt32k_out_gte200",
                    tier_name="输入 0-32K，输出 200+",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    min_completion_tokens=201,
                    input_cny_per_1m="3",
                    output_cny_per_1m="14",
                    cache_cny_per_1m="0.6",
                    cache_storage_cny_per_1m="0",
                    source_note="智谱官方同时按输入区间与输出区间阶梯计价。",
                ),
                build_cny_tier(
                    tier_key="32k_200k",
                    tier_name="输入 32K-200K",
                    min_prompt_tokens=32_001,
                    max_prompt_tokens=200_000,
                    input_cny_per_1m="4",
                    output_cny_per_1m="16",
                    cache_cny_per_1m="0.8",
                    cache_storage_cny_per_1m="0",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "glm-4.5-air",
        "display_name": "智谱 GLM-4.5 Air",
        "supports_vision": False,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 128_000,
        "max_output_tokens": 96_000,
        "pricing": build_tiered_payload(
            source_label="智谱官方价格（人民币原价）",
            source_url="https://bigmodel.cn/pricing",
            note=CHINA_PRICE_NOTE,
            tiers=[
                build_cny_tier(
                    tier_key="lt32k_out_lt200",
                    tier_name="输入 0-32K，输出 0-200",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    min_completion_tokens=0,
                    max_completion_tokens=200,
                    input_cny_per_1m="0.8",
                    output_cny_per_1m="2",
                    cache_cny_per_1m="0.16",
                    cache_storage_cny_per_1m="0",
                    source_note="智谱官方同时按输入区间与输出区间阶梯计价。",
                ),
                build_cny_tier(
                    tier_key="lt32k_out_gte200",
                    tier_name="输入 0-32K，输出 200+",
                    min_prompt_tokens=0,
                    max_prompt_tokens=32_000,
                    min_completion_tokens=201,
                    input_cny_per_1m="0.8",
                    output_cny_per_1m="6",
                    cache_cny_per_1m="0.16",
                    cache_storage_cny_per_1m="0",
                    source_note="智谱官方同时按输入区间与输出区间阶梯计价。",
                ),
                build_cny_tier(
                    tier_key="32k_128k",
                    tier_name="输入 32K-128K",
                    min_prompt_tokens=32_001,
                    max_prompt_tokens=128_000,
                    input_cny_per_1m="1.2",
                    output_cny_per_1m="8",
                    cache_cny_per_1m="0.24",
                    cache_storage_cny_per_1m="0",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入。",
    },
    {
        "model_name": "kimi-k2.6",
        "display_name": "月之暗面 Kimi K2.6",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 256_000,
        "max_output_tokens": 96_000,
        "pricing": build_tiered_payload(
            source_label="Kimi 官方价格（人民币原价）",
            source_url="https://platform.moonshot.cn/",
            note=f"{CHINA_PRICE_NOTE} 上下文窗口与最大输出按月之暗面官网与阿里云百炼官方托管条目交叉对齐。",
            tiers=[
                build_cny_tier(
                    tier_key="default",
                    tier_name="默认档",
                    min_prompt_tokens=0,
                    input_cny_per_1m="6.5",
                    output_cny_per_1m="27",
                    cache_cny_per_1m="1.1",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入；上下文窗口与输出上限按月之暗面官网与阿里云百炼官方托管条目对齐。",
    },
    {
        "model_name": "kimi-k2.5",
        "display_name": "月之暗面 Kimi K2.5",
        "supports_vision": True,
        "supports_tools": True,
        "supports_chat_completions": True,
        "supports_responses": False,
        "context_window_tokens": 262_144,
        "max_output_tokens": 98_304,
        "pricing": build_tiered_payload(
            source_label="Kimi 官方价格（人民币原价）",
            source_url="https://platform.moonshot.cn/",
            note=f"{CHINA_PRICE_NOTE} 上下文窗口与最大输出按月之暗面官网与阿里云百炼官方托管条目交叉对齐。",
            tiers=[
                build_cny_tier(
                    tier_key="default",
                    tier_name="默认档",
                    min_prompt_tokens=0,
                    input_cny_per_1m="4",
                    output_cny_per_1m="21",
                    cache_cny_per_1m="0.7",
                ),
            ],
        ),
        "remark": "人民币官方原价与汇率快照同步录入；上下文窗口与输出上限按月之暗面官网与阿里云百炼官方托管条目对齐。",
    },
]


def ensure_catalog(db, spec: dict) -> tuple[str, ModelCatalog]:
    catalog = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == spec["model_name"]))
    pricing = spec["pricing"]
    normalized_pricing = ModelPricingService.normalize_catalog_pricing(
        pricing_mode=pricing["pricing_mode"],
        pricing_json=pricing["pricing_json"],
        input_price_per_1k=pricing["input_price_per_1k"],
        output_price_per_1k=pricing["output_price_per_1k"],
        cache_price_per_1k=pricing["cache_price_per_1k"],
        cache_write_price_per_1k=pricing.get("cache_write_price_per_1k"),
    )
    exchange_snapshot = normalized_pricing["exchange_rate_snapshot"]
    if catalog is None:
        catalog = ModelCatalog(
            model_name=spec["model_name"],
            display_name=spec.get("display_name"),
            model_group=ProviderService.infer_model_group(spec["model_name"]),
            enabled=True,
            supports_stream=bool(spec.get("supports_stream", True)),
            supports_vision=bool(spec.get("supports_vision", False)),
            supports_tools=bool(spec.get("supports_tools", False)),
            supports_chat_completions=bool(spec.get("supports_chat_completions", True)),
            supports_responses=bool(spec.get("supports_responses", True)),
            context_window_tokens=spec.get("context_window_tokens"),
            max_input_tokens=spec.get("max_input_tokens"),
            max_output_tokens=spec.get("max_output_tokens"),
            pricing_mode=normalized_pricing["pricing_mode"],
            pricing_json=ModelPricingService.pricing_json_to_db_value(normalized_pricing["pricing_json"]),
            input_price_per_1k=normalized_pricing["input_price_per_1k"],
            output_price_per_1k=normalized_pricing["output_price_per_1k"],
            cache_price_per_1k=normalized_pricing["cache_price_per_1k"],
            cache_write_price_per_1k=normalized_pricing["cache_write_price_per_1k"],
            source_currency=normalized_pricing["source_currency"],
            billing_currency=normalized_pricing["billing_currency"],
            source_input_price_per_1k=normalized_pricing["source_input_price_per_1k"],
            source_output_price_per_1k=normalized_pricing["source_output_price_per_1k"],
            source_cache_price_per_1k=normalized_pricing["source_cache_price_per_1k"],
            source_cache_write_price_per_1k=normalized_pricing["source_cache_write_price_per_1k"],
            exchange_rate_to_billing_currency=exchange_snapshot.exchange_rate,
            exchange_rate_source=exchange_snapshot.exchange_rate_source,
            exchange_rate_at=ModelCatalogService._parse_snapshot_datetime(exchange_snapshot.exchange_rate_at),
            exchange_rate_version=exchange_snapshot.exchange_rate_version,
            rounding_strategy=exchange_snapshot.rounding_strategy,
            speed_label=spec.get("speed_label"),
            remark=spec.get("remark"),
        )
        db.add(catalog)
        db.flush()
        action = "created"
    else:
        catalog.model_group = ProviderService.infer_model_group(spec["model_name"])
        if spec.get("display_name"):
            catalog.display_name = spec["display_name"]
        catalog.supports_stream = bool(spec.get("supports_stream", catalog.supports_stream))
        catalog.supports_vision = bool(spec.get("supports_vision", catalog.supports_vision))
        catalog.supports_tools = bool(spec.get("supports_tools", catalog.supports_tools))
        catalog.supports_chat_completions = bool(
            spec.get("supports_chat_completions", catalog.supports_chat_completions)
        )
        catalog.supports_responses = bool(spec.get("supports_responses", catalog.supports_responses))
        if "context_window_tokens" in spec:
            catalog.context_window_tokens = spec.get("context_window_tokens")
        if "max_input_tokens" in spec:
            catalog.max_input_tokens = spec.get("max_input_tokens")
        if "max_output_tokens" in spec:
            catalog.max_output_tokens = spec.get("max_output_tokens")
        if spec.get("speed_label") is not None:
            catalog.speed_label = spec.get("speed_label")
        if spec.get("remark") is not None:
            catalog.remark = spec.get("remark")
        catalog.pricing_mode = normalized_pricing["pricing_mode"]
        catalog.pricing_json = ModelPricingService.pricing_json_to_db_value(normalized_pricing["pricing_json"])
        catalog.input_price_per_1k = normalized_pricing["input_price_per_1k"]
        catalog.output_price_per_1k = normalized_pricing["output_price_per_1k"]
        catalog.cache_price_per_1k = normalized_pricing["cache_price_per_1k"]
        catalog.cache_write_price_per_1k = normalized_pricing["cache_write_price_per_1k"]
        catalog.source_currency = normalized_pricing["source_currency"]
        catalog.billing_currency = normalized_pricing["billing_currency"]
        catalog.source_input_price_per_1k = normalized_pricing["source_input_price_per_1k"]
        catalog.source_output_price_per_1k = normalized_pricing["source_output_price_per_1k"]
        catalog.source_cache_price_per_1k = normalized_pricing["source_cache_price_per_1k"]
        catalog.source_cache_write_price_per_1k = normalized_pricing["source_cache_write_price_per_1k"]
        catalog.exchange_rate_to_billing_currency = exchange_snapshot.exchange_rate
        catalog.exchange_rate_source = exchange_snapshot.exchange_rate_source
        catalog.exchange_rate_at = ModelCatalogService._parse_snapshot_datetime(exchange_snapshot.exchange_rate_at)
        catalog.exchange_rate_version = exchange_snapshot.exchange_rate_version
        catalog.rounding_strategy = exchange_snapshot.rounding_strategy
        action = "updated"
    ModelCatalogService._sync_provider_prices_from_catalog(db, catalog, price_fields=PRICE_FIELDS)
    ModelCatalogService._sync_provider_capabilities_from_catalog(db, catalog)
    return action, catalog


def main() -> None:
    init_database(allow_production_ddl=True)
    db = SessionLocal()
    created: list[str] = []
    updated: list[str] = []
    try:
        for spec in MODEL_SPECS:
            action, _catalog = ensure_catalog(db, spec)
            if action == "created":
                created.append(spec["model_name"])
            else:
                updated.append(spec["model_name"])
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        print(
            f"[{now_beijing().isoformat(timespec='seconds')}] 已同步官方模型价格："
            f"新增 {len(created)} 个，更新 {len(updated)} 个。"
        )
        if created:
            print("新增模型：", "、".join(created))
        if updated:
            print("更新模型：", "、".join(updated))
        spark = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == "gpt-5.3-codex-spark"))
        if spark is not None:
            print(
                "gpt-5.3-codex-spark 当前价格模式：",
                spark.pricing_mode,
                "输入价：",
                spark.input_price_per_1k,
                "输出价：",
                spark.output_price_per_1k,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
