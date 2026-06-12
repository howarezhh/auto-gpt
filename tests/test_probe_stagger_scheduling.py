from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.health_service import HealthService
from app.services.model_catalog_service import ModelCatalogService


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
