from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.database import SessionLocal
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.model_catalog import ModelCatalogOptionOut, ModelCatalogOut
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_service import ProviderService
from app.utils.timezone import now_beijing


def test_provider_name_rejects_spaces_and_punctuation() -> None:
    assert ProviderService.normalize_provider_name("中文Provider123") == "中文Provider123"
    for value in ("中文 提供商", "中文-提供商", "provider.example", "provider_1"):
        try:
            ProviderService.normalize_provider_name(value)
        except ValueError as exc:
            assert "不能包含标点符号或空格" in str(exc)
        else:
            raise AssertionError(f"provider name should be rejected: {value}")


def test_provider_model_quality_metrics_respect_requested_window(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_load_quality_accumulators(db, *, quality_window_minutes=None):
        captured["quality_window_minutes"] = quality_window_minutes
        return (
            {1: {"recent_request_count": 2, "success_count": 1, "first_token_sum": 300.0, "first_token_count": 2}},
            {11: {"recent_request_count": 2, "success_count": 1, "first_token_sum": 300.0, "first_token_count": 2}},
        )

    monkeypatch.setattr(ProviderService, "_load_quality_accumulators", staticmethod(fake_load_quality_accumulators))
    provider_model = SimpleNamespace(id=11, health_status="healthy", circuit_state="closed")
    provider = SimpleNamespace(id=1, health_status="healthy", circuit_state="closed", provider_models=[provider_model])

    metrics = ProviderService._build_quality_metrics(
        object(),
        [provider],
        quality_window_minutes=24 * 60,
    )

    assert captured["quality_window_minutes"] == 24 * 60
    assert metrics["provider_models"][11]["quality_window_minutes"] == 24 * 60
    assert metrics["provider_models"][11]["recent_request_count"] == 2


def test_model_group_inference_covers_mainstream_model_families() -> None:
    assert ProviderService.infer_model_group("gpt-4.1") == "openai"
    assert ProviderService.infer_model_group("gpt-5.4") == "openai"
    assert ProviderService.infer_model_group("deepseek-chat") == "deepseek"
    assert ProviderService.infer_model_group("deepseek-v4-pro") == "deepseek"
    assert ProviderService.infer_model_group("qwen-plus") == "qwen"
    assert ProviderService.infer_model_group("gemini-2.5-pro") == "gemini"
    assert ProviderService.infer_model_group("claude-3-5-sonnet-latest") == "claude"
    assert ProviderService.infer_model_group("claude-opus-4.6") == "claude"
    assert ProviderService.infer_model_group("all-model") == "unknown"
    assert ProviderService.infer_model_group("unknown-local-model") == "unknown"


def test_model_group_forces_native_protocol_even_when_saved_protocol_is_wrong() -> None:
    gemini_model = SimpleNamespace(
        model_name="custom-gemini-alias",
        model_group="gemini",
        protocol_type="responses",
        supports_chat_completions=False,
        supports_responses=True,
    )
    claude_model = SimpleNamespace(
        model_name="custom-claude-alias",
        model_group="claude",
        protocol_type="both",
        supports_chat_completions=True,
        supports_responses=True,
    )

    assert ProviderService.provider_model_protocol_type(gemini_model) == "gemini"
    assert ProviderService.provider_model_protocol_type(claude_model) == "claude_messages"


def test_provider_model_mount_list_filters_by_model_group() -> None:
    captured = []

    class FakeDb:
        def scalar(self, statement):
            captured.append(statement)
            return 0

        def scalars(self, statement):
            captured.append(statement)
            return []

    result = ProviderService.list_provider_model_mounts(
        FakeDb(),
        page=1,
        page_size=20,
        model_group="通义千问",
    )

    assert result["total"] == 0
    compiled = captured[0].compile()
    assert "provider_models.model_group" in str(compiled)
    assert compiled.params["model_group_1"] == "qwen"


def test_model_catalog_list_filters_by_model_group() -> None:
    statement = ModelCatalogService._model_filter_query(model_group="Claude")
    compiled = statement.compile()

    assert "model_catalogs.model_group" in str(compiled)
    assert compiled.params["model_group_1"] == "claude"


def test_model_catalog_list_and_options_keep_provider_bindings_for_testing() -> None:
    now = now_beijing()
    catalog = ModelCatalog(
        id=101,
        model_name="绑定测试模型",
        display_name="绑定测试模型",
        model_group="openai",
        enabled=True,
        supports_stream=True,
        supports_vision=False,
        supports_tools=True,
        supports_chat_completions=True,
        supports_responses=True,
        context_window_tokens=128000,
        max_input_tokens=64000,
        max_output_tokens=8192,
        pricing_mode="fixed",
        pricing_json=None,
        input_price_per_1k=None,
        output_price_per_1k=None,
        cache_price_per_1k=None,
        cache_write_price_per_1k=None,
        source_currency="USD",
        billing_currency="USD",
        source_input_price_per_1k=None,
        source_output_price_per_1k=None,
        source_cache_price_per_1k=None,
        source_cache_write_price_per_1k=None,
        exchange_rate_to_billing_currency=None,
        exchange_rate_source=None,
        exchange_rate_at=None,
        exchange_rate_version=None,
        rounding_strategy="ROUND_HALF_UP",
        speed_label=None,
        remark=None,
        created_at=now,
        updated_at=now,
    )
    provider = Provider(
        id=11,
        name="绑定测试提供商",
        base_url="https://provider.example.com/v1",
        api_key="sk-test",
        enabled=True,
        priority=10,
        health_status="healthy",
        circuit_state="closed",
        maintenance_mode_enabled=False,
    )
    provider_model = ProviderModel(
        id=22,
        provider_id=11,
        model_name="绑定测试模型",
        enabled=True,
        priority=20,
        health_status="healthy",
        circuit_state="closed",
        supports_stream=True,
        supports_vision=False,
        supports_tools=True,
        supports_image_generation=False,
        supports_chat_completions=True,
        supports_responses=True,
        protocol_type="both",
        model_group="openai",
        content_integrity_status="trusted",
        content_probe_results_json=None,
        price_multiplier=Decimal("1.25"),
        created_at=now,
        updated_at=now,
    )
    provider.provider_models = [provider_model]
    provider_model.provider = provider

    list_payload = ModelCatalogService._serialize_catalog(catalog, [provider])
    list_item = ModelCatalogOut(**list_payload).model_dump()
    option_payload = ModelCatalogService._serialize_catalog_option(catalog, [provider])
    option_item = ModelCatalogOptionOut(**option_payload).model_dump()

    for item in (list_item, option_item):
        assert item["bound_provider_count"] == 1
        assert item["enabled_provider_count"] == 1
        assert item["provider_bindings"][0]["provider_id"] == 11
        assert item["provider_bindings"][0]["provider_model_id"] == 22
        assert item["provider_bindings"][0]["bound"] is True
        assert item["provider_bindings"][0]["enabled"] is True


def test_load_providers_for_catalogs_refreshes_filtered_provider_models_in_same_session() -> None:
    suffix = uuid4().hex[:8]
    model_a = f"绑定缓存模型甲-{suffix}"
    model_b = f"绑定缓存模型乙-{suffix}"
    db = SessionLocal()
    try:
        catalog_a = ModelCatalog(model_name=model_a, model_group="openai", enabled=True)
        catalog_b = ModelCatalog(model_name=model_b, model_group="openai", enabled=True)
        provider = Provider(
            name=f"绑定缓存测试提供商{suffix}",
            base_url="https://provider.example.com/v1",
            api_key="sk-test",
            enabled=True,
            health_status="healthy",
            circuit_state="closed",
        )
        provider.provider_models = [
            ProviderModel(
                model_name=model_a,
                enabled=True,
                health_status="healthy",
                circuit_state="closed",
                price_multiplier=Decimal("1"),
            ),
            ProviderModel(
                model_name=model_b,
                enabled=True,
                health_status="healthy",
                circuit_state="closed",
                price_multiplier=Decimal("1"),
            ),
        ]
        db.add_all([catalog_a, catalog_b, provider])
        db.flush()

        first_providers = ModelCatalogService._load_providers_for_catalogs(db, [catalog_a])
        assert [item.model_name for item in first_providers[0].provider_models] == [model_a]

        second_providers = ModelCatalogService._load_providers_for_catalogs(db, [catalog_b])
        assert [item.model_name for item in second_providers[0].provider_models] == [model_b]
    finally:
        db.rollback()
        db.close()


def test_model_catalog_health_is_aggregated_from_provider_mounts() -> None:
    now = now_beijing()
    catalog = ModelCatalog(
        id=102,
        model_name="聚合可用模型",
        display_name="聚合可用模型",
        model_group="openai",
        enabled=True,
        supports_stream=True,
        supports_vision=False,
        supports_tools=False,
        supports_chat_completions=True,
        supports_responses=True,
        context_window_tokens=128000,
        max_input_tokens=64000,
        max_output_tokens=8192,
        pricing_mode="fixed",
        pricing_json=None,
        input_price_per_1k=None,
        output_price_per_1k=None,
        cache_price_per_1k=None,
        cache_write_price_per_1k=None,
        source_currency="USD",
        billing_currency="USD",
        source_input_price_per_1k=None,
        source_output_price_per_1k=None,
        source_cache_price_per_1k=None,
        source_cache_write_price_per_1k=None,
        exchange_rate_to_billing_currency=None,
        exchange_rate_source=None,
        exchange_rate_at=None,
        exchange_rate_version=None,
        rounding_strategy="ROUND_HALF_UP",
        speed_label=None,
        remark=None,
        created_at=now,
        updated_at=now,
    )
    healthy_provider = Provider(
        id=21,
        name="可用提供商",
        base_url="https://healthy.example.com/v1",
        api_key="sk-test",
        enabled=True,
        priority=10,
        health_status="healthy",
        circuit_state="closed",
        maintenance_mode_enabled=False,
    )
    unhealthy_provider = Provider(
        id=22,
        name="异常提供商",
        base_url="https://unhealthy.example.com/v1",
        api_key="sk-test",
        enabled=True,
        priority=20,
        health_status="healthy",
        circuit_state="closed",
        maintenance_mode_enabled=False,
    )
    healthy_model = ProviderModel(
        id=31,
        provider_id=21,
        model_name="聚合可用模型",
        enabled=True,
        priority=10,
        health_status="healthy",
        circuit_state="closed",
        price_multiplier=Decimal("1"),
        created_at=now,
        updated_at=now,
    )
    unhealthy_model = ProviderModel(
        id=32,
        provider_id=22,
        model_name="聚合可用模型",
        enabled=True,
        priority=20,
        health_status="unhealthy",
        circuit_state="closed",
        price_multiplier=Decimal("1"),
        created_at=now,
        updated_at=now,
    )
    healthy_provider.provider_models = [healthy_model]
    unhealthy_provider.provider_models = [unhealthy_model]
    healthy_model.provider = healthy_provider
    unhealthy_model.provider = unhealthy_provider

    payload = ModelCatalogService._serialize_catalog(catalog, [healthy_provider, unhealthy_provider])

    assert payload["health_status"] == "healthy"
    assert payload["healthy_provider_count"] == 1
    assert payload["unhealthy_provider_count"] == 1
    assert "可用提供商" in payload["health_reason"]

    healthy_model.health_status = "unhealthy"
    payload = ModelCatalogService._serialize_catalog(catalog, [healthy_provider, unhealthy_provider])

    assert payload["health_status"] == "unhealthy"
    assert payload["healthy_provider_count"] == 0
    assert payload["unhealthy_provider_count"] == 2
    assert "所有已绑定提供商均不可用" in payload["health_reason"]
    assert "可用提供商 的挂载不可用" in payload["health_reason"]

    healthy_model.health_status = "healthy"
    healthy_provider.health_status = "unhealthy"
    payload = ModelCatalogService._serialize_catalog(catalog, [healthy_provider, unhealthy_provider])

    assert payload["health_status"] == "unhealthy"
    assert payload["healthy_provider_count"] == 0
    assert "可用提供商 自身不可用" in payload["health_reason"]


def test_provider_native_endpoint_path_is_normalized_and_serialized() -> None:
    assert ProviderService.normalize_native_endpoint_path("proxy/google/{model}:{action}") == "/proxy/google/{model}:{action}"
    provider = SimpleNamespace(
        id=1,
        name="Gemini 自定义网关",
        group_name="海外厂商",
        region_tag="us",
        enabled=True,
        priority=100,
        health_status="unknown",
        protocol_type="gemini",
        native_endpoint_path="/proxy/google/{model}:{action}",
        circuit_state="closed",
        last_latency_ms=None,
        provider_models=[SimpleNamespace(model_name="gemini-2.5-pro")],
    )

    option = ProviderService.provider_to_option_dict(provider)
    summary = ProviderService.provider_to_summary_dict(provider)

    assert option["native_endpoint_path"] == "/proxy/google/{model}:{action}"
    assert summary["native_endpoint_path"] == "/proxy/google/{model}:{action}"


def test_provider_batch_import_allows_provider_without_models() -> None:
    raw_item = {
        "名称": "无模型中文提供商",
        "Base URL": "https://provider.example.com/v1",
        "API Key": "sk-test",
        "类型": "openai_compatible",
        "启用": "是",
    }
    errors: list[str] = []

    payload = ProviderService._normalize_batch_provider_item(raw_item, errors)

    assert errors == []
    assert payload is not None
    assert payload["name"] == "无模型中文提供商"
    assert payload["models"] == []
    assert payload["model_configs"] == []


def test_provider_batch_import_requires_model_name_and_id_when_model_block_exists() -> None:
    raw_item = {
        "名称": "缺模型ID提供商",
        "Base URL": "https://provider.example.com/v1",
        "API Key": "sk-test",
        "模型": """
- 模型名称: 自定义 GPT
  启用: 是
  端点协议: 双协议
  模型分组: OpenAI
  倍率: 1
""",
    }
    errors: list[str] = []

    payload = ProviderService._normalize_batch_provider_item(raw_item, errors)

    assert payload is None
    assert any("模型第 1 组缺少字段：模型ID" in error for error in errors)


def test_provider_batch_import_preserves_model_alias_and_upstream_id() -> None:
    raw_item = {
        "名称": "自定义别名提供商",
        "Base URL": "https://provider.example.com/v1",
        "API Key": "sk-test",
        "模型": """
- 模型名称: 中文展示名
  模型ID: claude-3-5-sonnet-latest
  启用: 是
  端点协议: Claude
  模型分组: Claude
  倍率: 1.2
""",
    }
    errors: list[str] = []

    payload = ProviderService._normalize_batch_provider_item(raw_item, errors)

    assert errors == []
    assert payload is not None
    assert payload["models"] == ["中文展示名"]
    assert payload["model_configs"][0]["model_name"] == "中文展示名"
    assert payload["model_configs"][0]["upstream_model_name"] == "claude-3-5-sonnet-latest"
    assert payload["model_configs"][0]["model_group"] == "claude"
    assert payload["model_configs"][0]["protocol_type"] == "claude_messages"
    assert payload["model_configs"][0]["price_multiplier"] == 1.2


def test_provider_batch_import_defaults_optional_model_fields() -> None:
    raw_item = {
        "名称": "模型默认值提供商",
        "Base URL": "https://provider.example.com/v1",
        "API Key": "sk-test",
        "模型": """
- 模型名称: 自定义通义
  模型ID: qwen-plus
""",
    }
    errors: list[str] = []

    payload = ProviderService._normalize_batch_provider_item(raw_item, errors)

    assert errors == []
    assert payload is not None
    model_config = payload["model_configs"][0]
    assert model_config["model_name"] == "自定义通义"
    assert model_config["upstream_model_name"] == "qwen-plus"
    assert model_config["enabled"] is True
    assert model_config["model_group"] == "qwen"
    assert model_config["protocol_type"] == "chat_completions"
    assert model_config["price_multiplier"] == 1.0


def test_provider_export_keeps_api_key_plaintext() -> None:
    db = SessionLocal()
    try:
        provider = db.query(Provider).order_by(Provider.id.asc()).first()
        assert provider is not None
        text = ProviderService.export_providers_import_text(db)
        assert f"名称: {provider.name}" in text
        assert f"API Key: {provider.api_key}" in text
    finally:
        db.close()


def test_provider_model_export_template_mentions_batch_import_fields() -> None:
    assert "模型挂载批量导入模板" in ProviderService.MODEL_BATCH_IMPORT_TEMPLATE
    assert "提供商名称" in ProviderService.MODEL_BATCH_IMPORT_TEMPLATE
    assert "账户输入价/1M" in ProviderService.MODEL_BATCH_IMPORT_TEMPLATE


def test_protocol_lock_uses_upstream_model_id_for_custom_alias() -> None:
    provider_model = SimpleNamespace(
        model_name="中文展示名",
        upstream_model_name="gemini-2.5-pro",
        model_group=None,
        protocol_type="both",
        supports_chat_completions=True,
        supports_responses=True,
    )

    assert ProviderService.provider_model_protocol_type(provider_model) == "gemini"


def test_upstream_model_name_does_not_use_provider_model_id_as_fallback() -> None:
    provider_model = SimpleNamespace(
        model_name="平台模型别名",
        provider_model_id="gemini-2.5-pro",
    )

    assert ProviderService.provider_model_upstream_model_name(provider_model) == "平台模型别名"
