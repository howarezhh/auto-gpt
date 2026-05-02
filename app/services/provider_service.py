from collections import defaultdict
from datetime import datetime, timedelta

from httpx import HTTPError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.model_catalog import ModelCatalog
from app.models.request_log import RequestLog
from app.models.api_client_key_provider_binding import ApiClientKeyProviderBinding
from app.models.api_client_key import ApiClientKey
from app.models.app_setting import AppSetting
from app.schemas.provider import (
    ProviderCreate,
    ProviderDiscoverModelsIn,
    ProviderDiscoverModelsResponse,
    ProviderDiscoveredModelOut,
    ProviderModelConfigInput,
    ProviderModelConfigUpdate,
    ProviderUpdate,
)
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.provider_capacity_service import (
    ProviderCapacityService,
    ProviderCapacitySnapshot,
    ProviderCapacityUnavailableError,
)
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService
from app.utils.json_utils import dumps_json, loads_json


class ProviderService:
    QUALITY_WINDOW_MINUTES = 24 * 60
    TRACE_TERMINAL_SUCCESS_RESULTS = {"success"}
    TRACE_TERMINAL_FAILURE_RESULTS = {
        "http_error",
        "upstream_auth_error",
        "model_not_found",
        "rate_limited",
        "request_rejected",
        "exception",
        "client_cancelled",
    }

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
    def list_provider_dicts(db: Session) -> list[dict]:
        providers = ProviderService.list_providers(db)
        metrics = ProviderService._build_quality_metrics(db, providers)
        return [ProviderService.provider_to_dict(provider, metrics=metrics) for provider in providers]

    @staticmethod
    def get_provider(db: Session, provider_id: int) -> Provider | None:
        return db.scalar(
            select(Provider).options(selectinload(Provider.provider_models)).where(Provider.id == provider_id)
        )

    @staticmethod
    def create_provider(db: Session, payload: ProviderCreate) -> Provider:
        from app.services.model_catalog_service import ModelCatalogService

        provider = Provider(
            name=payload.name,
            base_url=payload.base_url.rstrip("/"),
            api_key=payload.api_key,
            provider_type=payload.provider_type,
            group_name=payload.group_name,
            region_tag=payload.region_tag,
            enabled=payload.enabled,
            priority=payload.priority,
            weight=payload.weight,
            timeout_ms=payload.timeout_ms,
            max_retries=payload.max_retries,
            max_active_requests=payload.max_active_requests,
            max_active_streams=payload.max_active_streams,
            max_qps=payload.max_qps,
            max_error_rate=payload.max_error_rate,
            first_token_timeout_sec=payload.first_token_timeout_sec,
            maintenance_window=payload.maintenance_window,
            maintenance_mode_enabled=payload.maintenance_mode_enabled,
            auto_circuit_break_enabled=payload.auto_circuit_break_enabled,
            auto_recover_enabled=payload.auto_recover_enabled,
            circuit_breaker_threshold_override=payload.circuit_breaker_threshold_override,
            recovery_probe_interval_sec_override=payload.recovery_probe_interval_sec_override,
            credential_rotated_at=datetime.utcnow(),
            remark=payload.remark,
        )
        db.add(provider)
        db.flush()
        ProviderService._replace_provider_models(db, provider, ProviderService._resolve_model_configs(payload))
        ProviderService.refresh_provider_state(provider)
        db.flush()
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    def update_provider(db: Session, provider: Provider, payload: ProviderUpdate) -> Provider:
        from app.services.model_catalog_service import ModelCatalogService

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
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    def delete_provider(db: Session, provider: Provider) -> None:
        from app.services.model_catalog_service import ModelCatalogService
        from app.services.api_key_auth_cache import ApiKeyAuthCache

        provider_id = provider.id
        affected_bindings = list(
            db.scalars(
                select(ApiClientKeyProviderBinding)
                .options(selectinload(ApiClientKeyProviderBinding.api_client_key))
                .where(ApiClientKeyProviderBinding.provider_id == provider_id)
            )
        )
        affected_default_keys = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.default_provider_id == provider_id)
            )
        )
        affected_key_refs = [
            (binding.api_client_key.id, binding.api_client_key.key_hash, binding.api_client_key.owner_user_id)
            for binding in affected_bindings
            if binding.api_client_key is not None
        ]
        affected_key_refs.extend(
            (api_key.id, api_key.key_hash, api_key.owner_user_id)
            for api_key in affected_default_keys
        )
        db.execute(update(AppSetting).where(AppSetting.default_provider_id == provider_id).values(default_provider_id=None))
        db.execute(update(ApiClientKey).where(ApiClientKey.default_provider_id == provider_id).values(default_provider_id=None))
        db.execute(update(RequestLog).where(RequestLog.provider_id == provider_id).values(provider_id=None))
        db.execute(delete(ApiClientKeyProviderBinding).where(ApiClientKeyProviderBinding.provider_id == provider_id))
        db.delete(provider)
        db.commit()
        for api_key_id, key_hash, owner_user_id in set(affected_key_refs):
            ApiKeyAuthCache.invalidate_api_key(api_key_id, key_hash)
            ApiKeyAuthCache.invalidate_user(owner_user_id)
        ProviderService.invalidate_provider_runtime_cache()
        ModelCatalogService.sync_model_catalogs(db)

    @staticmethod
    def update_provider_model(
        db: Session,
        provider: Provider,
        provider_model_id: int,
        payload: ProviderModelConfigUpdate,
    ) -> ProviderModel:
        from app.services.model_catalog_service import ModelCatalogService

        provider_model = next((item for item in provider.provider_models if item.id == provider_model_id), None)
        if provider_model is None:
            raise ValueError("Provider model not found")

        for field, value in payload.model_dump(exclude_unset=True).items():
            if field in {"input_price_per_1k", "output_price_per_1k", "cache_price_per_1k"}:
                continue
            if field == "price_multiplier" and value is None:
                continue
            setattr(provider_model, field, value)
        ProviderService._sync_provider_model_price_from_catalog(db, provider_model)

        ProviderService.refresh_provider_state(provider)
        db.flush()
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider_model)
        return provider_model

    @staticmethod
    def provider_to_dict(provider: Provider, *, metrics: dict | None = None) -> dict:
        metrics = metrics or {"providers": {}, "provider_models": {}}
        provider_metric = metrics["providers"].get(provider.id, {})
        try:
            capacity_snapshot = ProviderCapacityService.snapshot(provider.id)
        except ProviderCapacityUnavailableError:
            capacity_snapshot = ProviderCapacitySnapshot()
        best_input_price = min(
            (item.input_price_per_1k for item in provider.provider_models if item.input_price_per_1k is not None),
            default=None,
        )
        best_output_price = min(
            (item.output_price_per_1k for item in provider.provider_models if item.output_price_per_1k is not None),
            default=None,
        )
        return {
            "id": provider.id,
            "name": provider.name,
            "base_url": provider.base_url,
            "api_key_masked": ProviderService.mask_api_key(provider.api_key),
            "provider_type": provider.provider_type,
            "group_name": provider.group_name,
            "region_tag": provider.region_tag,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "weight": provider.weight,
            "timeout_ms": provider.timeout_ms,
            "max_retries": provider.max_retries,
            "max_active_requests": provider.max_active_requests,
            "max_active_streams": provider.max_active_streams,
            "max_qps": provider.max_qps,
            "max_error_rate": provider.max_error_rate,
            "first_token_timeout_sec": provider.first_token_timeout_sec,
            "active_requests": capacity_snapshot.active_requests,
            "active_streams": capacity_snapshot.active_streams,
            "current_qps": capacity_snapshot.current_qps,
            "maintenance_window": provider.maintenance_window,
            "maintenance_mode_enabled": provider.maintenance_mode_enabled,
            "auto_circuit_break_enabled": provider.auto_circuit_break_enabled,
            "auto_recover_enabled": provider.auto_recover_enabled,
            "circuit_breaker_threshold_override": provider.circuit_breaker_threshold_override,
            "recovery_probe_interval_sec_override": provider.recovery_probe_interval_sec_override,
            "models": [item.model_name for item in provider.provider_models],
            "model_configs": [
                ProviderService.provider_model_to_dict(item, metrics=metrics["provider_models"].get(item.id))
                for item in provider.provider_models
            ],
            "health_status": provider.health_status,
            "last_check_at": provider.last_check_at,
            "last_latency_ms": provider.last_latency_ms,
            "failure_count": provider.failure_count,
            "success_count": provider.success_count,
            "circuit_state": provider.circuit_state,
            "recent_request_count": provider_metric.get("recent_request_count", 0),
            "success_rate": provider_metric.get("success_rate"),
            "avg_first_token_latency_ms": provider_metric.get("avg_first_token_latency_ms"),
            "stability_score": provider_metric.get("stability_score"),
            "best_input_price_per_1k": best_input_price,
            "best_output_price_per_1k": best_output_price,
            "credential_rotated_at": provider.credential_rotated_at,
            "credential_hint": provider.credential_hint,
            "remark": provider.remark,
            "created_at": provider.created_at,
            "updated_at": provider.updated_at,
        }

    @staticmethod
    def get_effective_circuit_breaker_threshold(db: Session, provider: Provider) -> int:
        if provider.circuit_breaker_threshold_override is not None and provider.circuit_breaker_threshold_override > 0:
            return provider.circuit_breaker_threshold_override
        return max(1, SettingService.get_or_create(db).circuit_breaker_threshold)

    @staticmethod
    def get_effective_recovery_probe_interval_sec(db: Session, provider: Provider) -> int:
        if provider.recovery_probe_interval_sec_override is not None and provider.recovery_probe_interval_sec_override > 0:
            return provider.recovery_probe_interval_sec_override
        return max(10, SettingService.get_or_create(db).recovery_probe_interval_sec)

    @staticmethod
    def rotate_provider_credential(
        db: Session,
        provider: Provider,
        *,
        api_key: str,
        credential_hint: str | None,
    ) -> Provider:
        provider.api_key = api_key.strip()
        provider.credential_hint = credential_hint
        provider.credential_rotated_at = datetime.utcnow()
        provider.health_status = "unknown"
        provider.circuit_state = "closed"
        for provider_model in provider.provider_models:
            provider_model.health_status = "unknown"
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
            provider_model.last_error = None
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    async def discover_models(
        db: Session,
        payload: ProviderDiscoverModelsIn,
    ) -> ProviderDiscoverModelsResponse:
        provider = None
        if payload.provider_id is not None:
            provider = ProviderService.get_provider(db, payload.provider_id)
            if provider is None:
                raise ValueError("Provider not found")
        base_url = payload.base_url or (provider.base_url if provider else None)
        api_key = payload.api_key or (provider.api_key if provider else None)
        if not base_url or not api_key:
            raise ValueError("必须提供 Base URL 和 API 密钥，或指定已存在的中转站")

        normalized_base_url = base_url.rstrip("/")
        timeout_ms = payload.timeout_ms or (provider.timeout_ms if provider else 30000)
        timeout_sec = max(1, int(timeout_ms / 1000))
        response = None
        try:
            response = await UpstreamClientService.get_client().get(
                f"{normalized_base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout_sec,
            )
            response.raise_for_status()
        except HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise ValueError(f"获取可用模型失败：{detail}") from exc
        except Exception as exc:
            raise ValueError(f"获取可用模型失败：{exc}") from exc

        try:
            body = response.json()
        except Exception as exc:
            raise ValueError(f"上游 /models 返回的不是合法 JSON：{exc}") from exc

        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ValueError("上游 /models 返回格式不符合预期，缺少 data 数组")

        existing_names = set(payload.existing_model_names or [])
        discovered_names: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("id") or "").strip()
            if not model_name or model_name in discovered_names:
                continue
            discovered_names.append(model_name)

        discovered_items = [
            ProviderDiscoveredModelOut(
                model_name=model_name,
                supports_stream=ProviderService._infer_model_capabilities(model_name)["supports_stream"],
                supports_vision=ProviderService._infer_model_capabilities(model_name)["supports_vision"],
                already_configured=model_name in existing_names,
            )
            for model_name in discovered_names
        ]
        return ProviderDiscoverModelsResponse(
            provider_name=provider.name if provider else None,
            source_base_url=normalized_base_url,
            total_models=len(discovered_items),
            items=discovered_items,
        )

    @staticmethod
    def availability_timeseries(
        db: Session,
        *,
        provider: Provider,
        window_hours: int,
        bucket_minutes: int,
    ) -> list[dict]:
        normalized_window_hours = max(1, min(window_hours, 24 * 30))
        normalized_bucket_minutes = max(5, min(bucket_minutes, 24 * 60))
        since = datetime.utcnow() - timedelta(hours=normalized_window_hours)
        logs = list(
            db.scalars(
                select(RequestLog)
                .where(
                    RequestLog.provider_id == provider.id,
                    RequestLog.created_at >= since,
                    LogService._route_traffic_expr(),
                )
                .order_by(RequestLog.created_at.asc(), RequestLog.id.asc())
            )
        )
        buckets: dict[datetime, dict] = {}
        for item in logs:
            if item.created_at is None:
                continue
            minute_floor = item.created_at.replace(second=0, microsecond=0)
            bucket_minute = minute_floor.minute - (minute_floor.minute % normalized_bucket_minutes)
            bucket_start = minute_floor.replace(minute=bucket_minute)
            current = buckets.setdefault(
                bucket_start,
                {
                    "bucket_start": bucket_start,
                    "total_requests": 0,
                    "success_requests": 0,
                    "failed_requests": 0,
                    "latency_values": [],
                },
            )
            current["total_requests"] += 1
            current["success_requests"] += 1 if item.success else 0
            current["failed_requests"] += 0 if item.success else 1
            if item.latency_ms is not None:
                current["latency_values"].append(float(item.latency_ms))
        results = []
        for bucket_start in sorted(buckets.keys()):
            current = buckets[bucket_start]
            latency_values = current.pop("latency_values")
            total_requests = int(current["total_requests"] or 0)
            success_requests = int(current["success_requests"] or 0)
            current["success_rate"] = round((success_requests / total_requests) * 100, 2) if total_requests else 0.0
            current["avg_latency_ms"] = round(sum(latency_values) / len(latency_values), 2) if latency_values else None
            results.append(current)
        return results

    @staticmethod
    def provider_model_to_dict(provider_model: ProviderModel, *, metrics: dict | None = None) -> dict:
        metrics = metrics or {}
        return {
            "id": provider_model.id,
            "model_name": provider_model.model_name,
            "enabled": provider_model.enabled,
            "priority": provider_model.priority,
            "weight": provider_model.weight,
            "health_status": provider_model.health_status,
            "circuit_state": provider_model.circuit_state,
            "circuit_opened_at": provider_model.circuit_opened_at,
            "last_check_at": provider_model.last_check_at,
            "last_latency_ms": provider_model.last_latency_ms,
            "failure_count": provider_model.failure_count,
            "success_count": provider_model.success_count,
            "last_error": provider_model.last_error,
            "supports_stream": provider_model.supports_stream,
            "supports_vision": provider_model.supports_vision,
            "price_multiplier": provider_model.price_multiplier,
            "input_price_per_1k": provider_model.input_price_per_1k,
            "output_price_per_1k": provider_model.output_price_per_1k,
            "cache_price_per_1k": provider_model.cache_price_per_1k,
            "recent_request_count": metrics.get("recent_request_count", 0),
            "success_rate": metrics.get("success_rate"),
            "avg_first_token_latency_ms": metrics.get("avg_first_token_latency_ms"),
            "stability_score": metrics.get("stability_score"),
            "created_at": provider_model.created_at,
            "updated_at": provider_model.updated_at,
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
                price_multiplier=item.price_multiplier or 1.0,
                input_price_per_1k=item.input_price_per_1k,
                output_price_per_1k=item.output_price_per_1k,
                cache_price_per_1k=item.cache_price_per_1k,
            )
            for item in provider.provider_models
        ]

    @staticmethod
    def _replace_provider_models(db: Session, provider: Provider, model_configs: list[ProviderModelConfigInput]) -> None:
        existing_by_name = {item.model_name: item for item in provider.provider_models}
        catalogs_by_name = {
            item.model_name: item
            for item in db.scalars(select(ModelCatalog).where(ModelCatalog.model_name.in_([config.model_name for config in model_configs])))
        } if model_configs else {}
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
            provider_model.price_multiplier = config.price_multiplier or 1.0
            catalog = catalogs_by_name.get(config.model_name)
            if config.input_price_per_1k is not None:
                provider_model.input_price_per_1k = config.input_price_per_1k * provider_model.price_multiplier
            elif catalog is not None:
                provider_model.input_price_per_1k = (
                    catalog.input_price_per_1k * provider_model.price_multiplier
                    if catalog.input_price_per_1k is not None
                    else None
                )
            else:
                provider_model.input_price_per_1k = None

            if config.output_price_per_1k is not None:
                provider_model.output_price_per_1k = config.output_price_per_1k * provider_model.price_multiplier
            elif catalog is not None:
                provider_model.output_price_per_1k = (
                    catalog.output_price_per_1k * provider_model.price_multiplier
                    if catalog.output_price_per_1k is not None
                    else None
                )
            else:
                provider_model.output_price_per_1k = None

            if config.cache_price_per_1k is not None:
                provider_model.cache_price_per_1k = config.cache_price_per_1k * provider_model.price_multiplier
            elif catalog is not None:
                catalog_cache_price = catalog.cache_price_per_1k
                if catalog_cache_price is None:
                    catalog_cache_price = catalog.input_price_per_1k
                provider_model.cache_price_per_1k = (
                    catalog_cache_price * provider_model.price_multiplier
                    if catalog_cache_price is not None
                    else None
                )
            elif config.input_price_per_1k is not None:
                provider_model.cache_price_per_1k = config.input_price_per_1k * provider_model.price_multiplier
            else:
                provider_model.cache_price_per_1k = None
            if provider_model.health_status not in {"healthy", "degraded", "unhealthy"}:
                provider_model.health_status = "unknown"
            if not provider_model.circuit_state:
                provider_model.circuit_state = "closed"

        for provider_model in list(provider.provider_models):
            if provider_model.model_name not in keep_names:
                db.delete(provider_model)

        ProviderService._sync_models_json(provider)

    @staticmethod
    def _sync_provider_model_price_from_catalog(db: Session, provider_model: ProviderModel) -> None:
        catalog = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == provider_model.model_name))
        if catalog is None:
            return
        if catalog.input_price_per_1k is not None:
            provider_model.input_price_per_1k = catalog.input_price_per_1k * provider_model.price_multiplier
        if catalog.output_price_per_1k is not None:
            provider_model.output_price_per_1k = catalog.output_price_per_1k * provider_model.price_multiplier
        catalog_cache_price = catalog.cache_price_per_1k
        if catalog_cache_price is None:
            catalog_cache_price = catalog.input_price_per_1k
        if catalog_cache_price is not None:
            provider_model.cache_price_per_1k = catalog_cache_price * provider_model.price_multiplier

    @staticmethod
    def _sync_models_json(provider: Provider) -> None:
        provider.models_json = dumps_json([item.model_name for item in provider.provider_models if item.enabled])

    @staticmethod
    def invalidate_provider_runtime_cache() -> None:
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")

    @staticmethod
    def _build_quality_metrics(db: Session, providers: list[Provider]) -> dict[str, dict]:
        provider_ids = {item.id for item in providers}
        provider_model_map = {
            item.id: item
            for provider in providers
            for item in provider.provider_models
        }
        if not provider_ids:
            return {"providers": {}, "provider_models": {}}

        since = datetime.utcnow() - timedelta(minutes=ProviderService.QUALITY_WINDOW_MINUTES)
        logs = list(
            db.scalars(
                select(RequestLog).where(
                    RequestLog.created_at >= since,
                    LogService._route_traffic_expr(),
                )
            )
        )

        provider_stats: dict[int, dict] = defaultdict(ProviderService._empty_quality_accumulator)
        model_stats: dict[int, dict] = defaultdict(ProviderService._empty_quality_accumulator)

        for log in logs:
            trace = loads_json(log.trace_json, [])
            if isinstance(trace, list):
                for item in trace:
                    if not isinstance(item, dict):
                        continue
                    provider_id = item.get("provider_id")
                    provider_model_id = item.get("provider_model_id")
                    result = item.get("result")
                    if not isinstance(provider_id, int) or provider_id not in provider_ids:
                        continue
                    if result in ProviderService.TRACE_TERMINAL_SUCCESS_RESULTS:
                        ProviderService._register_attempt(provider_stats[provider_id], success=True)
                        if isinstance(provider_model_id, int) and provider_model_id in provider_model_map:
                            ProviderService._register_attempt(model_stats[provider_model_id], success=True)
                    elif result in ProviderService.TRACE_TERMINAL_FAILURE_RESULTS:
                        ProviderService._register_attempt(provider_stats[provider_id], success=False)
                        if isinstance(provider_model_id, int) and provider_model_id in provider_model_map:
                            ProviderService._register_attempt(model_stats[provider_model_id], success=False)

            if (
                log.success
                and isinstance(log.provider_id, int)
                and log.provider_id in provider_ids
                and isinstance(log.resolved_provider_model_id, int)
                and log.resolved_provider_model_id in provider_model_map
                and log.first_token_latency_ms is not None
            ):
                ProviderService._register_first_token(provider_stats[log.provider_id], log.first_token_latency_ms)
                ProviderService._register_first_token(model_stats[log.resolved_provider_model_id], log.first_token_latency_ms)

        provider_metrics = {
            provider.id: ProviderService._finalize_quality_snapshot(
                provider_stats.get(provider.id, ProviderService._empty_quality_accumulator()),
                health_status=provider.health_status,
                circuit_state=provider.circuit_state,
            )
            for provider in providers
        }
        provider_model_metrics = {
            provider_model.id: ProviderService._finalize_quality_snapshot(
                model_stats.get(provider_model.id, ProviderService._empty_quality_accumulator()),
                health_status=provider_model.health_status,
                circuit_state=provider_model.circuit_state,
            )
            for provider_model in provider_model_map.values()
        }
        return {"providers": provider_metrics, "provider_models": provider_model_metrics}

    @staticmethod
    def _empty_quality_accumulator() -> dict[str, int | float]:
        return {
            "recent_request_count": 0,
            "success_count": 0,
            "first_token_sum": 0.0,
            "first_token_count": 0,
        }

    @staticmethod
    def _register_attempt(target: dict[str, int | float], *, success: bool) -> None:
        target["recent_request_count"] += 1
        if success:
            target["success_count"] += 1

    @staticmethod
    def _register_first_token(target: dict[str, int | float], first_token_latency_ms: int) -> None:
        target["first_token_sum"] += float(first_token_latency_ms)
        target["first_token_count"] += 1

    @staticmethod
    def _finalize_quality_snapshot(
        stats: dict[str, int | float],
        *,
        health_status: str,
        circuit_state: str,
    ) -> dict[str, int | float | None]:
        request_count = int(stats.get("recent_request_count", 0) or 0)
        success_count = int(stats.get("success_count", 0) or 0)
        first_token_count = int(stats.get("first_token_count", 0) or 0)
        avg_first_token_latency_ms = (
            round(float(stats["first_token_sum"]) / first_token_count, 2)
            if first_token_count
            else None
        )
        success_rate = round((success_count / request_count) * 100, 2) if request_count else None
        stability_score = ProviderService._calculate_stability_score(
            request_count=request_count,
            success_rate=success_rate,
            avg_first_token_latency_ms=avg_first_token_latency_ms,
            health_status=health_status,
            circuit_state=circuit_state,
        )
        return {
            "recent_request_count": request_count,
            "success_rate": success_rate,
            "avg_first_token_latency_ms": avg_first_token_latency_ms,
            "stability_score": stability_score,
        }

    @staticmethod
    def _calculate_stability_score(
        *,
        request_count: int,
        success_rate: float | None,
        avg_first_token_latency_ms: float | None,
        health_status: str,
        circuit_state: str,
    ) -> float:
        if request_count <= 0:
            base = {
                "healthy": 85.0,
                "degraded": 65.0,
                "unhealthy": 30.0,
                "unknown": 50.0,
            }.get(health_status, 50.0)
            if circuit_state == "half_open":
                base -= 8.0
            elif circuit_state == "open":
                base -= 20.0
        else:
            base = success_rate if success_rate is not None else 50.0
            if avg_first_token_latency_ms is not None:
                base -= min(35.0, avg_first_token_latency_ms / 100.0)
            if health_status == "degraded":
                base -= 10.0
            elif health_status == "unhealthy":
                base -= 25.0
            elif health_status == "unknown":
                base -= 5.0
            if circuit_state == "half_open":
                base -= 8.0
            elif circuit_state == "open":
                base -= 20.0
            if request_count < 5:
                base -= 5.0
        return round(max(0.0, min(100.0, base)), 2)
