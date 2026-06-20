import asyncio
from types import SimpleNamespace

from app.routers.models import select_model_mapping_target
from app.schemas.model_mapping import ModelMappingSelectionProbe
from app.services.model_mapping_service import ModelMappingService


def test_model_mapping_preview_returns_selected_target_trace(monkeypatch) -> None:
    async def fake_resolve_for_request(**kwargs):
        assert kwargs["source_model_name"] == "gpt-source"
        assert kwargs["sticky_key"] == "gpt-source"
        return SimpleNamespace(
            source_model_name="gpt-source",
            selected_model_name="gpt-target",
            trace={
                "result": "model_mapping_selected",
                "selection_reason": "target_config_order",
                "targets": [{"model_name": "gpt-target", "available": True}],
            },
        )

    monkeypatch.setattr(ModelMappingService, "resolve_for_request", staticmethod(fake_resolve_for_request))

    result = asyncio.run(
        select_model_mapping_target(
            ModelMappingSelectionProbe(source_model_name="gpt-source"),
            db=object(),
        )
    )

    assert result.mapped is True
    assert result.source_model_name == "gpt-source"
    assert result.selected_model_name == "gpt-target"
    assert result.trace["result"] == "model_mapping_selected"
    assert result.trace["targets"][0]["available"] is True


def test_model_mapping_preview_returns_trace_when_mapping_not_applied(monkeypatch) -> None:
    async def fake_resolve_for_request(**kwargs):
        return None

    monkeypatch.setattr(ModelMappingService, "resolve_for_request", staticmethod(fake_resolve_for_request))
    monkeypatch.setattr(
        ModelMappingService,
        "get_mapping",
        staticmethod(lambda db, source_model_name: SimpleNamespace(id=7, enabled=False)),
    )

    result = asyncio.run(
        select_model_mapping_target(
            ModelMappingSelectionProbe(source_model_name="gpt-source"),
            db=object(),
        )
    )

    assert result.mapped is False
    assert result.selected_model_name == "gpt-source"
    assert result.trace == {
        "result": "model_mapping_not_applied",
        "reason": "model_mapping_disabled_or_no_available_target",
        "source_model_name": "gpt-source",
        "mapping_id": 7,
        "mapping_enabled": False,
    }
