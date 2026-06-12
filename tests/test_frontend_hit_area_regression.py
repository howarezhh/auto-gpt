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


def test_provider_form_exposes_native_protocol_and_custom_path() -> None:
    providers_html = read_text("app/templates/providers.html")
    app_js = read_text("app/static/js/app.js")

    assert 'id="provider-protocol-type"' in providers_html
    assert 'id="provider-native-endpoint-path"' in providers_html
    assert 'native_endpoint_path: providerNativeEndpointPathInput?.value.trim() || null' in app_js
    assert 'inferProviderProtocolFromType(providerTypeInput.value)' in app_js
    assert 'inferProviderModelProtocolType(nameInput.value)' in app_js
