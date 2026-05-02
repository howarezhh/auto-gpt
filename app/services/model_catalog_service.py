from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.api_client_key import ApiClientKey
from app.models.api_key_policy_template import ApiKeyPolicyTemplate
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.user_account import UserAccount
from app.schemas.model_catalog import ModelCatalogCreate, ModelCatalogUpdate, ModelProviderBindingIn
from app.services.cache_service import CacheService
from app.services.provider_service import ProviderService
from app.utils.json_utils import dumps_json, loads_json


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
    def list_model_page(
        db: Session,
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        provider_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 10), 100)
        query = ModelCatalogService._model_filter_query(keyword=keyword, enabled=enabled, provider_id=provider_id)
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        catalogs = list(
            db.scalars(
                query.order_by(ModelCatalog.model_name.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        providers = ProviderService.list_providers(db)
        items = [ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs]
        return {
            "items": items,
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
            "summary": ModelCatalogService.model_summary(db),
        }

    @staticmethod
    def model_summary(db: Session) -> dict:
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        payloads = [ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs]
        return {
            "total": len(payloads),
            "enabled": sum(1 for item in payloads if item["enabled"]),
            "bound_providers": sum(item["provider_count"] for item in payloads),
            "enabled_providers": sum(item["enabled_provider_count"] for item in payloads),
        }

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
            supports_stream=payload.supports_stream,
            supports_vision=payload.supports_vision,
            supports_tools=payload.supports_tools,
            supports_chat_completions=payload.supports_chat_completions,
            supports_responses=payload.supports_responses,
            context_window_tokens=payload.context_window_tokens,
            max_input_tokens=payload.max_input_tokens,
            max_output_tokens=payload.max_output_tokens,
            input_price_per_1k=payload.input_price_per_1k,
            output_price_per_1k=payload.output_price_per_1k,
            cache_price_per_1k=(
                payload.cache_price_per_1k
                if payload.cache_price_per_1k is not None
                else payload.input_price_per_1k
            ),
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
        if "cache_price_per_1k" in data and data["cache_price_per_1k"] is None:
            data["cache_price_per_1k"] = data.get("input_price_per_1k", catalog.input_price_per_1k)
        for field, value in data.items():
            setattr(catalog, field, value)
        changed_price_fields = set(data) & {"input_price_per_1k", "output_price_per_1k", "cache_price_per_1k"}
        if changed_price_fields:
            ModelCatalogService._sync_provider_prices_from_catalog(db, catalog, price_fields=changed_price_fields)
        if {
            "supports_stream",
            "supports_vision",
            "supports_tools",
            "supports_chat_completions",
            "supports_responses",
            "context_window_tokens",
            "max_input_tokens",
            "max_output_tokens",
        } & set(data):
            ModelCatalogService._sync_provider_capabilities_from_catalog(db, catalog)
        if provider_bindings is not None:
            ModelCatalogService._apply_provider_bindings(db, catalog, provider_bindings)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        db.refresh(catalog)
        return catalog

    @staticmethod
    def batch_update_context_window(
        db: Session,
        *,
        model_names: list[str],
        context_window_tokens: int | None,
    ) -> list[ModelCatalog]:
        normalized_names = [item.strip() for item in model_names if isinstance(item, str) and item.strip()]
        if not normalized_names:
            raise ValueError("请选择要更新的模型")
        unique_names = list(dict.fromkeys(normalized_names))
        catalogs = list(
            db.scalars(
                select(ModelCatalog)
                .where(ModelCatalog.model_name.in_(unique_names))
                .order_by(ModelCatalog.model_name.asc())
            )
        )
        found_names = {catalog.model_name for catalog in catalogs}
        missing_names = [name for name in unique_names if name not in found_names]
        if missing_names:
            raise ValueError(f"模型不存在：{'、'.join(missing_names)}")
        for catalog in catalogs:
            catalog.context_window_tokens = context_window_tokens
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        for catalog in catalogs:
            db.refresh(catalog)
        return catalogs

    @staticmethod
    def delete_model(db: Session, catalog: ModelCatalog) -> None:
        model_name = catalog.model_name
        providers = ProviderService.list_providers(db)
        for provider in providers:
            for provider_model in list(provider.provider_models):
                if provider_model.model_name == model_name:
                    provider.provider_models.remove(provider_model)
            ProviderService.refresh_provider_state(provider)
        ModelCatalogService._remove_model_from_authorization_scopes(db, model_name)
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
            catalog_created = False
            if catalog is None:
                catalog = ModelCatalog(
                    model_name=model_name,
                    display_name=None,
                    enabled=True,
                    supports_stream=any(item.supports_stream for item in items),
                    supports_vision=any(item.supports_vision for item in items),
                    supports_tools=any(item.supports_tools for item in items),
                    supports_chat_completions=any(item.supports_chat_completions for item in items),
                    supports_responses=any(item.supports_responses for item in items),
                    context_window_tokens=ModelCatalogService._pick_max_int(items, field_name="context_window_tokens"),
                    max_input_tokens=ModelCatalogService._pick_max_int(items, field_name="max_input_tokens"),
                    max_output_tokens=ModelCatalogService._pick_max_int(items, field_name="max_output_tokens"),
                    input_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="input_price_per_1k"),
                    output_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="output_price_per_1k"),
                    cache_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="cache_price_per_1k"),
                )
                db.add(catalog)
                db.flush()
                catalogs[model_name] = catalog
                catalog_created = True
                changed = True

            for item in items:
                if item.supports_stream != catalog.supports_stream:
                    item.supports_stream = catalog.supports_stream
                    changed = True
                if item.supports_vision != catalog.supports_vision:
                    item.supports_vision = catalog.supports_vision
                    changed = True
                if item.supports_tools != catalog.supports_tools:
                    item.supports_tools = catalog.supports_tools
                    changed = True
                if item.supports_chat_completions != catalog.supports_chat_completions:
                    item.supports_chat_completions = catalog.supports_chat_completions
                    changed = True
                if item.supports_responses != catalog.supports_responses:
                    item.supports_responses = catalog.supports_responses
                    changed = True
                for field in ("context_window_tokens", "max_input_tokens", "max_output_tokens"):
                    if getattr(item, field) != getattr(catalog, field):
                        setattr(item, field, getattr(catalog, field))
                        changed = True
                if catalog_created:
                    derived_multiplier = ModelCatalogService._derive_multiplier(
                        base_input=catalog.input_price_per_1k,
                        direct_input=item.input_price_per_1k,
                        base_output=catalog.output_price_per_1k,
                        direct_output=item.output_price_per_1k,
                        base_cache=catalog.cache_price_per_1k,
                        direct_cache=item.cache_price_per_1k,
                        fallback=item.price_multiplier or 1.0,
                    )
                    if abs((item.price_multiplier or 1.0) - derived_multiplier) > 1e-9:
                        item.price_multiplier = derived_multiplier
                        changed = True
                if ModelCatalogService._sync_provider_model_shared_fields(item, catalog):
                    changed = True

        if changed:
            db.commit()
            ModelCatalogService.invalidate_model_runtime_cache()

    @staticmethod
    def list_user_models(db: Session, *, user: UserAccount) -> list[dict]:
        key_scopes = ModelCatalogService._collect_user_route_scopes(db, user=user)
        if not key_scopes:
            return []
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        payloads: list[dict] = []
        for catalog in catalogs:
            if not catalog.enabled:
                continue
            serialized = ModelCatalogService._serialize_catalog(catalog, providers)
            allowed_bindings = [
                binding
                for binding in serialized["provider_bindings"]
                if ModelCatalogService._is_binding_routable(binding)
                and ModelCatalogService._is_model_allowed_for_user_scope(
                    model_name=catalog.model_name,
                    provider_id=binding["provider_id"],
                    key_scopes=key_scopes,
                )
            ]
            filtered_names = [
                binding["provider_name"]
                for binding in allowed_bindings
            ]
            filtered_input_prices = [
                binding["effective_input_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_input_price_per_1k"] is not None
            ]
            filtered_output_prices = [
                binding["effective_output_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_output_price_per_1k"] is not None
            ]
            filtered_cache_prices = [
                binding["effective_cache_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_cache_price_per_1k"] is not None
            ]
            if not filtered_names:
                continue
            payloads.append(
                {
                    "model_name": catalog.model_name,
                    "display_name": catalog.display_name,
                    "speed_label": catalog.speed_label,
                    "remark": catalog.remark,
                    "supports_stream": catalog.supports_stream,
                    "supports_vision": catalog.supports_vision,
                    "supports_tools": catalog.supports_tools,
                    "supports_chat_completions": catalog.supports_chat_completions,
                    "supports_responses": catalog.supports_responses,
                    "context_window_tokens": catalog.context_window_tokens,
                    "max_input_tokens": catalog.max_input_tokens,
                    "max_output_tokens": catalog.max_output_tokens,
                    "input_price_per_1k": min(filtered_input_prices) if filtered_input_prices else catalog.input_price_per_1k,
                    "output_price_per_1k": min(filtered_output_prices) if filtered_output_prices else catalog.output_price_per_1k,
                    "cache_price_per_1k": (
                        min(filtered_cache_prices)
                        if filtered_cache_prices
                        else (
                            catalog.cache_price_per_1k
                            if catalog.cache_price_per_1k is not None
                            else catalog.input_price_per_1k
                        )
                    ),
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
    def _model_filter_query(
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        provider_id: int | None = None,
    ):
        query = select(ModelCatalog)
        if keyword:
            normalized = f"%{keyword.strip()}%"
            query = query.where(
                or_(
                    ModelCatalog.model_name.ilike(normalized),
                    ModelCatalog.display_name.ilike(normalized),
                    ModelCatalog.speed_label.ilike(normalized),
                    ModelCatalog.remark.ilike(normalized),
                    ModelCatalog.model_name.in_(
                        select(ProviderModel.model_name)
                        .join(Provider)
                        .where(Provider.name.ilike(normalized))
                    ),
                )
            )
        if enabled is not None:
            query = query.where(ModelCatalog.enabled.is_(enabled))
        if provider_id is not None and provider_id > 0:
            query = query.where(
                ModelCatalog.model_name.in_(
                    select(ProviderModel.model_name).where(ProviderModel.provider_id == provider_id)
                )
            )
        return query

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
            effective_cache = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=(
                    catalog.cache_price_per_1k
                    if catalog.cache_price_per_1k is not None
                    else catalog.input_price_per_1k
                ),
                direct_price_per_1k=provider_model.cache_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            bindings.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_enabled": provider.enabled,
                    "provider_health_status": provider.health_status,
                    "provider_circuit_state": provider.circuit_state,
                    "provider_maintenance_mode_enabled": provider.maintenance_mode_enabled,
                    "bound": provider_model is not None,
                    "enabled": provider_model.enabled if provider_model else False,
                    "priority": provider_model.priority if provider_model else 100,
                    "weight": provider_model.weight if provider_model else 100,
                    "price_multiplier": provider_model.price_multiplier if provider_model else 1.0,
                    "model_health_status": provider_model.health_status if provider_model else None,
                    "model_circuit_state": provider_model.circuit_state if provider_model else None,
                    "effective_input_price_per_1k": effective_input,
                    "effective_output_price_per_1k": effective_output,
                    "effective_cache_price_per_1k": effective_cache,
                    "direct_input_price_per_1k": provider_model.input_price_per_1k if provider_model else None,
                    "direct_output_price_per_1k": provider_model.output_price_per_1k if provider_model else None,
                    "direct_cache_price_per_1k": provider_model.cache_price_per_1k if provider_model else None,
                }
            )

        active_bindings = [item for item in bindings if item["bound"]]
        enabled_bindings = [item for item in active_bindings if ModelCatalogService._is_binding_routable(item)]
        input_prices = [item["effective_input_price_per_1k"] for item in enabled_bindings if item["effective_input_price_per_1k"] is not None]
        output_prices = [item["effective_output_price_per_1k"] for item in enabled_bindings if item["effective_output_price_per_1k"] is not None]
        cache_prices = [item["effective_cache_price_per_1k"] for item in enabled_bindings if item["effective_cache_price_per_1k"] is not None]
        bound_multipliers = [item["price_multiplier"] for item in active_bindings]
        routable_multipliers = [item["price_multiplier"] for item in enabled_bindings]
        avg_bound_price_multiplier = ModelCatalogService._average_multiplier(bound_multipliers)
        avg_routable_price_multiplier = ModelCatalogService._average_multiplier(routable_multipliers)
        return {
            "id": catalog.id,
            "model_name": catalog.model_name,
            "display_name": catalog.display_name,
            "enabled": catalog.enabled,
            "supports_stream": catalog.supports_stream,
            "supports_vision": catalog.supports_vision,
            "supports_tools": catalog.supports_tools,
            "supports_chat_completions": catalog.supports_chat_completions,
            "supports_responses": catalog.supports_responses,
            "context_window_tokens": catalog.context_window_tokens,
            "max_input_tokens": catalog.max_input_tokens,
            "max_output_tokens": catalog.max_output_tokens,
            "input_price_per_1k": catalog.input_price_per_1k,
            "output_price_per_1k": catalog.output_price_per_1k,
            "cache_price_per_1k": catalog.cache_price_per_1k,
            "speed_label": catalog.speed_label,
            "remark": catalog.remark,
            "provider_count": len(active_bindings),
            "enabled_provider_count": len(enabled_bindings),
            "lowest_input_price_per_1k": min(input_prices) if input_prices else catalog.input_price_per_1k,
            "lowest_output_price_per_1k": min(output_prices) if output_prices else catalog.output_price_per_1k,
            "lowest_cache_price_per_1k": (
                min(cache_prices)
                if cache_prices
                else (
                    catalog.cache_price_per_1k
                    if catalog.cache_price_per_1k is not None
                    else catalog.input_price_per_1k
                )
            ),
            "avg_price_multiplier": avg_bound_price_multiplier,
            "avg_bound_price_multiplier": avg_bound_price_multiplier,
            "avg_routable_price_multiplier": avg_routable_price_multiplier,
            "bound_price_multiplier_count": len(bound_multipliers),
            "routable_price_multiplier_count": len(routable_multipliers),
            "min_bound_price_multiplier": min(bound_multipliers) if bound_multipliers else None,
            "max_bound_price_multiplier": max(bound_multipliers) if bound_multipliers else None,
            "available_provider_names": [item["provider_name"] for item in enabled_bindings],
            "provider_bindings": bindings,
            "created_at": catalog.created_at,
            "updated_at": catalog.updated_at,
        }

    @staticmethod
    def _average_multiplier(multipliers: list[float]) -> float | None:
        return round(sum(multipliers) / len(multipliers), 4) if multipliers else None

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
            provider_model.supports_stream = catalog.supports_stream
            provider_model.supports_vision = catalog.supports_vision
            provider_model.supports_tools = catalog.supports_tools
            provider_model.supports_chat_completions = catalog.supports_chat_completions
            provider_model.supports_responses = catalog.supports_responses
            provider_model.context_window_tokens = catalog.context_window_tokens
            provider_model.max_input_tokens = catalog.max_input_tokens
            provider_model.max_output_tokens = catalog.max_output_tokens
            provider_model.priority = binding.priority
            provider_model.weight = binding.weight
            provider_model.price_multiplier = binding.price_multiplier
            if catalog.input_price_per_1k is not None:
                provider_model.input_price_per_1k = catalog.input_price_per_1k * binding.price_multiplier
            if catalog.output_price_per_1k is not None:
                provider_model.output_price_per_1k = catalog.output_price_per_1k * binding.price_multiplier
            if catalog.cache_price_per_1k is not None:
                provider_model.cache_price_per_1k = catalog.cache_price_per_1k * binding.price_multiplier
            elif catalog.input_price_per_1k is not None:
                provider_model.cache_price_per_1k = catalog.input_price_per_1k * binding.price_multiplier
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
    def _sync_provider_prices_from_catalog(db: Session, catalog: ModelCatalog, *, price_fields: set[str] | None = None) -> None:
        fields = price_fields or {"input_price_per_1k", "output_price_per_1k", "cache_price_per_1k"}
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .where(ProviderModel.model_name == catalog.model_name)
            )
        )
        for provider_model in provider_models:
            if "input_price_per_1k" in fields:
                if catalog.input_price_per_1k is not None:
                    provider_model.input_price_per_1k = catalog.input_price_per_1k * provider_model.price_multiplier
                else:
                    provider_model.input_price_per_1k = None
            if "output_price_per_1k" in fields:
                if catalog.output_price_per_1k is not None:
                    provider_model.output_price_per_1k = catalog.output_price_per_1k * provider_model.price_multiplier
                else:
                    provider_model.output_price_per_1k = None
            if "cache_price_per_1k" in fields:
                source_cache_price = catalog.cache_price_per_1k
                if source_cache_price is None:
                    source_cache_price = catalog.input_price_per_1k
                if source_cache_price is not None:
                    provider_model.cache_price_per_1k = source_cache_price * provider_model.price_multiplier
                else:
                    provider_model.cache_price_per_1k = None

    @staticmethod
    def _sync_provider_model_shared_fields(provider_model: ProviderModel, catalog: ModelCatalog) -> bool:
        changed = False
        if provider_model.supports_stream != catalog.supports_stream:
            provider_model.supports_stream = catalog.supports_stream
            changed = True
        if provider_model.supports_vision != catalog.supports_vision:
            provider_model.supports_vision = catalog.supports_vision
            changed = True
        if provider_model.supports_tools != catalog.supports_tools:
            provider_model.supports_tools = catalog.supports_tools
            changed = True
        for field in (
            "supports_chat_completions",
            "supports_responses",
            "context_window_tokens",
            "max_input_tokens",
            "max_output_tokens",
        ):
            if getattr(provider_model, field) != getattr(catalog, field):
                setattr(provider_model, field, getattr(catalog, field))
                changed = True
        expected_input = (
            catalog.input_price_per_1k * provider_model.price_multiplier
            if catalog.input_price_per_1k is not None
            else None
        )
        expected_output = (
            catalog.output_price_per_1k * provider_model.price_multiplier
            if catalog.output_price_per_1k is not None
            else None
        )
        catalog_cache_price = catalog.cache_price_per_1k
        if catalog_cache_price is None:
            catalog_cache_price = catalog.input_price_per_1k
        expected_cache = (
            catalog_cache_price * provider_model.price_multiplier
            if catalog_cache_price is not None
            else None
        )
        for field, expected in (
            ("input_price_per_1k", expected_input),
            ("output_price_per_1k", expected_output),
            ("cache_price_per_1k", expected_cache),
        ):
            current = getattr(provider_model, field)
            if not ModelCatalogService._nullable_float_equal(current, expected):
                setattr(provider_model, field, expected)
                changed = True
        return changed

    @staticmethod
    def _sync_provider_capabilities_from_catalog(db: Session, catalog: ModelCatalog) -> None:
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .where(ProviderModel.model_name == catalog.model_name)
            )
        )
        for provider_model in provider_models:
            provider_model.supports_stream = catalog.supports_stream
            provider_model.supports_vision = catalog.supports_vision
            provider_model.supports_tools = catalog.supports_tools
            provider_model.supports_chat_completions = catalog.supports_chat_completions
            provider_model.supports_responses = catalog.supports_responses
            provider_model.context_window_tokens = catalog.context_window_tokens
            provider_model.max_input_tokens = catalog.max_input_tokens
            provider_model.max_output_tokens = catalog.max_output_tokens

    @staticmethod
    def _nullable_float_equal(left: float | None, right: float | None) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return abs(left - right) <= 1e-12

    @staticmethod
    def _pick_base_price(provider_models: list[ProviderModel], *, field_name: str) -> float | None:
        values = [getattr(item, field_name) for item in provider_models if getattr(item, field_name) is not None]
        return min(values) if values else None

    @staticmethod
    def _pick_max_int(provider_models: list[ProviderModel], *, field_name: str) -> int | None:
        values = [int(getattr(item, field_name)) for item in provider_models if getattr(item, field_name) is not None]
        return max(values) if values else None

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
        base_cache: float | None,
        direct_cache: float | None,
        fallback: float,
    ) -> float:
        if base_input and direct_input is not None:
            return max(direct_input / base_input, 0.000001)
        if base_output and direct_output is not None:
            return max(direct_output / base_output, 0.000001)
        if base_cache and direct_cache is not None:
            return max(direct_cache / base_cache, 0.000001)
        return max(fallback or 1.0, 0.000001)

    @staticmethod
    def _collect_user_route_scopes(db: Session, *, user: UserAccount) -> list[dict]:
        owned_keys = list(
            db.scalars(
                select(ApiClientKey)
                .options(selectinload(ApiClientKey.provider_bindings))
                .where(ApiClientKey.owner_user_id == user.id, ApiClientKey.enabled.is_(True))
            )
        )
        scopes = []
        for item in owned_keys:
            provider_ids = {binding.provider_id for binding in item.provider_bindings}
            if item.default_provider_id is not None:
                provider_ids.add(item.default_provider_id)
            if not provider_ids:
                continue
            scopes.append(
                {
                    "provider_ids": provider_ids,
                    "allowed_model_names": set(loads_json(item.allowed_model_names_json, [])),
                }
            )
        return scopes

    @staticmethod
    def _is_model_allowed_for_user_scope(*, model_name: str, provider_id: int, key_scopes: list[dict]) -> bool:
        for scope in key_scopes:
            if provider_id not in scope["provider_ids"]:
                continue
            allowed_model_names = scope["allowed_model_names"]
            if not allowed_model_names or model_name in allowed_model_names:
                return True
        return False

    @staticmethod
    def _remove_model_from_authorization_scopes(db: Session, model_name: str) -> None:
        from app.services.api_key_auth_cache import ApiKeyAuthCache

        api_keys = list(db.scalars(select(ApiClientKey)))
        for api_key in api_keys:
            allowed_model_names = list(loads_json(api_key.allowed_model_names_json, []))
            if model_name not in allowed_model_names:
                continue
            api_key.allowed_model_names_json = dumps_json(
                [item for item in allowed_model_names if item != model_name]
            )
            ApiKeyAuthCache.invalidate_api_key(api_key.id, api_key.key_hash)
            ApiKeyAuthCache.invalidate_user(api_key.owner_user_id)

        templates = list(db.scalars(select(ApiKeyPolicyTemplate)))
        for template in templates:
            allowed_model_names = list(loads_json(template.allowed_model_names_json, []))
            if model_name not in allowed_model_names:
                continue
            template.allowed_model_names_json = dumps_json(
                [item for item in allowed_model_names if item != model_name]
            )

    @staticmethod
    def _is_binding_routable(binding: dict) -> bool:
        return (
            binding["bound"]
            and binding["enabled"]
            and binding["provider_enabled"]
            and not binding.get("provider_maintenance_mode_enabled")
            and binding.get("provider_circuit_state") != "open"
            and binding.get("model_circuit_state") != "open"
            and binding.get("model_health_status") != "unhealthy"
        )
