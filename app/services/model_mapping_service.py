from __future__ import annotations

import hashlib
import random
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
from app.services.router_service import RouteCandidate, RoutePolicyContext, RouterService
from app.utils.json_utils import dumps_json, loads_json


@dataclass(slots=True)
class ModelMappingResolution:
    source_model_name: str
    selected_model_name: str
    strategy: str
    mapping_id: int | None
    trace: dict[str, Any]


class ModelMappingService:
    """负责模型映射规则管理和路由前目标模型选择。"""

    CACHE_PREFIX = "model-mappings"
    STRATEGIES = {"auto", "priority", "weighted"}

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
            strategy=payload.strategy,
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
            "strategy": mapping.strategy,
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
    async def resolve_for_request(
        *,
        source_model_name: str | None,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        sticky_key: str | None,
        forced_provider_id: int | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
    ) -> ModelMappingResolution | None:
        if not source_model_name:
            return None
        return await run_in_threadpool(
            ModelMappingService._resolve_for_request_sync,
            source_model_name=source_model_name,
            route_context=route_context,
            api_client_auth=api_client_auth,
            sticky_key=sticky_key,
            forced_provider_id=forced_provider_id,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )

    @staticmethod
    def _resolve_for_request_sync(
        *,
        source_model_name: str,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        sticky_key: str | None,
        forced_provider_id: int | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
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
            evaluated: list[dict[str, Any]] = []
            for index, target in enumerate(targets):
                target_model_name = str(target.get("model_name") or "").strip()
                if not target_model_name:
                    continue
                if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, target_model_name):
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason="api_key_model_not_allowed"))
                    continue
                try:
                    candidates = RouterService.order_candidates(
                        db,
                        model_name=target_model_name,
                        sticky_key=sticky_key,
                        forced_provider_id=forced_provider_id,
                        route_context=route_context,
                        require_vision=require_vision,
                        require_stream=require_stream,
                        require_tools=require_tools,
                        require_image_generation=require_image_generation,
                        require_chat_completions=require_chat_completions,
                        require_responses=require_responses,
                    )
                except Exception as exc:
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason=str(exc)))
                    continue
                if not candidates:
                    evaluated.append(ModelMappingService._target_trace(target, index, available=False, reason="no_route_candidate"))
                    continue
                best_candidate = candidates[0]
                target_trace = ModelMappingService._target_trace(
                    target,
                    index,
                    available=True,
                    candidate=best_candidate,
                    candidate_count=len(candidates),
                )
                evaluated.append(target_trace)
            available = [item for item in evaluated if item.get("available")]
            if not available:
                return ModelMappingResolution(
                    source_model_name=source_model_name,
                    selected_model_name=source_model_name,
                    strategy=mapping.strategy,
                    mapping_id=mapping.id,
                    trace={
                        "result": "model_mapping_no_available_target",
                        "source_model_name": source_model_name,
                        "strategy": mapping.strategy,
                        "targets": evaluated,
                    },
                )
            selected = ModelMappingService._select_target(
                available,
                strategy=mapping.strategy,
                sticky_key=sticky_key or source_model_name,
            )
            return ModelMappingResolution(
                source_model_name=source_model_name,
                selected_model_name=str(selected["model_name"]),
                strategy=mapping.strategy,
                mapping_id=mapping.id,
                trace={
                    "result": "model_mapping_selected",
                    "mapping_id": mapping.id,
                    "source_model_name": source_model_name,
                    "selected_model_name": selected["model_name"],
                    "strategy": mapping.strategy,
                    "targets": evaluated,
                    "selection_reason": selected.get("selection_reason"),
                },
            )
        finally:
            db.close()

    @staticmethod
    def _select_target(targets: list[dict[str, Any]], *, strategy: str, sticky_key: str | None) -> dict[str, Any]:
        normalized_strategy = strategy if strategy in ModelMappingService.STRATEGIES else "auto"
        if normalized_strategy == "weighted":
            weighted = []
            for item in targets:
                weight = max(0.0, float(item.get("weight") or 0))
                score = max(1.0, float(item.get("score") or 1.0))
                weighted.append(max(0.0, weight * score))
            if any(value > 0 for value in weighted):
                rng = random.Random(ModelMappingService._stable_seed(sticky_key, targets))
                selected = rng.choices(targets, weights=weighted, k=1)[0]
                selected["selection_reason"] = "按权重与路由分加权选择"
                return selected
        if normalized_strategy == "priority":
            selected = sorted(
                targets,
                key=lambda item: (
                    int(item.get("priority") or 100),
                    int(item.get("order") or 0),
                    -float(item.get("route_score") or 0),
                    str(item.get("model_name") or ""),
                ),
            )[0]
            selected["selection_reason"] = "按配置优先级选择首个可用目标"
            return selected
        selected = sorted(
            targets,
            key=lambda item: (
                -float(item.get("score") or 0),
                int(item.get("priority") or 100),
                int(item.get("order") or 0),
                str(item.get("model_name") or ""),
            ),
        )[0]
        selected["selection_reason"] = "按健康、成功率、延迟、成本与配置权重综合择优"
        return selected

    @staticmethod
    def _target_trace(
        target: dict[str, Any],
        index: int,
        *,
        available: bool,
        reason: str | None = None,
        candidate: RouteCandidate | None = None,
        candidate_count: int = 0,
    ) -> dict[str, Any]:
        model_name = str(target.get("model_name") or "").strip()
        priority = int(target.get("priority") or 100)
        weight = int(target.get("weight") or 0)
        route_score = float(candidate.route_score) if candidate is not None else 0.0
        success_rate = float(candidate.recent_success_rate) if candidate is not None else 0.0
        latency_ms = candidate.recent_avg_latency_ms if candidate is not None else None
        cost = ModelMappingService._candidate_cost(candidate) if candidate is not None else None
        score = ModelMappingService._target_score(
            priority=priority,
            weight=weight,
            route_score=route_score,
            success_rate=success_rate,
            latency_ms=latency_ms,
            cost=cost,
        ) if available else 0.0
        payload: dict[str, Any] = {
            "model_name": model_name,
            "available": available,
            "enabled": bool(target.get("enabled", True)),
            "priority": priority,
            "weight": weight,
            "order": index,
            "route_score": route_score,
            "score": score,
            "candidate_count": candidate_count,
        }
        if reason:
            payload["reason"] = reason
        if candidate is not None:
            payload.update({
                "provider_id": candidate.provider.id,
                "provider_name": candidate.provider.name,
                "provider_model_id": candidate.provider_model.id,
                "recent_success_rate": success_rate,
                "recent_avg_latency_ms": latency_ms,
                "recent_failure_rate": candidate.recent_failure_rate,
                "effective_cost": cost,
            })
        return payload

    @staticmethod
    def _target_score(
        *,
        priority: int,
        weight: int,
        route_score: float,
        success_rate: float,
        latency_ms: float | None,
        cost: float | None,
    ) -> float:
        priority_bonus = max(0.0, 40.0 - float(priority))
        weight_bonus = max(0.0, min(float(weight), 1000.0)) / 20.0
        success_bonus = max(0.0, min(success_rate, 1.0)) * 30.0
        latency_penalty = min(25.0, float(latency_ms or 0) / 120.0)
        cost_penalty = min(20.0, float(cost or 0) * 2.0) if cost is not None else 0.0
        return float(route_score) + priority_bonus + weight_bonus + success_bonus - latency_penalty - cost_penalty

    @staticmethod
    def _candidate_cost(candidate: RouteCandidate | None) -> float | None:
        if candidate is None:
            return None
        values = [
            value
            for value in (candidate.provider_model.input_price_per_1k, candidate.provider_model.output_price_per_1k)
            if value is not None
        ]
        if not values:
            return None
        return float(sum(values) / len(values))

    @staticmethod
    def _stable_seed(sticky_key: str | None, targets: list[dict[str, Any]]) -> int:
        basis = sticky_key or "|".join(str(item.get("model_name") or "") for item in targets)
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)

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
                "priority": int(item.get("priority") or 100),
                "weight": int(item.get("weight") or 100),
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
