from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_interaction_hit_area_guard_is_registered() -> None:
    app_js = read_text("app/static/js/app.js")

    assert "function initInteractionHitAreaGuards()" in app_js
    assert "interactionHitAreaGuardsBound" in app_js
    assert 'event.target.closest("label.settings-switch-card")' in app_js
    assert 'event.target.closest(".settings-switch-control")' in app_js
    assert ".ip-action-help-icon" in app_js
    assert "helpTrigger.closest(HELP_TOOLTIP_TRIGGER_SELECTOR)" in app_js
    assert "event.preventDefault();" in app_js
    assert "initInteractionHitAreaGuards();" in app_js


def test_switch_cards_only_signal_switch_control_as_click_target() -> None:
    app_css = read_text("app/static/css/app.css")

    assert re.search(r"\.settings-switch-card\s*\{[^}]*cursor:\s*default;", app_css, re.S)
    assert re.search(r"\.settings-switch-control\s*\{[^}]*cursor:\s*pointer;", app_css, re.S)
    assert re.search(r"\.settings-switch-slider\s*\{[^}]*pointer-events:\s*none;", app_css, re.S)
    assert re.search(r"\.ip-action-help-icon\s*\{[^}]*cursor:\s*help;", app_css, re.S)


def test_settings_switch_labels_have_explicit_targets() -> None:
    settings_html = read_text("app/templates/settings.html")
    switch_labels = re.findall(r"<label\s+class=\"settings-switch-card[^\"]*\"[^>]*>", settings_html)

    assert switch_labels
    assert all(" for=" in label for label in switch_labels)


def test_frontend_protocol_detection_skips_native_protocol_models() -> None:
    app_js = read_text("app/static/js/app.js")

    assert '["gemini", "claude_messages"].includes(String(item.protocolType || ""))' in app_js
    assert "Gemini/Claude 原生协议模型无需端点协议检测，请使用健康检测和可信检测" in app_js
    assert "item.protocolLabel = formatProviderModelProtocolLabel(modelConfig)" not in app_js
    assert "protocolLabel: formatProviderModelProtocolLabel(modelConfig)" in app_js
    assert "protocolLabel: formatProviderModelProtocolLabel(model)" in app_js


def test_frontend_probe_buttons_use_per_model_live_results() -> None:
    app_js = read_text("app/static/js/app.js")

    assert "const PROVIDER_MODEL_PROBE_STAGGER_MS = 10000;" in app_js
    assert "async function runStaggeredByPreviousCompletion" in app_js
    assert "createPlaygroundProviderProbeResult(provider)" in app_js
    assert "`/api/providers/${provider.id}/models/${modelConfig.id}/test`" in app_js
    assert "applyPlaygroundProviderModelProbeResult(providerResult, modelConfig" in app_js
    assert "`/api/providers/${provider.id}/test`" not in app_js
    assert '"/api/providers/test-connectivity"' not in app_js
    assert '"/api/providers/test-all"' not in app_js
    assert '"/api/models/test-all"' not in app_js
    assert '"/api/providers/models/protocol-detection"' in app_js


def test_playground_uses_mode_workbench_layout_and_provider_terms() -> None:
    playground_html = read_text("app/templates/playground.html")
    app_js = read_text("app/static/js/app.js")
    app_css = read_text("app/static/css/app.css")

    assert 'class="playground-mode-tabs"' in playground_html
    assert 'data-playground-tab="single"' in playground_html
    assert 'data-playground-tab="batch"' in playground_html
    assert 'class="playground-mode-panel playground-workbench' in playground_html
    assert 'class="playground-mode-panel playground-batch-workbench' in playground_html
    assert 'id="playground-form"' in playground_html
    assert 'id="playground-batch-form"' in playground_html
    assert "选中启用提供商" in playground_html
    assert "批量测试所选提供商" in playground_html

    assert "function activatePlaygroundMode" in app_js
    assert 'document.querySelectorAll("[data-playground-tab]")' in app_js
    assert "当前还没有已配置提供商" in app_js
    assert "provider.base_url || provider.provider_type || null" in app_js
    assert "请至少选择一个提供商" in app_js
    assert "个渠道" not in playground_html

    assert ".playground-mode-tabs" in app_css
    assert ".playground-workbench" in app_css
    assert ".playground-batch-workbench" in app_css
    assert ".playground-mode-panel[hidden]" in app_css


