from __future__ import annotations

from types import SimpleNamespace

from app.services.provider_service import ProviderService


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
    assert ProviderService.infer_model_group("deepseek-chat") == "deepseek"
    assert ProviderService.infer_model_group("qwen-plus") == "qwen"
    assert ProviderService.infer_model_group("gemini-2.5-pro") == "gemini"
    assert ProviderService.infer_model_group("claude-3-5-sonnet-latest") == "claude"
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
