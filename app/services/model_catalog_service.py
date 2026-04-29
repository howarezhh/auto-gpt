from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.api_client_key import ApiClientKey
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.user_account import UserAccount
from app.schemas.model_catalog import ModelCatalogCreate, ModelCatalogUpdate, ModelProviderBindingIn
from app.services.cache_service import CacheService
from app.services.provider_service import ProviderService


class ModelCatalogService:
    @staticmethod
    def list_catalogs(db: Session) -> list[ModelCatalog]:
        return list(db.scalars(select(ModelCatalog).order_by(ModelCatalog.model_name.asc())))

    @staticmethod
    def get_catalog(db: Session, model_name: str) -> ModelCatalog | None:
        return db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == model_name))

    @staticmethod
    def list_model_dicts(db: Session) -> list[dict]:
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        return [ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs]

    @staticmethod
    def get_model_detail(db: Session, model_name: str) -> dict | None:
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        catalog = next((item for item in catalogs if item.model_name == model_name), None)
        if catalog is None:
            return None
        return ModelCatalogService._serialize_catalog(catalog, providers, include_all_providers=True)

    @staticmethod
    def create_model(db: Session, payload: ModelCatalogCreate) -> ModelCatalog:
        if ModelCatalogService.get_catalog(db, payload.model_name) is not None:
            raise ValueError("模型已存在")
        catalog = ModelCatalog(
            model_name=payload.model_name,
            display_name=payload.display_name,
            enabled=payload.enabled,
            input_price_per_1k=payload.input_price_per_1k,
            output_price_per_1k=payload.output_price_per_1k,
            speed_label=payload.speed_label,
            remark=payload.remark,
        )
        db.add(catalog)
        db.flush()
        ModelCatalogService._apply_provider_bindings(db, catalog, payload.provider_bindings)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        db.refresh(catalog)
        return catalog

    @staticmethod
    def update_model(db: Session, catalog: ModelCatalog, payload: ModelCatalogUpdate) -> ModelCatalog:
        data = payload.model_dump(exclude_unset=True, exclude={"provider_bindings"})
        provider_bindings = payload.provider_bindings if "provider_bindings" in payload.model_fields_set else None
        for field, value in data.items():
            setattr(catalog, field, value)
        if data:
            ModelCatalogService._sync_provider_prices_from_catalog(db, catalog)
        if provider_bindings is not None:
            ModelCatalogService._apply_provider_bindings(db, catalog, provider_bindings)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        db.refresh(catalog)
        return catalog

    @staticmethod
    def delete_model(db: Session, catalog: ModelCatalog) -> None:
        providers = ProviderService.list_providers(db)
        for provider in providers:
            for provider_model in list(provider.provider_models):
                if provider_model.model_name == catalog.model_name:
                    provider.provider_models.remove(provider_model)
            ProviderService.refresh_provider_state(provider)
        db.delete(catalog)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()

    @staticmethod
    def sync_model_catalogs(db: Session) -> None:
        catalogs = {item.model_name: item for item in ModelCatalogService.list_catalogs(db)}
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .options(selectinload(ProviderModel.provider))
                .order_by(ProviderModel.model_name.asc(), ProviderModel.provider_id.asc())
            )
        )
        grouped: dict[str, list[ProviderModel]] = defaultdict(list)
        for item in provider_models:
            grouped[item.model_name].append(item)

        changed = False
        for model_name, items in grouped.items():
            catalog = catalogs.get(model_name)
            if catalog is None:
                catalog = ModelCatalog(
                    model_name=model_name,
                    display_name=None,
                    enabled=True,
                    input_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="input_price_per_1k"),
                    output_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="output_price_per_1k"),
                )
                db.add(catalog)
                db.flush()
                catalogs[model_name] = catalog
                changed = True

            for item in items:
                derived_multiplier = ModelCatalogService._derive_multiplier(
                    base_input=catalog.input_price_per_1k,
                    direct_input=item.input_price_per_1k,
                    base_output=catalog.output_price_per_1k,
                    direct_output=item.output_price_per_1k,
                    fallback=item.price_multiplier or 1.0,
                )
                if abs((item.price_multiplier or 1.0) - derived_multiplier) > 1e-9:
                    item.price_multiplier = derived_multiplier
                    changed = True

        if changed:
            db.commit()
            ModelCatalogService.invalidate_model_runtime_cache()

    @staticmethod
    def list_user_models(db: Session, *, user: UserAccount) -> list[dict]:
        provider_ids = ModelCatalogService._collect_user_provider_ids(db, user=user)
        if not provider_ids:
            return []
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        allowed_provider_ids = set(provider_ids)
        payloads: list[dict] = []
        for catalog in catalogs:
            if not catalog.enabled:
                continue
            serialized = ModelCatalogService._serialize_catalog(catalog, providers)
            filtered_names = [
                binding["provider_name"]
                for binding in serialized["provider_bindings"]
                if binding["bound"] and binding["enabled"] and binding["provider_enabled"] and binding["provider_id"] in allowed_provider_ids
            ]
            filtered_input_prices = [
                binding["effective_input_price_per_1k"]
                for binding in serialized["provider_bindings"]
                if binding["bound"] and binding["enabled"] and binding["provider_enabled"] and binding["provider_id"] in allowed_provider_ids and binding["effective_input_price_per_1k"] is not None
            ]
            filtered_output_prices = [
                binding["effective_output_price_per_1k"]
                for binding in serialized["provider_bindings"]
                if binding["bound"] and binding["enabled"] and binding["provider_enabled"] and binding["provider_id"] in allowed_provider_ids and binding["effective_output_price_per_1k"] is not None
            ]
            if not filtered_names:
                continue
            payloads.append(
                {
                    "model_name": catalog.model_name,
                    "display_name": catalog.display_name,
                    "speed_label": catalog.speed_label,
                    "remark": catalog.remark,
                    "input_price_per_1k": min(filtered_input_prices) if filtered_input_prices else catalog.input_price_per_1k,
                    "output_price_per_1k": min(filtered_output_prices) if filtered_output_prices else catalog.output_price_per_1k,
                    "available_provider_names": filtered_names,
                    "enabled_provider_count": len(filtered_names),
                }
            )
        return payloads

    @staticmethod
    def enabled_model_name_set(db: Session) -> set[str]:
        return set(db.scalars(select(ModelCatalog.model_name).where(ModelCatalog.enabled.is_(True))))

    @staticmethod
    def invalidate_model_runtime_cache() -> None:
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")

    @staticmethod
    def _load_catalogs_and_providers(db: Session) -> tuple[list[ModelCatalog], list[Provider]]:
        catalogs = ModelCatalogService.list_catalogs(db)
        providers = ProviderService.list_providers(db)
        return catalogs, providers

    @staticmethod
    def _serialize_catalog(catalog: ModelCatalog, providers: list[Provider], *, include_all_providers: bool = False) -> dict:
        provider_model_map = {
            provider.id: next((item for item in provider.provider_models if item.model_name == catalog.model_name), None)
            for provider in providers
        }
        bindings = []
        for provider in providers:
            provider_model = provider_model_map.get(provider.id)
            if provider_model is None and not include_all_providers:
                continue
            effective_input = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=catalog.input_price_per_1k,
                direct_price_per_1k=provider_model.input_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            effective_output = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=catalog.output_price_per_1k,
                direct_price_per_1k=provider_model.output_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            bindings.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_enabled": provider.enabled,
                    "provider_health_status": provider.health_status,
                    "bound": provider_model is not None,
                    "enabled": provider_model.enabled if provider_model else False,
                    "priority": provider_model.priority if provider_model else 100,
                    "weight": provider_model.weight if provider_model else 100,
                    "price_multiplier": provider_model.price_multiplier if provider_model else 1.0,
                    "model_health_status": provider_model.health_status if provider_model else None,
                    "effective_input_price_per_1k": effective_input,
                    "effective_output_price_per_1k": effective_output,
                    "direct_input_price_per_1k": provider_model.input_price_per_1k if provider_model else None,
                    "direct_output_price_per_1k": provider_model.output_price_per_1k if provider_model else None,
                }
            )

        active_bindings = [item for item in bindings if item["bound"]]
        enabled_bindings = [item for item in active_bindings if item["enabled"] and item["provider_enabled"]]
        input_prices = [item["effective_input_price_per_1k"] for item in enabled_bindings if item["effective_input_price_per_1k"] is not None]
        output_prices = [item["effective_output_price_per_1k"] for item in enabled_bindings if item["effective_output_price_per_1k"] is not None]
        multipliers = [item["price_multiplier"] for item in active_bindings]
        return {
            "id": catalog.id,
            "model_name": catalog.model_name,
            "display_name": catalog.display_name,
            "enabled": catalog.enabled,
            "input_price_per_1k": catalog.input_price_per_1k,
            "output_price_per_1k": catalog.output_price_per_1k,
            "speed_label": catalog.speed_label,
            "remark": catalog.remark,
            "provider_count": len(active_bindings),
            "enabled_provider_count": len(enabled_bindings),
            "lowest_input_price_per_1k": min(input_prices) if input_prices else catalog.input_price_per_1k,
            "lowest_output_price_per_1k": min(output_prices) if output_prices else catalog.output_price_per_1k,
            "avg_price_multiplier": round(sum(multipliers) / len(multipliers), 4) if multipliers else None,
            "available_provider_names": [item["provider_name"] for item in enabled_bindings],
            "provider_bindings": bindings,
            "created_at": catalog.created_at,
            "updated_at": catalog.updated_at,
        }

    @staticmethod
    def _apply_provider_bindings(db: Session, catalog: ModelCatalog, bindings: list[ModelProviderBindingIn]) -> None:
        providers = ProviderService.list_providers(db)
        provider_map = {item.id: item for item in providers}
        existing_map = {
            provider.id: next((item for item in provider.provider_models if item.model_name == catalog.model_name), None)
            for provider in providers
        }
        processed_provider_ids: set[int] = set()

        for binding in bindings:
            provider = provider_map.get(binding.provider_id)
            if provider is None:
                raise ValueError(f"中转站不存在: {binding.provider_id}")
            processed_provider_ids.add(provider.id)
            provider_model = existing_map.get(provider.id)
            if not binding.bound:
                if provider_model is not None:
                    provider.provider_models.remove(provider_model)
                    ProviderService.refresh_provider_state(provider)
                continue

            if provider_model is None:
                provider_model = ProviderModel(provider=provider, model_name=catalog.model_name)
                db.add(provider_model)
            provider_model.enabled = binding.enabled
            provider_model.priority = binding.priority
            provider_model.weight = binding.weight
            provider_model.price_multiplier = binding.price_multiplier
            if catalog.input_price_per_1k is not None:
                provider_model.input_price_per_1k = catalog.input_price_per_1k * binding.price_multiplier
            if catalog.output_price_per_1k is not None:
                provider_model.output_price_per_1k = catalog.output_price_per_1k * binding.price_multiplier
            ProviderService.refresh_provider_state(provider)

        for provider in providers:
            if provider.id in processed_provider_ids:
                continue
            provider_model = existing_map.get(provider.id)
            if provider_model is None:
                continue
            provider.provider_models.remove(provider_model)
            ProviderService.refresh_provider_state(provider)

    @staticmethod
    def _sync_provider_prices_from_catalog(db: Session, catalog: ModelCatalog) -> None:
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .where(ProviderModel.model_name == catalog.model_name)
            )
        )
        for provider_model in provider_models:
            if catalog.input_price_per_1k is not None:
                provider_model.input_price_per_1k = catalog.input_price_per_1k * provider_model.price_multiplier
            else:
                provider_model.input_price_per_1k = None
            if catalog.output_price_per_1k is not None:
                provider_model.output_price_per_1k = catalog.output_price_per_1k * provider_model.price_multiplier
            else:
                provider_model.output_price_per_1k = None

    @staticmethod
    def _pick_base_price(provider_models: list[ProviderModel], *, field_name: str) -> float | None:
        values = [getattr(item, field_name) for item in provider_models if getattr(item, field_name) is not None]
        return min(values) if values else None

    @staticmethod
    def _effective_price_per_1k(
        *,
        base_price_per_1k: float | None,
        direct_price_per_1k: float | None,
        price_multiplier: float,
    ) -> float | None:
        if base_price_per_1k is not None:
            return base_price_per_1k * price_multiplier
        return direct_price_per_1k

    @staticmethod
    def _derive_multiplier(
        *,
        base_input: float | None,
        direct_input: float | None,
        base_output: float | None,
        direct_output: float | None,
        fallback: float,
    ) -> float:
        if base_input and direct_input is not None:
            return max(direct_input / base_input, 0.000001)
        if base_output and direct_output is not None:
            return max(direct_output / base_output, 0.000001)
        return max(fallback or 1.0, 0.000001)

    @staticmethod
    def _collect_user_provider_ids(db: Session, *, user: UserAccount) -> list[int]:
        owned_keys = list(
            db.scalars(
                select(ApiClientKey)
                .options(selectinload(ApiClientKey.provider_bindings))
                .where(ApiClientKey.owner_user_id == user.id, ApiClientKey.enabled.is_(True))
            )
        )
        provider_ids: set[int] = set()
        for item in owned_keys:
            provider_ids.update(binding.provider_id for binding in item.provider_bindings)
            if item.default_provider_id is not None:
                provider_ids.add(item.default_provider_id)
        return sorted(provider_ids)
