from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models.model_catalog import ModelCatalog
from app.models.model_mapping import ModelMapping
from app.schemas.model_mapping import ModelMappingCreate, ModelMappingTarget, ModelMappingUpdate
from app.services.api_key_service import ApiClientAuthContext, ApiKeyService
from app.services.cache_service import CacheService
from app.services.router_service import RouterService
from app.utils.json_utils import dumps_json, loads_json


@dataclass(slots=True)
class ModelMappingResolution:
    source_model_name: str
    selected_model_name: str
    mapping_id: int | None
    candidate_model_names: tuple[str, ...]
    trace: dict[str, Any]
    strategy: str = "target_config_order"


class ModelMappingService:
    """负责模型映射规则管理和路由前目标模型选择。"""

    CACHE_PREFIX = "model-mappings"

    @staticmethod
    def list_mappings(db: Session) -> list[dict[str, Any]]:
        mappings = list(db.scalars(select(ModelMapping).order_by(ModelMapping.source_model_name.asc())))
        return [ModelMappingService.serialize_mapping(item) for item in mappings]

    @staticmethod
    def get_mapping(db: Session, source_model_name: str) -> ModelMapping | None:
        return db.scalar(select(ModelMapping).where(ModelMapping.source_model_name == source_model_name.strip()))

    @staticmethod
    def create_mapping(db: Session, payload: ModelMappingCreate) -> ModelMapping:
        if ModelMappingService.get_mapping(db, payload.source_model_name) is not None:
            raise ValueError("源模型映射已存在")
        ModelMappingService._validate_target_catalogs(db, payload.targets)
        mapping = ModelMapping(
            source_model_name=payload.source_model_name,
            enabled=payload.enabled,
            targets_json=dumps_json([item.model_dump() for item in payload.targets]),
            remark=payload.remark,
        )
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        ModelMappingService.invalidate_cache()
        return mapping

    @staticmethod
    def update_mapping(db: Session, mapping: ModelMapping, payload: ModelMappingUpdate) -> ModelMapping:
        data = payload.model_dump(exclude_unset=True)
        targets = data.pop("targets", None)
        if targets is not None:
            target_items = [item if isinstance(item, ModelMappingTarget) else ModelMappingTarget(**item) for item in targets]
            if mapping.source_model_name in {item.model_name for item in target_items}:
                raise ValueError("目标模型不能与源模型相同")
            ModelMappingService._validate_target_catalogs(db, target_items)
            mapping.targets_json = dumps_json([item.model_dump() for item in target_items])
        for field, value in data.items():
            setattr(mapping, field, value)
        db.commit()
        db.refresh(mapping)
        ModelMappingService.invalidate_cache()
        return mapping

    @staticmethod
    def delete_mapping(db: Session, mapping: ModelMapping) -> None:
        db.delete(mapping)
        db.commit()
        ModelMappingService.invalidate_cache()

    @staticmethod
    def serialize_mapping(mapping: ModelMapping) -> dict[str, Any]:
        return {
            "id": mapping.id,
            "source_model_name": mapping.source_model_name,
            "enabled": mapping.enabled,
            "targets": ModelMappingService._parse_targets(mapping.targets_json),
            "remark": mapping.remark,
            "created_at": mapping.created_at,
            "updated_at": mapping.updated_at,
        }

    @staticmethod
    def invalidate_cache() -> None:
        CacheService.invalidate_prefix(ModelMappingService.CACHE_PREFIX)
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")

    @staticmethod
    def normalize_legacy_mapping_data(db: Session) -> bool:
        """清理模型映射目标历史冗余字段。"""
        changed = False
        mappings = list(db.scalars(select(ModelMapping)))
        for mapping in mappings:
            normalized_targets = ModelMappingService._parse_targets(mapping.targets_json)
            normalized_raw = dumps_json(normalized_targets)
            if mapping.targets_json != normalized_raw:
                mapping.targets_json = normalized_raw
                changed = True
        if changed:
            db.commit()
            ModelMappingService.invalidate_cache()
        return changed

    @staticmethod
    async def resolve_for_request(
        *,
        source_model_name: str | None,
        api_client_auth: ApiClientAuthContext | None,
        sticky_key: str | None,
        excluded_target_model_names: tuple[str, ...] | None = None,
    ) -> ModelMappingResolution | None:
        if not source_model_name:
            return None
        return await run_in_threadpool(
            ModelMappingService._resolve_for_request_sync,
            source_model_name=source_model_name,
            api_client_auth=api_client_auth,
            sticky_key=sticky_key,
            excluded_target_model_names=excluded_target_model_names,
        )

    @staticmethod
    def _resolve_for_request_sync(
        *,
        source_model_name: str,
        api_client_auth: ApiClientAuthContext | None,
        sticky_key: str | None,
        excluded_target_model_names: tuple[str, ...] | None = None,
    ) -> ModelMappingResolution | None:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            mapping = ModelMappingService.get_mapping(db, source_model_name)
            if mapping is None or not mapping.enabled:
                return None
            targets = [item for item in ModelMappingService._parse_targets(mapping.targets_json) if item.get("enabled", True)]
            if not targets:
                return None
            excluded_target_set = {
                item.strip()
                for item in (excluded_target_model_names or ())
                if isinstance(item, str) and item.strip()
            }
            recent_route = RouterService.load_recent_session_route(db, sticky_key)
            target_model_names = [
                str(item.get("model_name") or "").strip()
                for item in targets
                if str(item.get("model_name") or "").strip()
            ]
            catalogs = {
                catalog.model_name: catalog
                for catalog in db.scalars(
                    select(ModelCatalog).where(ModelCatalog.model_name.in_(target_model_names))
                )
            } if target_model_names else {}
            evaluated: list[dict[str, Any]] = []
            for index, target in enumerate(targets):
                target_model_name = str(target.get("model_name") or "").strip()
                if not target_model_name:
                    continue
                if target_model_name in excluded_target_set:
                    evaluated.append(
                        ModelMappingService._target_trace(
                            target,
                            index,
                            available=False,
                            reason="excluded_by_failover",
                        )
                    )
                    continue
                if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, target_model_name):
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason="api_key_model_not_allowed"))
                    continue
                catalog = catalogs.get(target_model_name)
                if catalog is None:
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason="target_model_not_found"))
                    continue
                if not catalog.enabled:
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason="target_model_disabled"))
                    continue
                evaluated.append(ModelMappingService._target_trace(target, index, available=True))
            available = [item for item in evaluated if item.get("available")]
            if not available:
                return ModelMappingResolution(
                    source_model_name=source_model_name,
                    selected_model_name=source_model_name,
                    mapping_id=mapping.id,
                    candidate_model_names=(),
                    trace={
                        "result": "model_mapping_no_available_target",
                        "source_model_name": source_model_name,
                        "provider_checks_deferred": True,
                        "targets": evaluated,
                    },
                )
            ordered_targets = ModelMappingService._order_targets(
                available,
                recent_model_name=recent_route.model_name if recent_route is not None else None,
            )
            selected = ordered_targets[0]
            candidate_model_names = tuple(
                str(item.get("model_name") or "").strip()
                for item in ordered_targets
                if str(item.get("model_name") or "").strip()
            )
            return ModelMappingResolution(
                source_model_name=source_model_name,
                selected_model_name=str(selected["model_name"]),
                mapping_id=mapping.id,
                candidate_model_names=candidate_model_names,
                trace={
                    "result": "model_mapping_selected",
                    "mapping_id": mapping.id,
                    "source_model_name": source_model_name,
                    "selected_model_name": selected["model_name"],
                    "provider_checks_deferred": True,
                    "candidate_model_names": list(candidate_model_names),
                    "targets": evaluated,
                    "selection_reason": selected.get("selection_reason"),
                    "recent_session_model_name": recent_route.model_name if recent_route is not None else None,
                },
            )
        finally:
            db.close()

    @staticmethod
    def _order_targets(
        targets: list[dict[str, Any]],
        *,
        strategy: str | None = None,
        sticky_key: str | None = None,
        recent_model_name: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = strategy, sticky_key
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        normalized_recent = recent_model_name.strip() if isinstance(recent_model_name, str) and recent_model_name.strip() else None
        if normalized_recent:
            recent_target = next(
                (item for item in targets if str(item.get("model_name") or "").strip() == normalized_recent),
                None,
            )
            if recent_target is not None:
                recent_target["selection_reason"] = "同一会话上次成功目标模型仍可用，优先复用"
                ordered.append(recent_target)
                seen.add(str(recent_target.get("model_name") or ""))
        for item in ModelMappingService._ordered_targets_by_config(targets):
            model_name = str(item.get("model_name") or "")
            if model_name in seen:
                continue
            ordered.append(item)
            seen.add(model_name)
        if ordered and not ordered[0].get("selection_reason"):
            ordered[0]["selection_reason"] = "按目标模型配置顺序选择，提供商能力与健康在下一阶段判断"
        return ordered

    @staticmethod
    def _ordered_targets_by_config(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(targets)

    @staticmethod
    def _target_trace(
        target: dict[str, Any],
        index: int,
        *,
        available: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        model_name = str(target.get("model_name") or "").strip()
        payload: dict[str, Any] = {
            "model_name": model_name,
            "available": available,
            "enabled": bool(target.get("enabled", True)),
            "order": index,
        }
        if reason:
            payload["reason"] = reason
        return payload

    @staticmethod
    def _parse_targets(raw_value: str | None) -> list[dict[str, Any]]:
        raw_targets = loads_json(raw_value, [])
        if not isinstance(raw_targets, list):
            return []
        targets: list[dict[str, Any]] = []
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("model_name") or "").strip()
            if not model_name:
                continue
            targets.append({
                "model_name": model_name,
                "enabled": bool(item.get("enabled", True)),
            })
        return targets

    @staticmethod
    def _validate_target_catalogs(db: Session, targets: list[ModelMappingTarget]) -> None:
        target_names = [item.model_name for item in targets]
        if not target_names:
            raise ValueError("至少需要配置一个目标模型")
        existing_names = set(
            db.scalars(select(ModelCatalog.model_name).where(ModelCatalog.model_name.in_(target_names)))
        )
        missing = [name for name in target_names if name not in existing_names]
        if missing:
            raise ValueError(f"目标模型不存在于模型管理：{'、'.join(missing)}")
