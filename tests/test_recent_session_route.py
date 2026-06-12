from __future__ import annotations

from types import SimpleNamespace

from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_service import ProviderService
from app.services.router_service import RecentSessionRoute, RouterService


class FakeDb:
    def __init__(self, provider, provider_model) -> None:
        self.provider = provider
        self.provider_model = provider_model

    def get(self, model, item_id):
        if item_id == self.provider_model.id:
            return self.provider_model
        if item_id == self.provider.id:
            return self.provider
        return None


def test_recent_session_direct_candidate_ignores_health_and_circuit(monkeypatch) -> None:
    provider_model = SimpleNamespace(
        id=11,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        priority=100,
        health_status="unhealthy",
        circuit_state="open",
        last_latency_ms=None,
        supports_stream=True,
        supports_vision=False,
        supports_tools=False,
        supports_image_generation=False,
        supports_chat_completions=True,
        supports_responses=True,
    )
    provider = SimpleNamespace(
        id=1,
        name="测试提供商",
        enabled=True,
        maintenance_mode_enabled=False,
        priority=100,
        health_status="unhealthy",
        circuit_state="open",
        last_latency_ms=None,
        max_active_requests=0,
        max_active_streams=0,
        max_qps=0,
        max_rpm=0,
        provider_models=[provider_model],
    )

    monkeypatch.setattr(
        RouterService,
        "load_recent_session_route",
        staticmethod(lambda db, sticky_key: RecentSessionRoute(provider_id=1, provider_model_id=11, model_name="测试模型")),
    )
    monkeypatch.setattr(ModelCatalogService, "enabled_model_name_set", staticmethod(lambda db: {"测试模型"}))
    monkeypatch.setattr(LogService, "route_metric_summary", staticmethod(lambda db, **kwargs: {}))
    monkeypatch.setattr(ProviderService, "provider_supports_chat_completions", staticmethod(lambda provider: True))
    monkeypatch.setattr(ProviderService, "provider_supports_responses", staticmethod(lambda provider: True))
    monkeypatch.setattr(RouterService, "_provider_blocked_by_content_policy", staticmethod(lambda provider, *, route_context: False))
    monkeypatch.setattr(
        RouterService,
        "_provider_model_blocked_by_content_policy",
        staticmethod(lambda provider_model, *, provider, route_context: False),
    )
    monkeypatch.setattr(RouterService, "_capability_probe_failed", staticmethod(lambda *args, **kwargs: False))

    candidates = RouterService._append_recent_session_direct_candidate(
        FakeDb(provider, provider_model),
        [],
        sticky_key="会话A",
        model_name="测试模型",
        require_chat_completions=True,
    )

    assert len(candidates) == 1
    assert candidates[0].provider.id == 1
    assert candidates[0].provider_model.id == 11
    assert candidates[0].selection_reason == "recent_session_direct"