def test_billing_frontend_exposes_multicurrency_pricing_fields() -> None:
    models_html = read_text("app/templates/models.html")
    app_js = read_text("app/static/js/app.js")
    api_key_detail_html = read_text("app/templates/api_key_detail.html")
    user_billing_html = read_text("app/templates/user_billing.html")
    user_log_detail_html = read_text("app/templates/user_log_detail.html")
    provider_models_html = read_text("app/templates/provider_models.html")
    base_html = read_text("app/templates/base.html")

    for field_id in (
        "model-cache-write-price",
        "model-source-currency",
        "model-billing-currency",
        "model-source-input-price",
        "model-source-output-price",
        "model-source-cache-price",
        "model-source-cache-write-price",
        "model-exchange-rate",
        "model-exchange-rate-source",
        "model-exchange-rate-at",
        "model-exchange-rate-version",
        "model-fee-components",
    ):
        assert f'id="{field_id}"' in models_html

    assert "function currencySymbol" in app_js
    assert "function decimalStringToPricePer1K" in app_js
    assert "function buildPricingJsonFromForm" in app_js
    assert "source_currency: sourceCurrency" in app_js
    assert "billing_currency: billingCurrency" in app_js
    assert "fee_components: parseFeeComponentsField(feeComponentsInput)" in app_js
    assert "cacheWritePrice = toFiniteNumber(log?.channel_price_cache_write_per_1k);" in app_js
    assert "cacheWritePrice = toFiniteNumber(log?.channel_price_cache_write_per_1k) ?? inputPrice" not in app_js
    assert "原币种价格" in app_js
    assert "账户币种价格" in app_js
    assert "exchange_rate_to_billing_currency" in app_js
    assert "exchange_rate_to_billing_currency: parseOptionalRawDecimalField(exchangeRateInput, \"汇率\")" in app_js
    assert "exchange_rate_to_billing_currency: exchangeRateInput.value.trim() ? Number(exchangeRateInput.value) : null" not in app_js
    assert "return toPricePer1K(value);" not in app_js
    assert "js/app.js') }}?v=20260614-" in base_html

    assert "<th>原币种</th>" in api_key_detail_html
    assert "<th>原币种</th>" in user_billing_html
    assert "billing_currency or 'USD'" in read_text("app/templates/user_models.html")
    assert "source_input_price_per_1k|display_price_per_1m" in read_text("app/templates/user_models.html")
    assert "source_amount|display_money" in user_billing_html
    assert "source_amount|display_money" in read_text("app/templates/user_api_key_detail.html")
    assert "原币种快照" in user_log_detail_html
    assert "留空表示沿用输入单价" not in provider_models_html


def test_provider_form_keeps_protocol_on_model_config_and_provider_type_dropdown() -> None:
    providers_html = read_text("app/templates/providers.html")
    provider_models_html = read_text("app/templates/provider_models.html")
    app_js = read_text("app/static/js/app.js")

    assert 'id="provider-protocol-type"' not in providers_html
    assert 'id="provider-native-endpoint-path"' not in providers_html
    assert '<select class="field-input" id="provider-type">' in providers_html
    for label in ("OpenAI 兼容", "OpenAI-Response", "Gemini", "Claude / Anthropic", "Azure OpenAI", "DeepSeek", "通义千问", "New API", "CherryIN", "Ollama"):
        assert label in providers_html
    assert "const PROVIDER_TYPE_OPTIONS = [" in app_js
    assert "function normalizeProviderType" in app_js
    assert "function inferProviderType" in app_js
    assert "function renderProviderTypeOptions" in app_js
    assert "providerTypeManuallyTouched = true" in app_js
    assert "syncProviderTypeFromForm({ force: !providerTypeManuallyTouched })" in app_js
    assert 'gemini: "google_gemini"' in app_js
    assert 'claude: "anthropic_official"' in app_js
    assert 'data-model-config-field="native_endpoint_path"' in app_js
    assert 'id="provider-model-edit-model-name"' in provider_models_html
    assert 'id="provider-model-edit-upstream-model-name"' in provider_models_html
    assert 'id="provider-model-edit-native-endpoint-path"' in provider_models_html
    assert "providerModelEditModelNameInput" in app_js
    assert "model_name: modelName" in app_js
    assert "upstream_model_name: upstreamModelName" in app_js
    assert "providerModelEditNativeEndpointPathInput?.value.trim() || null" in app_js
    assert "const inferredProtocol = inferProviderModelProtocolType(modelName);" in app_js


