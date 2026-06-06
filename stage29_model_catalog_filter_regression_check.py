from __future__ import annotations

from unittest.mock import patch

from app.services.model_catalog_service import ModelCatalogService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _FakeCatalog:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


class _FakeQuery:
    def order_by(self, *_args):
        return self


class _FakeDb:
    def __init__(self, catalogs: list[_FakeCatalog]) -> None:
        self.catalogs = catalogs

    def scalars(self, _query):
        return self.catalogs


def _model_health_filter_should_not_replace_enabled_filter() -> None:
    catalogs = [_FakeCatalog("健康停用模型"), _FakeCatalog("异常停用模型"), _FakeCatalog("健康启用模型")]
    filter_calls: list[dict] = []

    def fake_filter_query(**kwargs):
        filter_calls.append(kwargs)
        return _FakeQuery()

    def fake_serialize(catalog, _providers):
        health_map = {
            "健康停用模型": "healthy",
            "异常停用模型": "unhealthy",
            "健康启用模型": "healthy",
        }
        return {
            "model_name": catalog.model_name,
            "display_name": catalog.model_name,
            "enabled": "启用" in catalog.model_name,
            "health_status": health_map[catalog.model_name],
        }

    with (
        patch("app.services.model_catalog_service.ProviderService.list_providers", return_value=[]),
        patch.object(ModelCatalogService, "_model_filter_query", side_effect=fake_filter_query),
        patch.object(ModelCatalogService, "_serialize_catalog", side_effect=fake_serialize),
        patch.object(ModelCatalogService, "model_summary", return_value={}),
    ):
        result = ModelCatalogService.list_model_page(
            _FakeDb(catalogs),
            enabled=False,
            health_status="unhealthy",
            page=1,
            page_size=10,
        )

    _assert(filter_calls and filter_calls[0]["enabled"] is False, f"enabled filter was not forwarded: {filter_calls}")
    _assert(filter_calls[0]["provider_id"] is None, f"provider filter should stay independent: {filter_calls}")
    _assert(result["total"] == 1, f"health filter should keep only unhealthy models: {result}")
    _assert(result["items"][0]["model_name"] == "异常停用模型", f"unexpected filtered model: {result}")


def _invalid_model_health_filter_should_be_rejected() -> None:
    try:
        ModelCatalogService._normalize_model_health_filter("disabled")
    except ValueError:
        return
    raise AssertionError("invalid health filter value should raise ValueError")


def main() -> None:
    _model_health_filter_should_not_replace_enabled_filter()
    _invalid_model_health_filter_should_be_rejected()
    print("stage29 model catalog filter regression check passed")


if __name__ == "__main__":
    main()
