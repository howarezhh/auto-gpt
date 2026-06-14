from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.health_service import HealthService
from app.services.model_catalog_service import ModelCatalogService
from app.services.probe_rate_limit_service import ProbeRateLimitService


def test_model_catalog_health_staggers_same_provider_models(monkeypatch) -> None:
    provider = SimpleNamespace(
        id=1,
        name="测试提供商",
        enabled=True,
        maintenance_mode_enabled=False,
        provider_models=[
            SimpleNamespace(id=11, provider_id=1, model_name="测试模型一", enabled=True),
            SimpleNamespace(id=12, provider_id=1, model_name="测试模型二", enabled=True),
        ],
    )
    catalogs = [
        SimpleNamespace(model_name="测试模型一"),
        SimpleNamespace(model_name="测试模型二"),
    ]
    active = 0
    max_active = 0
    calls: list[int] = []

    async def fake_model_checks(provider_arg, models_to_check, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        provider_model = models_to_check[0]
        calls.append(provider_model.id)
        await asyncio.sleep(0.05)
        active -= 1
        return [
            {
                "model_name": provider_model.model_name,
                "success": True,
                "provider_success": True,
                "health_status": "healthy",
                "latency_ms": 1,
                "status_code": 200,
                "message": "ok",
                "endpoint_results": [],
            }
        ]

    monkeypatch.setattr(HealthService, "PROVIDER_MODEL_PROBE_STAGGER_SECONDS", 0.01)
    monkeypatch.setattr(HealthService, "_run_provider_model_checks", staticmethod(fake_model_checks))

    results = asyncio.run(
        ModelCatalogService._probe_catalogs_health(
            catalogs,
            [provider],
            quick_text_only=True,
        )
    )

    assert calls == [11, 12]
    assert max_active == 2
    assert [item["catalog"].model_name for item in results] == ["测试模型一", "测试模型二"]
    assert all(len(item["channel_results"]) == 1 for item in results)


def test_scheduled_content_integrity_staggers_same_provider_models(monkeypatch) -> None:
    provider = SimpleNamespace(
        id=1,
        name="内容可信提供商",
        enabled=True,
        health_status="healthy",
        maintenance_mode_enabled=False,
        content_guard_enabled=True,
        provider_models=[
            SimpleNamespace(id=11, provider_id=1, model_name="可信模型一", enabled=True),
            SimpleNamespace(id=12, provider_id=1, model_name="可信模型二", enabled=True),
        ],
    )
    calls: list[int] = []

    def fake_should_run(_provider, _provider_model, *, setting):
        return True

    async def fake_run(provider_id, provider_model_id):
        calls.append(provider_model_id)
        return {
            "provider_id": provider_id,
            "provider_model_id": provider_model_id,
            "model_name": f"可信模型{provider_model_id}",
            "success": True,
            "endpoint_results": [],
        }

    async def fake_stagger(items, runner, *, stagger_seconds=None):
        assert [item.id for item in items] == [11, 12]
        results = []
        for item in items:
            results.append(await runner(item))
        return results

    monkeypatch.setattr(HealthService, "_should_run_scheduled_content_probe", staticmethod(fake_should_run))
    monkeypatch.setattr(HealthService, "_run_scheduled_content_trust_probe_for_model", staticmethod(fake_run))
    monkeypatch.setattr(HealthService, "_gather_staggered_by_previous_completion", staticmethod(fake_stagger))

    results = asyncio.run(
        HealthService._run_scheduled_content_trust_probes(
            SimpleNamespace(),
            [provider],
            setting=SimpleNamespace(content_guard_enabled=True),
        )
    )

    assert calls == [11, 12]
    assert results[0]["models_success"] == 2


def test_probe_rate_limit_uses_runtime_settings(monkeypatch) -> None:
    provider = SimpleNamespace(id=1, name="限频提供商")
    provider_model = SimpleNamespace(id=11, model_name="限频模型")

    monkeypatch.setattr(
        "app.services.probe_rate_limit_service.SettingService.get_cached",
        staticmethod(lambda: SimpleNamespace(probe_rate_limit_per_minute=3, probe_type_rate_limit_per_minute=2)),
    )
    monkeypatch.setattr(
        "app.services.probe_rate_limit_service.RedisService.get_client",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("测试使用本地限频"))),
    )
    monkeypatch.setattr("app.services.probe_rate_limit_service.get_settings", lambda: SimpleNamespace(is_production=lambda: False))
    ProbeRateLimitService._local_windows.clear()

    async def run_case():
        first = await ProbeRateLimitService.claim(provider, provider_model, probe_type="health_stream")
        second = await ProbeRateLimitService.claim(provider, provider_model, probe_type="health_stream")
        third = await ProbeRateLimitService.claim(provider, provider_model, probe_type="health_stream")
        other = await ProbeRateLimitService.claim(provider, provider_model, probe_type="content_guard_json")
        return first, second, third, other

    first, second, third, other = asyncio.run(run_case())

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.limit == 2
    assert other.allowed is True

    ProbeRateLimitService._local_windows.clear()
