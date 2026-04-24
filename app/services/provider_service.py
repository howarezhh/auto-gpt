from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.provider import ProviderCreate, ProviderModelConfigInput, ProviderModelConfigUpdate, ProviderUpdate
from app.utils.json_utils import dumps_json, loads_json


class ProviderService:
    @staticmethod
    def _infer_model_capabilities(model_name: str) -> dict[str, bool]:
        normalized = (model_name or "").strip().lower()
        supports_vision = any(prefix in normalized for prefix in ("gpt-4o", "gpt-4.1", "gpt-5"))
        return {
            "supports_stream": True,
            "supports_vision": supports_vision,
        }

    @staticmethod
    def _build_model_config_input_from_name(model_name: str) -> ProviderModelConfigInput:
        capabilities = ProviderService._infer_model_capabilities(model_name)
        return ProviderModelConfigInput(
            model_name=model_name,
            supports_stream=capabilities["supports_stream"],
            supports_vision=capabilities["supports_vision"],
        )

    @staticmethod
    def mask_api_key(api_key: str) -> str:
        if len(api_key) <= 8:
            return "******"
        return f"{api_key[:4]}...{api_key[-4:]}"

    @staticmethod
    def list_providers(db: Session) -> list[Provider]:
        return list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .order_by(Provider.priority.asc(), Provider.id.asc())
            )
        )

    @staticmethod
    def get_provider(db: Session, provider_id: int) -> Provider | None:
        return db.scalar(
            select(Provider).options(selectinload(Provider.provider_models)).where(Provider.id == provider_id)
        )

    @staticmethod
    def create_provider(db: Session, payload: ProviderCreate) -> Provider:
        provider = Provider(
            name=payload.name,
            base_url=payload.base_url.rstrip("/"),
            api_key=payload.api_key,
            provider_type=payload.provider_type,
            enabled=payload.enabled,
            priority=payload.priority,
            weight=payload.weight,
            timeout_ms=payload.timeout_ms,
            max_retries=payload.max_retries,
            remark=payload.remark,
        )
        db.add(provider)
        db.flush()
        ProviderService._replace_provider_models(db, provider, ProviderService._resolve_model_configs(payload))
        ProviderService.refresh_provider_state(provider)
        db.commit()
        db.refresh(provider)
        return provider

    @staticmethod
    def update_provider(db: Session, provider: Provider, payload: ProviderUpdate) -> Provider:
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if field in {"models", "model_configs"}:
                continue
            if field == "base_url" and isinstance(value, str):
                value = value.rstrip("/")
            setattr(provider, field, value)

        if "models" in data or "model_configs" in data:
            ProviderService._replace_provider_models(db, provider, ProviderService._resolve_model_configs(payload, provider))

        if {"base_url", "api_key"} & set(data.keys()):
            provider.health_status = "unknown"
            provider.circuit_state = "closed"
            for provider_model in provider.provider_models:
                provider_model.health_status = "unknown"
                provider_model.last_error = None

        ProviderService.refresh_provider_state(provider)
        db.commit()
        db.refresh(provider)
        return provider

    @staticmethod
    def delete_provider(db: Session, provider: Provider) -> None:
        db.delete(provider)
        db.commit()

    @staticmethod
    def update_provider_model(
        db: Session,
        provider: Provider,
        provider_model_id: int,
        payload: ProviderModelConfigUpdate,
    ) -> ProviderModel:
        provider_model = next((item for item in provider.provider_models if item.id == provider_model_id), None)
        if provider_model is None:
            raise ValueError("Provider model not found")

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(provider_model, field, value)

        ProviderService.refresh_provider_state(provider)
        db.commit()
        db.refresh(provider_model)
        return provider_model

    @staticmethod
    def provider_to_dict(provider: Provider) -> dict:
        return {
            "id": provider.id,
            "name": provider.name,
            "base_url": provider.base_url,
            "api_key_masked": ProviderService.mask_api_key(provider.api_key),
            "provider_type": provider.provider_type,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "weight": provider.weight,
            "timeout_ms": provider.timeout_ms,
            "max_retries": provider.max_retries,
            "models": [item.model_name for item in provider.provider_models],
            "model_configs": [
                {
                    "id": item.id,
                    "model_name": item.model_name,
                    "enabled": item.enabled,
                    "priority": item.priority,
                    "weight": item.weight,
                    "health_status": item.health_status,
                    "circuit_state": item.circuit_state,
                    "circuit_opened_at": item.circuit_opened_at,
                    "last_check_at": item.last_check_at,
                    "last_latency_ms": item.last_latency_ms,
                    "failure_count": item.failure_count,
                    "success_count": item.success_count,
                    "last_error": item.last_error,
                    "supports_stream": item.supports_stream,
                    "supports_vision": item.supports_vision,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in provider.provider_models
            ],
            "health_status": provider.health_status,
            "last_check_at": provider.last_check_at,
            "last_latency_ms": provider.last_latency_ms,
            "failure_count": provider.failure_count,
            "success_count": provider.success_count,
            "circuit_state": provider.circuit_state,
            "remark": provider.remark,
            "created_at": provider.created_at,
            "updated_at": provider.updated_at,
        }

    @staticmethod
    def sync_legacy_provider_models(db: Session) -> None:
        providers = list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .order_by(Provider.id.asc())
            )
        )
        changed = False
        for provider in providers:
            if provider.provider_models:
                for provider_model in provider.provider_models:
                    inferred = ProviderService._infer_model_capabilities(provider_model.model_name)
                    if inferred["supports_stream"] and not provider_model.supports_stream:
                        provider_model.supports_stream = True
                        changed = True
                    if inferred["supports_vision"] and not provider_model.supports_vision:
                        provider_model.supports_vision = True
                        changed = True
                ProviderService._sync_models_json(provider)
                continue
            legacy_models = loads_json(provider.models_json, [])
            if not legacy_models:
                continue
            ProviderService._replace_provider_models(
                db,
                provider,
                [ProviderService._build_model_config_input_from_name(model_name) for model_name in legacy_models],
            )
            ProviderService.refresh_provider_state(provider)
            changed = True
        if changed:
            db.commit()

    @staticmethod
    def refresh_provider_state(provider: Provider) -> None:
        enabled_models = [item for item in provider.provider_models if item.enabled]
        ProviderService._sync_models_json(provider)
        provider.last_check_at = max((item.last_check_at for item in enabled_models if item.last_check_at), default=None)
        provider.last_latency_ms = next(
            (item.last_latency_ms for item in enabled_models if item.last_latency_ms is not None),
            provider.last_latency_ms,
        )

        if not provider.enabled or not enabled_models:
            provider.health_status = "unknown"
            provider.circuit_state = "closed"
            return

        statuses = {item.health_status for item in enabled_models}
        if statuses == {"healthy"}:
            provider.health_status = "healthy"
            provider.circuit_state = "closed"
            return
        if statuses == {"unhealthy"} and all(item.circuit_state == "open" for item in enabled_models):
            provider.health_status = "unhealthy"
            provider.circuit_state = "open"
            return
        if "healthy" in statuses or "degraded" in statuses:
            provider.health_status = "degraded"
            provider.circuit_state = "closed"
            return

        provider.health_status = "unknown"
        provider.circuit_state = "closed"

    @staticmethod
    def _resolve_model_configs(payload: ProviderCreate | ProviderUpdate, provider: Provider | None = None) -> list[ProviderModelConfigInput]:
        if getattr(payload, "model_configs", None):
            return list(payload.model_configs)
        models = getattr(payload, "models", None)
        if models is not None:
            return [ProviderService._build_model_config_input_from_name(item) for item in models]
        if provider is None:
            return []
        return [
            ProviderModelConfigInput(
                model_name=item.model_name,
                enabled=item.enabled,
                priority=item.priority,
                weight=item.weight,
                supports_stream=item.supports_stream,
                supports_vision=item.supports_vision,
            )
            for item in provider.provider_models
        ]

    @staticmethod
    def _replace_provider_models(db: Session, provider: Provider, model_configs: list[ProviderModelConfigInput]) -> None:
        existing_by_name = {item.model_name: item for item in provider.provider_models}
        keep_names: set[str] = set()

        for config in model_configs:
            keep_names.add(config.model_name)
            provider_model = existing_by_name.get(config.model_name)
            if provider_model is None:
                provider_model = ProviderModel(provider=provider, model_name=config.model_name)
                db.add(provider_model)
            provider_model.enabled = config.enabled
            provider_model.priority = config.priority
            provider_model.weight = config.weight
            provider_model.supports_stream = config.supports_stream
            provider_model.supports_vision = config.supports_vision
            if provider_model.health_status not in {"healthy", "degraded", "unhealthy"}:
                provider_model.health_status = "unknown"
            if not provider_model.circuit_state:
                provider_model.circuit_state = "closed"

        for provider_model in list(provider.provider_models):
            if provider_model.model_name not in keep_names:
                db.delete(provider_model)

        ProviderService._sync_models_json(provider)

    @staticmethod
    def _sync_models_json(provider: Provider) -> None:
        provider.models_json = dumps_json([item.model_name for item in provider.provider_models if item.enabled])
