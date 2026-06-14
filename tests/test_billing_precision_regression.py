import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Numeric

from app.models.logging_events import BillingProcessEvent, RequestAuthEvent, RequestBillingEvent
from app.models.request_log import RequestLog
from app.services.billing_reservation_service import BillingReservationService
from app.services.billing_service import BillingService
from app.services.currency_service import CurrencyService
from app.services.model_pricing_service import ModelPricingService
from app.services.proxy_service import ProxyService


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_cny_catalog_pricing_keeps_source_prices_and_exchange_snapshot() -> None:
    pricing = {
        "source_currency": "CNY",
        "billing_currency": "USD",
        "exchange_rate_to_usd": "0.1471713577725329730343978813",
        "exchange_rate_source": "official_pricing_import_snapshot",
        "exchange_rate_at": "2026-06-01T00:00:00+08:00",
        "exchange_rate_version": "official_pricing_2026_06_01_usd_cny_6_7948",
        "source_input_price_per_1k": "1.23",
        "source_output_price_per_1k": "4.56",
        "source_cache_write_price_per_1k": "0.78",
        "fee_components": [
            {"component_key": "image", "unit": "per_image", "source_amount": "0.28", "source_currency": "CNY"},
        ],
    }

    normalized = ModelPricingService.normalize_catalog_pricing(
        pricing_mode="fixed",
        pricing_json=pricing,
        input_price_per_1k=None,
        output_price_per_1k=None,
        cache_price_per_1k=None,
        cache_write_price_per_1k=None,
    )

    assert normalized["source_currency"] == "CNY"
    assert normalized["billing_currency"] == "USD"
    assert normalized["source_input_price_per_1k"] == Decimal("1.230000000000")
    assert normalized["input_price_per_1k"] == Decimal("0.181020770061")
    assert normalized["source_cache_write_price_per_1k"] == Decimal("0.780000000000")
    assert normalized["cache_write_price_per_1k"] == Decimal("0.114793659063")
    assert normalized["exchange_rate_snapshot"].exchange_rate_version == "official_pricing_2026_06_01_usd_cny_6_7948"
    assert normalized["pricing_json"]["fee_components"][0]["source_amount"] == Decimal("0.280000000000")
    assert normalized["pricing_json"]["fee_components"][0]["amount"] == Decimal("0.041207980176")
    assert ModelPricingService.pricing_json_to_db_value(pricing) is not None


def test_provider_price_resolution_preserves_fixed_cache_write_price() -> None:
    resolved = ModelPricingService.resolve_catalog_prices_for_provider(
        pricing_mode="fixed",
        pricing_json=None,
        input_price_per_1k=Decimal("0.10"),
        output_price_per_1k=Decimal("0.20"),
        cache_price_per_1k=Decimal("0.03"),
        cache_write_price_per_1k=Decimal("0.07"),
        price_multiplier=Decimal("2"),
    )

    assert resolved["cache_write_price_per_1k"] == Decimal("0.140000000000")
    assert resolved["source_cache_write_price_per_1k"] == Decimal("0.140000000000")


def test_user_model_catalog_does_not_fallback_cache_write_to_input_price() -> None:
    source = read_text("app/services/model_catalog_service.py")
    user_model_section = source[source.index("def list_user_models") : source.index("def enabled_model_name_set")]
    cache_write_section = user_model_section[
        user_model_section.index('"cache_write_price_per_1k":') : user_model_section.index('"available_provider_names":')
    ]

    assert "else catalog.input_price_per_1k" not in cache_write_section
    assert '"source_currency": catalog.source_currency' in user_model_section
    assert '"billing_currency": catalog.billing_currency' in user_model_section


def test_fee_component_cost_tracks_billing_and_source_currency_separately() -> None:
    log = RequestLog(
        request_path="/v1/images/generations",
        request_body_json='{"n": 2}',
        response_body_json='{"data": [{"url": "https://example.test/1.png"}, {"url": "https://example.test/2.png"}]}',
    )
    log.billing_currency = "USD"
    log.source_currency = "CNY"

    billing_cost, source_cost = BillingService._compute_fee_component_costs(
        log,
        [
            {
                "component_key": "image",
                "unit": "per_image",
                "amount": Decimal("0.014717135777"),
                "currency": "USD",
                "source_amount": Decimal("0.10"),
                "source_currency": "CNY",
            }
        ],
        pricing_metadata={},
    )

    assert billing_cost == Decimal("0.029434272")
    assert source_cost == Decimal("0.200000000")