def test_external_native_protocol_entries_are_visible_in_docs_and_user_pages() -> None:
    docs_html = read_text("app/templates/docs.html")
    user_key_html = read_text("app/templates/user_api_key_detail.html")
    user_home_html = read_text("app/templates/user_home.html")
    ip_management_html = read_text("app/templates/ip_management.html")
    app_js = read_text("app/static/js/app.js")
    base_html = read_text("app/templates/base.html")

    assert "/v1beta/models/{model}:generateContent" in docs_html
    assert "/v1beta/models/YOUR_GEMINI_MODEL:generateContent" in docs_html
    assert "/v1/messages" in docs_html
    assert "Gemini 原生入口" in user_key_html
    assert "Claude 原生入口" in user_key_html
    assert "Gemini 原生入口" in user_home_html
    assert "Claude 原生入口" in user_home_html
    assert "外部代理生效" in ip_management_html
    assert 'external_v1: "外部代理"' in app_js
    assert "app.js') }}?v=20260614-" in base_html


def test_provider_directory_uses_server_pagination_and_same_row_filters() -> None:
    providers_html = read_text("app/templates/providers.html")
    app_js = read_text("app/static/js/app.js")
    app_css = read_text("app/static/css/app.css")

    for field_id in (
        "provider-search",
        "provider-health-filter",
        "provider-enabled-filter",
        "provider-trust-filter",
        "provider-circuit-filter",
        "provider-type-filter",
        "provider-group-filter",
        "provider-page-size",
    ):
        assert f'id="{field_id}"' in providers_html
    assert 'id="provider-page-meta"' in providers_html
    assert 'id="provider-prev-page-btn"' in providers_html
    assert 'id="provider-next-page-btn"' in providers_html
    assert "/api/providers/directory" in app_js
    assert "page_size: String(pageSize || 20)" in app_js
    assert "loadAllProviderDirectoryItemsForCurrentFilters" in app_js
    assert ".provider-directory-filters" in app_css
    assert "repeat(7, minmax(118px, 1fr))" in app_css


def test_frontend_locks_native_protocol_by_model_group() -> None:
    app_js = read_text("app/static/js/app.js")

    assert "function protocolTypeForModelGroup" in app_js
    assert 'if (group === "gemini") return "gemini";' in app_js
    assert 'if (group === "claude") return "claude_messages";' in app_js
    assert "function isProviderModelProtocolLocked" in app_js
    assert 'groupInput?.addEventListener("change", () => {' in app_js
    assert "groupInput.dataset.userEdited = \"true\";" in app_js
    assert "syncModelProtocolLock(row)" in app_js
    assert "protocolInput.disabled = true;" in app_js
    assert "protocolInput.disabled = false;" in app_js
    assert "const protocolType = protocolTypeForModelGroup(modelGroup, upstreamModelName || modelName, requestedProtocolType);" in app_js
    assert "syncProviderModelEditProtocolLock(modelConfig.upstream_model_name || modelConfig.model_name)" in app_js
    assert "protocol_type: protocolType" in app_js
    assert "Gemini / Claude 类模型只能使用对应原生协议；中国国内模型或 GPT 系列模型会按名称和分组自动推断为 Chat、Responses 或双协议。" in app_js


def test_provider_form_custom_model_uses_name_and_id_fields() -> None:
    providers_html = read_text("app/templates/providers.html")
    provider_models_html = read_text("app/templates/provider_models.html")
    app_js = read_text("app/static/js/app.js")

    assert 'id="provider-custom-model-name"' in providers_html
    assert 'id="provider-custom-model-id"' in providers_html
    assert 'id="provider-model-edit-model-name"' in provider_models_html
    assert 'id="provider-model-edit-upstream-model-name"' in provider_models_html
    assert "模型名称" in providers_html
    assert "模型ID" in providers_html
    assert "模型名称、模型ID" in provider_models_html
    assert "用于本平台展示和用户请求。" in provider_models_html
    assert "用于实际请求上游提供商。" in provider_models_html
    assert 'const customModelIdInput = document.getElementById("provider-custom-model-id")' in app_js
    assert "upstream_model_name: upstreamModelName" in app_js
    assert "upstream_model_name: discoveredModel.upstream_model_name || discoveredModel.model_id || modelName" in app_js
    assert "upstream_model_name: catalogModel.upstream_model_name || catalogModel.model_id || catalogModel.model_name || modelName" in app_js
    assert "请先输入模型ID" in app_js
    assert "isProviderModelProtocolLocked(modelGroup, modelName)" in app_js


