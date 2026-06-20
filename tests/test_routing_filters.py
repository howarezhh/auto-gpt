from __future__ import annotations

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.routing import RouteCandidate
from app.services.routing import helpers as route_helpers
from app.services.routing.filters import CapacityFilter, RouteFilterEvaluationContext, RouteFilterChain
from app.utils.timezone import now_beijing


def _provider(provider_id: int = 1) -> Provider:
    return Provider(
        id=provider_id,
        name=f"测试提供商-{provider_id}",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="secret",
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        maintenance_mode_enabled=False,
    )


def _provider_model(model_id: int = 11, provider_id: int = 1) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider_id=provider_id,
        model_name="测试模型",
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        supports_stream=True,
        supports_tools=True,
        supports_vision=True,
        supports_chat_completions=True,
        supports_responses=True,
    )


def _context(**kwargs) -> RouteFilterEvaluationContext:
    values = {
        "db": object(),
        "model_name": "测试模型",
        "route_context": None,
        "allowed_provider_ids": None,
        "enabled_model_names": {"测试模型"},
        "required_endpoint_path": "/chat/completions",
        "now": now_beijing(),
        "require_chat_completions": True,
    }
    values.update(kwargs)
    return RouteFilterEvaluationContext(**values)


def _allow_content_trust(monkeypatch) -> None:
    monkeypatch.setattr(route_helpers, "content_policy_diagnostic_reason", lambda *args, **kwargs: None)
    monkeypatch.setattr(route_helpers, "provider_model_blocked_by_content_policy", lambda *args, **kwargs: False)


def test_filter_chain_rejects_disabled_provider(monkeypatch) -> None:
    _allow_content_trust(monkeypatch)
    provider = _provider()
    provider.enabled = False

    decision = RouteFilterChain().evaluate_candidate(provider, _provider_model(), _context())

    assert not decision.allowed
    assert decision.reason_code == "provider_disabled"


def test_filter_chain_rejects_model_name_mismatch(monkeypatch) -> None:
    _allow_content_trust(monkeypatch)
    model = _provider_model()
    model.model_name = "其它模型"

    decision = RouteFilterChain().evaluate_candidate(
        _provider(),
        model,
        _context(enabled_model_names={"测试模型", "其它模型"}),
    )

    assert not decision.allowed
    assert decision.reason_code == "model_name_mismatch"


def test_filter_chain_rejects_missing_tools_capability(monkeypatch) -> None:
    _allow_content_trust(monkeypatch)
    model = _provider_model()
    model.supports_tools = False

    decision = RouteFilterChain().evaluate_candidate(_provider(), model, _context(require_tools=True))

    assert not decision.allowed
    assert decision.reason_code == "tools_not_supported"


def test_filter_chain_reports_native_protocol_mismatch(monkeypatch) -> None:
    _allow_content_trust(monkeypatch)
    provider = _provider()
    provider.provider_type = "openai_compatible"

    decision = RouteFilterChain().evaluate_candidate(
        provider,
        _provider_model(),
        _context(required_upstream_protocol_type="gemini"),
    )

    assert not decision.allowed
    assert decision.reason_code == "protocol_mismatch"
    assert decision.details["required_protocol"] == "gemini"


def test_capacity_filter_sets_load_factor_and_records_missing_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(route_helpers, "capacity_load_factor", lambda *args, **kwargs: 0.42)
    available = RouteCandidate(provider=_provider(1), provider_model=_provider_model(11, 1))
    missing = RouteCandidate(provider=_provider(2), provider_model=_provider_model(22, 2))

    result = CapacityFilter().apply_with_snapshots(
        [available, missing],
        snapshots={1: ProviderCapacitySnapshot(active_requests=0, active_streams=0, current_qps=0, current_rpm=0)},
        is_stream=False,
    )

    assert result.kept_candidates == [available]
    assert available.load_factor == 0.42
    assert result.reason_counts == {"capacity_snapshot_unavailable": 1}
    assert result.rejected_events[0].reason_code == "capacity_snapshot_unavailable"