def test_unpriced_preflight_rejects_candidate_when_tokens_are_estimable(monkeypatch) -> None:
    monkeypatch.setattr(
        ProxyService,
        "_estimate_preflight_request_cost",
        staticmethod(lambda *args, **kwargs: (None, 12, 3)),
    )
    owner = SimpleNamespace(id=9, currency_code="USD", balance_amount=Decimal("10"), frozen_amount=Decimal("0"))
    auth = SimpleNamespace(api_client_key=SimpleNamespace(owner_user=owner))
    provider_model = SimpleNamespace(id=7, model_name="未定价模型")

    result = asyncio.run(
        ProxyService._precheck_owner_balance_for_candidate(
            None,
            api_client_auth=auth,
            provider_model=provider_model,
            payload={"messages": [{"role": "user", "content": "ping"}]},
            request_path="/v1/chat/completions",
            model_name="未定价模型",
            trace_id="trace-test",
        )
    )

    assert result["code"] == "model_price_unset"
    assert result["estimated_input_tokens"] == 12
    assert result["estimated_output_tokens"] == 3


def test_reservation_lua_tops_up_existing_lease_instead_of_short_circuiting() -> None:
    lua = BillingReservationService._ACQUIRE_LUA

    assert "if amount <= existing_amount then" in lua
    assert "local delta = amount - existing_amount" in lua
    assert "current + delta > available" in lua
    assert "INCRBY', active_key, delta" in lua


def test_money_audit_columns_use_numeric_not_float_annotations() -> None:
    money_columns = [
        RequestAuthEvent.__table__.c.remaining_cost_daily,
        RequestBillingEvent.__table__.c.prompt_cost,
        RequestBillingEvent.__table__.c.completion_cost,
        RequestBillingEvent.__table__.c.total_cost,
        RequestBillingEvent.__table__.c.balance_before,
        RequestBillingEvent.__table__.c.balance_after,
        BillingProcessEvent.__table__.c.balance_delta,
        BillingProcessEvent.__table__.c.balance_after,
    ]

    assert all(isinstance(column.type, Numeric) for column in money_columns)
    logging_events_source = read_text("app/models/logging_events.py")
    assert "Mapped[float | None] = mapped_column(Numeric" not in logging_events_source


def test_billing_precision_static_guards() -> None:
    user_accounts_source = read_text("app/routers/user_accounts.py")
    user_quota_source = read_text("app/services/user_quota_service.py")
    log_service_source = read_text("app/services/log_service.py")
    log_schema_source = read_text("app/schemas/log.py")
    model_catalog_schema_source = read_text("app/schemas/model_catalog.py")
    provider_schema_source = read_text("app/schemas/provider.py")
    pricing_script_source = read_text("scripts/sync_official_model_pricing.py")
    migration_source = read_text("migrations/2026-06-09_extend_typed_logging_details.sql")

    assert "amount=float" not in user_accounts_source
    assert "BillingService.to_float(" not in read_text("app/services/billing_service.py")
    assert "func.sum(case((RequestLog.success.is_(True), RequestLog.total_tokens)" in user_quota_source
    assert "channel_price_cache_write_per_1k if item.channel_price_cache_write_per_1k is not None else input_price" not in log_service_source
    assert "prompt_cost: float" not in log_schema_source
    assert "channel_price_input_per_1k: float" not in log_schema_source
    assert "exchange_rate_to_billing_currency: float" not in log_schema_source
    assert "input_price_per_1k: float" not in model_catalog_schema_source
    assert "cache_write_price_per_1k: float" not in provider_schema_source
    assert "USD_CNY_RATE" not in pricing_script_source
    assert "prompt_cost NUMERIC(24, 9)" in migration_source
    assert "balance_delta NUMERIC(24, 9)" in migration_source


def test_currency_snapshot_default_cny_to_usd_is_deterministic() -> None:
    snapshot = CurrencyService.resolve_conversion_snapshot(
        source_currency="CNY",
        billing_currency="USD",
        pricing_metadata={},
    )

    assert snapshot.exchange_rate == Decimal("0.1471713577725329730343978813")
    assert snapshot.exchange_rate_source == "official_pricing_import_snapshot"
    assert snapshot.rounding_strategy == "ROUND_HALF_UP"