def test_provider_form_validates_name_and_health_probe_detail_labels() -> None:
    app_js = read_text("app/static/js/app.js")

    assert "const PROVIDER_NAME_PATTERN = /^[\\u4e00-\\u9fffA-Za-z0-9]+$/;" in app_js
    assert "function validateProviderNameField" in app_js
    assert "提供商名称只能包含中文、英文字母或数字，不能包含标点符号或空格。" in app_js
    assert "提供商名称已存在，请换一个名称。" in app_js
    assert "providerNameInput?.addEventListener(\"input\"" in app_js
    assert "providerNameInput?.addEventListener(\"blur\"" in app_js
    assert "probe.provider_name || (probe.provider_id ? `提供商 ${probe.provider_id}` : \"-\")" in app_js
    assert "probe.model_display_id || probe.model_name || \"-\"" in app_js


def test_models_page_has_group_filter_and_immediate_test_progress() -> None:
    models_html = read_text("app/templates/models.html")
    app_js = read_text("app/static/js/app.js")
    models_router = read_text("app/routers/models.py")
    model_catalog_service = read_text("app/services/model_catalog_service.py")

    assert 'id="models-model-group"' in models_html
    assert "<span>模型分组</span>" in models_html
    assert "const modelGroupFilterSelect = document.getElementById(\"models-model-group\")" in app_js
    assert "function renderModelGroupFilterOptions()" in app_js
    assert "params.set(\"model_group\", modelGroupFilterSelect.value)" in app_js
    assert "modelGroupFilterSelect.addEventListener(\"change\"" in app_js
    assert "model_group: str | None = Query(default=None)" in models_router
    assert "model_group=model_group" in models_router
    assert "normalized_model_group = ProviderService.normalize_model_group(model_group)" in model_catalog_service
    assert "ModelCatalog.model_group == normalized_model_group" in model_catalog_service
    assert "renderSingleModelTestProgressBody({" in app_js
    assert "请求已提交，正在按模型绑定的提供商执行健康测试。" in app_js
    assert "refreshHealthCheckResultModal(" in app_js


def test_ip_management_event_log_filters_and_detail_are_wired() -> None:
    ip_html = read_text("app/templates/ip_management.html")
    app_js = read_text("app/static/js/app.js")
    app_css = read_text("app/static/css/app.css")

    for field_id in (
        "ip-event-api-key",
        "ip-event-request-log-id",
        "ip-event-user-account-id",
    ):
        assert f'id="{field_id}"' in ip_html
    assert "<th><span class=\"ip-table-heading\">日志" in ip_html
    assert 'api_key: document.getElementById("ip-event-api-key")?.value.trim()' in app_js
    assert 'request_log_id: document.getElementById("ip-event-request-log-id")?.value' in app_js
    assert 'user_account_id: document.getElementById("ip-event-user-account-id")?.value' in app_js
    assert "event.request_log_id" in app_js
    assert "event.api_client_key_prefix" in app_js
    assert "event.user_account_id" in app_js
    assert ".ip-event-detail-summary" in app_css


def test_log_center_delete_filtered_controls_are_wired() -> None:
    logs_html = read_text("app/templates/logs.html")
    app_js = read_text("app/static/js/app.js")
    logs_router = read_text("app/routers/logs.py")
    logging_router = read_text("app/routers/logging_api.py")

    assert 'id="logs-delete-filtered-btn"' in logs_html
    assert 'id="typed-logs-delete-filtered-btn"' in logs_html
    assert 'id="logs-start-at"' in logs_html
    assert 'id="logs-end-at"' in logs_html
    assert "/api/logs/filtered" in app_js
    assert "/api/logging/typed-events/" in app_js
    assert "hasScopedDeleteParams" in app_js
    assert "describeScopedDeleteParams" in app_js
    assert "删除筛选出的请求日志" in app_js
    assert "删除筛选出的${config.title}" in app_js
    assert '@router.delete("/filtered")' in logs_router
    assert "delete_logs_by_filters" in logs_router
    assert '@router.delete("/typed-events/{typed_log_type}")' in logging_router
    assert "request_path=request_path or path" in logging_router
