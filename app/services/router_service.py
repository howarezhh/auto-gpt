import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_capacity_service import ProviderCapacityService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService


@dataclass(slots=True)
class RouteCandidate:
    provider: Provider
    provider_model: ProviderModel
    recent_failure_rate: float = 0.0
    recent_success_rate: float = 1.0
    recent_avg_latency_ms: float | None = None
    dynamic_weight: float = 1.0
    route_score: float = 0.0


@dataclass(slots=True)
class RoutePolicyContext:
    route_mode: str
    default_provider_id: int | None
    manual_allow_fallback: bool
    allowed_provider_ids: list[int] | None = None
    forced_provider_id: int | None = None
    preferred_provider_ids: list[int] | None = None
    preferred_region_tags: list[str] | None = None
    max_candidate_count: int | None = None
    latency_bias: int = 1
    success_rate_bias: int = 1
    cost_bias: int = 0

    def with_forced_provider_id(self, forced_provider_id: int | None) -> "RoutePolicyContext":
        return RoutePolicyContext(
            route_mode=self.route_mode,
            default_provider_id=self.default_provider_id,
            manual_allow_fallback=self.manual_allow_fallback,
            allowed_provider_ids=list(self.allowed_provider_ids) if self.allowed_provider_ids is not None else None,
            forced_provider_id=forced_provider_id,
            preferred_provider_ids=list(self.preferred_provider_ids) if self.preferred_provider_ids is not None else None,
            preferred_region_tags=list(self.preferred_region_tags) if self.preferred_region_tags is not None else None,
            max_candidate_count=self.max_candidate_count,
            latency_bias=self.latency_bias,
            success_rate_bias=self.success_rate_bias,
            cost_bias=self.cost_bias,
        )


class RouterService:
    RECENT_WINDOW_MINUTES = 5
    ROUTE_DIAGNOSTIC_SAMPLE_LIMIT = 8

    @staticmethod
    def get_available_candidates(
        db: Session,
        model_name: str | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
    ) -> list[RouteCandidate]:
        cache_key = RouterService._build_candidate_cache_key(
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        providers = ProviderService.list_providers(db)
        now = datetime.utcnow()
        metrics = LogService.route_metric_summary(db, window_minutes=RouterService.RECENT_WINDOW_MINUTES, requested_model=model_name)
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)

        candidates: list[RouteCandidate] = []
        for provider in providers:
            if not provider.enabled or provider.circuit_state == "open" or provider.maintenance_mode_enabled:
                continue
            if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                continue
            for provider_model in provider.provider_models:
                if not provider_model.enabled:
                    continue
                if provider_model.model_name not in enabled_model_names:
                    continue
                if model_name and provider_model.model_name != model_name:
                    continue
                if require_stream and not provider_model.supports_stream:
                    continue
                if require_vision and not provider_model.supports_vision:
                    continue
                if require_tools and not ProviderService.provider_model_supports_tools(provider_model):
                    continue
                if require_image_generation and not ProviderService.provider_model_supports_image_generation(provider_model):
                    continue
                if require_chat_completions and not provider_model.supports_chat_completions:
                    continue
                if require_responses and not provider_model.supports_responses:
                    continue
                if provider_model.circuit_state == "open":
                    if not RouterService._should_probe_open_model(
                        provider=provider,
                        provider_model=provider_model,
                        recovery_interval_sec=ProviderService.get_effective_recovery_probe_interval_sec(db, provider),
                        now=now,
                    ):
                        continue
                    if not RouterService._claim_half_open_probe(db, provider_model, now):
                        continue
                if provider_model.health_status == "unhealthy" and provider_model.circuit_state not in {"half_open"}:
                    continue

                metric = metrics.get((provider.id, provider_model.model_name), {})
                recent_failure_rate = float(metric.get("failure_rate", 0.0))
                recent_success_rate = float(metric.get("success_rate", 1.0))
                recent_avg_latency_ms = metric.get("avg_latency_ms")
                dynamic_weight = RouterService._dynamic_weight(provider_model.weight, recent_failure_rate)
                route_score = RouterService._route_score(
                    provider=provider,
                    provider_model=provider_model,
                    recent_success_rate=recent_success_rate,
                    recent_avg_latency_ms=recent_avg_latency_ms,
                    route_context=route_context,
                )
                candidates.append(
                    RouteCandidate(
                        provider=provider,
                        provider_model=provider_model,
                        recent_failure_rate=recent_failure_rate,
                        recent_success_rate=recent_success_rate,
                        recent_avg_latency_ms=recent_avg_latency_ms,
                        dynamic_weight=dynamic_weight,
                        route_score=route_score,
                    )
                )
        return candidates

    @staticmethod
    def order_candidates(
        db: Session,
        model_name: str | None = None,
        sticky_key: str | None = None,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
    ) -> list[RouteCandidate]:
        candidates = RouterService.get_available_candidates(
            db,
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        effective_forced_provider_id = route_context.forced_provider_id if route_context and route_context.forced_provider_id is not None else forced_provider_id
        if effective_forced_provider_id is not None:
            candidates = [item for item in candidates if item.provider.id == effective_forced_provider_id]
        candidates = RouterService._filter_capacity_candidates(candidates, is_stream=require_stream)
        return RouterService._order_filtered_candidates(
            db,
            candidates,
            sticky_key=sticky_key,
            route_context=route_context,
        )

    @staticmethod
    async def async_order_candidates(
        db: Session,
        model_name: str | None = None,
        sticky_key: str | None = None,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
    ) -> list[RouteCandidate]:
        candidates = await run_in_threadpool(
            RouterService._get_available_candidates_with_scoped_session,
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        effective_forced_provider_id = route_context.forced_provider_id if route_context and route_context.forced_provider_id is not None else forced_provider_id
        if effective_forced_provider_id is not None:
            candidates = [item for item in candidates if item.provider.id == effective_forced_provider_id]
        candidates = await RouterService._async_filter_capacity_candidates(candidates, is_stream=require_stream)
        return await run_in_threadpool(
            RouterService._order_filtered_candidates_with_scoped_session,
            candidates,
            sticky_key=sticky_key,
            route_context=route_context,
        )

    @staticmethod
    async def async_diagnose_candidate_unavailability(
        *,
        model_name: str | None = None,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
        is_stream: bool = False,
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            RouterService._diagnose_candidate_unavailability_with_scoped_session,
            model_name=model_name,
            forced_provider_id=forced_provider_id,
            route_context=route_context,
            require_vision=require_vision,
            require_stream=require_stream,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
            is_stream=is_stream,
        )

    @staticmethod
    def _get_available_candidates_with_scoped_session(
        *,
        model_name: str | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
    ) -> list[RouteCandidate]:
        db = SessionLocal()
        try:
            return RouterService.get_available_candidates(
                db,
                model_name=model_name,
                route_context=route_context,
                require_vision=require_vision,
                require_stream=require_stream,
                require_tools=require_tools,
                require_image_generation=require_image_generation,
                require_chat_completions=require_chat_completions,
                require_responses=require_responses,
            )
        finally:
            db.close()

    @staticmethod
    def _order_filtered_candidates_with_scoped_session(
        candidates: list[RouteCandidate],
        *,
        sticky_key: str | None,
        route_context: RoutePolicyContext | None,
    ) -> list[RouteCandidate]:
        db = SessionLocal()
        try:
            return RouterService._order_filtered_candidates(
                db,
                candidates,
                sticky_key=sticky_key,
                route_context=route_context,
            )
        finally:
            db.close()

    @staticmethod
    def _diagnose_candidate_unavailability_with_scoped_session(
        *,
        model_name: str | None = None,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
        is_stream: bool = False,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            return RouterService.diagnose_candidate_unavailability(
                db,
                model_name=model_name,
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                require_vision=require_vision,
                require_stream=require_stream,
                require_tools=require_tools,
                require_image_generation=require_image_generation,
                require_chat_completions=require_chat_completions,
                require_responses=require_responses,
                is_stream=is_stream,
            )
        finally:
            db.close()

    @staticmethod
    def diagnose_candidate_unavailability(
        db: Session,
        *,
        model_name: str | None = None,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
        is_stream: bool = False,
    ) -> dict[str, Any]:
        providers = ProviderService.list_providers(db)
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)
        metrics = LogService.route_metric_summary(db, window_minutes=RouterService.RECENT_WINDOW_MINUTES, requested_model=model_name)
        now = datetime.utcnow()
        effective_forced_provider_id = route_context.forced_provider_id if route_context and route_context.forced_provider_id is not None else forced_provider_id
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        diagnostics: dict[str, Any] = {
            "requested_model": model_name,
            "require_stream": require_stream,
            "require_vision": require_vision,
            "require_tools": require_tools,
            "require_image_generation": require_image_generation,
            "require_chat_completions": require_chat_completions,
            "require_responses": require_responses,
            "is_stream_request": is_stream,
            "forced_provider_id": effective_forced_provider_id,
            "allowed_provider_ids": sorted(allowed_provider_ids) if allowed_provider_ids is not None else None,
            "provider_total": len(providers),
            "provider_scanned": 0,
            "mounted_model_total": 0,
            "matching_model_mount_count": 0,
            "pre_capacity_candidate_count": 0,
            "final_candidate_count": 0,
            "reason_counts": {},
            "samples": [],
        }
        provider_id_set = {provider.id for provider in providers}
        if effective_forced_provider_id is not None and effective_forced_provider_id not in provider_id_set:
            RouterService._record_diagnostic_reason(
                diagnostics,
                "forced_provider_not_found",
                extra={"provider_id": effective_forced_provider_id},
            )
        pre_capacity_candidates: list[RouteCandidate] = []
        for provider in providers:
            if effective_forced_provider_id is not None and provider.id != effective_forced_provider_id:
                continue
            diagnostics["provider_scanned"] += 1
            if not provider.provider_models:
                RouterService._record_diagnostic_reason(diagnostics, "provider_without_models", provider=provider)
                continue
            if not provider.enabled:
                RouterService._record_diagnostic_reason(diagnostics, "provider_disabled", provider=provider)
                continue
            if provider.circuit_state == "open":
                RouterService._record_diagnostic_reason(diagnostics, "provider_circuit_open", provider=provider)
                continue
            if provider.maintenance_mode_enabled:
                RouterService._record_diagnostic_reason(diagnostics, "provider_maintenance_mode", provider=provider)
                continue
            if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                RouterService._record_diagnostic_reason(diagnostics, "provider_not_authorized", provider=provider)
                continue
            for provider_model in provider.provider_models:
                diagnostics["mounted_model_total"] += 1
                if model_name and provider_model.model_name == model_name:
                    diagnostics["matching_model_mount_count"] += 1
                if not provider_model.enabled:
                    RouterService._record_diagnostic_reason(diagnostics, "model_disabled", provider=provider, provider_model=provider_model)
                    continue
                if provider_model.model_name not in enabled_model_names:
                    RouterService._record_diagnostic_reason(diagnostics, "model_globally_disabled", provider=provider, provider_model=provider_model)
                    continue
                if model_name and provider_model.model_name != model_name:
                    RouterService._record_diagnostic_reason(diagnostics, "model_name_mismatch", provider=provider, provider_model=provider_model)
                    continue
                if require_stream and not provider_model.supports_stream:
                    RouterService._record_diagnostic_reason(diagnostics, "stream_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_vision and not provider_model.supports_vision:
                    RouterService._record_diagnostic_reason(diagnostics, "vision_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_tools and not ProviderService.provider_model_supports_tools(provider_model):
                    RouterService._record_diagnostic_reason(diagnostics, "tools_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_image_generation and not ProviderService.provider_model_supports_image_generation(provider_model):
                    RouterService._record_diagnostic_reason(diagnostics, "image_generation_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_chat_completions and not provider_model.supports_chat_completions:
                    RouterService._record_diagnostic_reason(diagnostics, "chat_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_responses and not provider_model.supports_responses:
                    RouterService._record_diagnostic_reason(diagnostics, "responses_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if provider_model.circuit_state == "open" and not RouterService._should_probe_open_model(
                    provider=provider,
                    provider_model=provider_model,
                    recovery_interval_sec=ProviderService.get_effective_recovery_probe_interval_sec(db, provider),
                    now=now,
                ):
                    RouterService._record_diagnostic_reason(diagnostics, "model_circuit_open", provider=provider, provider_model=provider_model)
                    continue
                if provider_model.health_status == "unhealthy" and provider_model.circuit_state not in {"half_open"}:
                    RouterService._record_diagnostic_reason(diagnostics, "model_unhealthy", provider=provider, provider_model=provider_model)
                    continue
                pre_capacity_candidates.append(
                    RouteCandidate(
                        provider=provider,
                        provider_model=provider_model,
                        recent_failure_rate=float(metrics.get((provider.id, provider_model.model_name), {}).get("failure_rate", 0.0)),
                    )
                )
        diagnostics["pre_capacity_candidate_count"] = len(pre_capacity_candidates)
        if not pre_capacity_candidates:
            diagnostics["summary"] = RouterService._build_diagnostic_summary(diagnostics)
            return diagnostics

        snapshots = ProviderCapacityService.snapshots({item.provider.id for item in pre_capacity_candidates})
        for candidate in pre_capacity_candidates:
            snapshot = snapshots.get(candidate.provider.id)
            if snapshot is None:
                RouterService._record_diagnostic_reason(
                    diagnostics,
                    "capacity_snapshot_unavailable",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model,
                )
                continue
            if not ProviderCapacityService._has_capacity(candidate.provider, snapshot=snapshot, is_stream=is_stream):
                RouterService._record_diagnostic_reason(
                    diagnostics,
                    "provider_capacity_exceeded",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model,
                )
                continue
            if (
                candidate.provider.max_error_rate is not None
                and candidate.provider.max_error_rate > 0
                and candidate.recent_failure_rate * 100 >= candidate.provider.max_error_rate
            ):
                RouterService._record_diagnostic_reason(
                    diagnostics,
                    "provider_failure_rate_limited",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model,
                )
                continue
            diagnostics["final_candidate_count"] += 1
        diagnostics["summary"] = RouterService._build_diagnostic_summary(diagnostics)
        return diagnostics

    @staticmethod
    def _order_filtered_candidates(
        db: Session,
        candidates: list[RouteCandidate],
        *,
        sticky_key: str | None,
        route_context: RoutePolicyContext | None,
    ) -> list[RouteCandidate]:
        setting = SettingService.get_or_create(db)
        if not candidates:
            return []

        if route_context and route_context.preferred_provider_ids:
            preferred_set = set(route_context.preferred_provider_ids)
            preferred = [item for item in candidates if item.provider.id in preferred_set]
            others = [item for item in candidates if item.provider.id not in preferred_set]
            candidates = preferred + others

        route_mode = route_context.route_mode if route_context else setting.route_mode
        manual_allow_fallback = route_context.manual_allow_fallback if route_context else setting.manual_allow_fallback
        default_provider_id = route_context.default_provider_id if route_context else setting.default_provider_id

        sorted_candidates = sorted(
            candidates,
            key=lambda item: (
                -item.route_score,
                item.provider_model.priority,
                item.provider.priority,
                item.provider.id,
                item.provider_model.id,
            ),
        )
        ordered: list[RouteCandidate] = []
        seen: set[tuple[int, int]] = set()

        def append_candidate(candidate: RouteCandidate | None) -> None:
            if candidate is None:
                return
            key = (candidate.provider.id, candidate.provider_model.id)
            if key in seen:
                return
            ordered.append(candidate)
            seen.add(key)

        default_candidate = next(
            (item for item in sorted_candidates if item.provider.id == default_provider_id),
            None,
        )

        if route_mode == "manual":
            append_candidate(default_candidate)
            if manual_allow_fallback:
                for candidate in sorted_candidates:
                    append_candidate(candidate)
            return RouterService._trim_candidates(ordered, route_context=route_context)

        if route_mode == "failover":
            append_candidate(default_candidate)
            for candidate in sorted_candidates:
                append_candidate(candidate)
            return RouterService._trim_candidates(ordered, route_context=route_context)

        if route_mode == "weighted":
            for candidate in RouterService._weighted_shuffle(sorted_candidates):
                append_candidate(candidate)
            return RouterService._trim_candidates(ordered, route_context=route_context)

        if route_mode == "sticky":
            for candidate in RouterService._sticky_order(sorted_candidates, sticky_key):
                append_candidate(candidate)
            return RouterService._trim_candidates(ordered, route_context=route_context)

        return RouterService._trim_candidates(sorted_candidates, route_context=route_context)

    @staticmethod
    def _trim_candidates(candidates: list[RouteCandidate], *, route_context: RoutePolicyContext | None) -> list[RouteCandidate]:
        if route_context and route_context.max_candidate_count is not None:
            return candidates[: max(1, route_context.max_candidate_count)]
        return candidates

    @staticmethod
    def _filter_capacity_candidates(candidates: list[RouteCandidate], *, is_stream: bool) -> list[RouteCandidate]:
        if not candidates:
            return []
        snapshots = ProviderCapacityService.snapshots({item.provider.id for item in candidates})
        filtered: list[RouteCandidate] = []
        for candidate in candidates:
            snapshot = snapshots.get(candidate.provider.id)
            if snapshot is None:
                continue
            if not ProviderCapacityService._has_capacity(candidate.provider, snapshot=snapshot, is_stream=is_stream):
                continue
            if (
                candidate.provider.max_error_rate is not None
                and candidate.provider.max_error_rate > 0
                and candidate.recent_failure_rate * 100 >= candidate.provider.max_error_rate
            ):
                continue
            filtered.append(candidate)
        return filtered

    @staticmethod
    async def _async_filter_capacity_candidates(candidates: list[RouteCandidate], *, is_stream: bool) -> list[RouteCandidate]:
        if not candidates:
            return []
        snapshots = await ProviderCapacityService.async_snapshots({item.provider.id for item in candidates})
        filtered: list[RouteCandidate] = []
        for candidate in candidates:
            snapshot = snapshots.get(candidate.provider.id)
            if snapshot is None:
                continue
            if not ProviderCapacityService._has_capacity(candidate.provider, snapshot=snapshot, is_stream=is_stream):
                continue
            if (
                candidate.provider.max_error_rate is not None
                and candidate.provider.max_error_rate > 0
                and candidate.recent_failure_rate * 100 >= candidate.provider.max_error_rate
            ):
                continue
            filtered.append(candidate)
        return filtered

    @staticmethod
    def _record_diagnostic_reason(
        diagnostics: dict[str, Any],
        reason_code: str,
        *,
        provider: Provider | None = None,
        provider_model: ProviderModel | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        counts = diagnostics.setdefault("reason_counts", {})
        counts[reason_code] = int(counts.get(reason_code, 0) or 0) + 1
        samples = diagnostics.setdefault("samples", [])
        if len(samples) >= RouterService.ROUTE_DIAGNOSTIC_SAMPLE_LIMIT:
            return
        sample: dict[str, Any] = {
            "reason": reason_code,
            "reason_label": RouterService._diagnostic_reason_label(reason_code),
        }
        if provider is not None:
            sample["provider_id"] = provider.id
            sample["provider_name"] = provider.name
        if provider_model is not None:
            sample["provider_model_id"] = provider_model.id
            sample["model_name"] = provider_model.model_name
        if extra:
            sample.update(extra)
        samples.append(sample)

    @staticmethod
    def _diagnostic_reason_label(reason_code: str) -> str:
        return {
            "forced_provider_not_found": "指定中转站不存在",
            "provider_without_models": "中转站未挂载模型",
            "provider_disabled": "中转站已禁用",
            "provider_circuit_open": "中转站已熔断",
            "provider_maintenance_mode": "中转站维护中",
            "provider_not_authorized": "当前密钥未授权该中转站",
            "model_disabled": "中转站模型已禁用",
            "model_globally_disabled": "模型管理中已禁用",
            "model_name_mismatch": "模型名不匹配",
            "stream_not_supported": "模型不支持流式",
            "vision_not_supported": "模型不支持图像",
            "tools_not_supported": "模型不支持工具调用",
            "image_generation_not_supported": "模型不支持图片生成工具",
            "chat_not_supported": "模型不支持 chat/completions",
            "responses_not_supported": "模型不支持 responses",
            "model_circuit_open": "模型已熔断",
            "model_unhealthy": "模型健康状态异常",
            "capacity_snapshot_unavailable": "未获取到容量快照",
            "provider_capacity_exceeded": "中转站容量已满",
            "provider_failure_rate_limited": "中转站失败率超限",
        }.get(reason_code, reason_code)

    @staticmethod
    def _build_diagnostic_summary(diagnostics: dict[str, Any]) -> str:
        counts = diagnostics.get("reason_counts") or {}
        if not counts:
            return "当前未记录到明确的候选筛除原因"
        ordered = sorted(counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))
        return "；".join(
            f"{RouterService._diagnostic_reason_label(reason_code)} {count}"
            for reason_code, count in ordered[:5]
        )

    @staticmethod
    def _build_candidate_cache_key(
        *,
        model_name: str | None,
        route_context: RoutePolicyContext | None,
        require_vision: bool,
        require_stream: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
    ) -> str:
        parts = [
            "route-candidates",
            model_name or "*",
            "vision" if require_vision else "text",
            "stream" if require_stream else "json",
            "tools" if require_tools else "no-tools",
            "imagegen" if require_image_generation else "no-imagegen",
            "chat" if require_chat_completions else "any-chat",
            "responses" if require_responses else "any-responses",
        ]
        if route_context is not None:
            parts.extend(
                [
                    route_context.route_mode,
                    str(route_context.default_provider_id or 0),
                    ",".join(str(item) for item in (route_context.allowed_provider_ids or [])),
                    ",".join(str(item) for item in (route_context.preferred_provider_ids or [])),
                    ",".join(item for item in (route_context.preferred_region_tags or [])),
                    str(route_context.max_candidate_count or 0),
                    str(route_context.latency_bias),
                    str(route_context.success_rate_bias),
                    str(route_context.cost_bias),
                ]
            )
        return "|".join(parts)

    @staticmethod
    def _route_score(
        *,
        provider: Provider,
        provider_model: ProviderModel,
        recent_success_rate: float,
        recent_avg_latency_ms: float | None,
        route_context: RoutePolicyContext | None = None,
    ) -> float:
        health_score = {
            "healthy": 100.0,
            "degraded": 70.0,
            "unknown": 55.0,
            "unhealthy": 0.0,
        }.get(provider_model.health_status, 50.0)
        if provider_model.circuit_state == "half_open":
            health_score -= 25.0

        priority_score = max(0.0, 30.0 - float(provider_model.priority))
        provider_priority_score = max(0.0, 20.0 - float(provider.priority))
        latency_bias = route_context.latency_bias if route_context is not None else 1
        success_rate_bias = route_context.success_rate_bias if route_context is not None else 1
        cost_bias = route_context.cost_bias if route_context is not None else 0
        latency_penalty = min(25.0, (recent_avg_latency_ms or provider_model.last_latency_ms or 0) / 100.0) * max(latency_bias, 0)
        success_score = (recent_success_rate * 40.0) * max(success_rate_bias, 0)
        cost_score = 0.0
        if cost_bias > 0:
            effective_cost = RouterService._effective_model_cost(provider_model)
            if effective_cost is not None:
                cost_score = max(0.0, 30.0 - effective_cost) * cost_bias
        region_bonus = 0.0
        if route_context and route_context.preferred_region_tags and provider.region_tag in set(route_context.preferred_region_tags):
            region_bonus = 20.0

        return health_score + priority_score + provider_priority_score + success_score + cost_score + region_bonus - latency_penalty

    @staticmethod
    def _effective_model_cost(provider_model: ProviderModel) -> float | None:
        values = [item for item in (provider_model.input_price_per_1k, provider_model.output_price_per_1k) if item is not None]
        if not values:
            return None
        return sum(values) / len(values)

    @staticmethod
    def _dynamic_weight(base_weight: int, recent_failure_rate: float) -> float:
        if recent_failure_rate >= 0.8:
            return max(1.0, base_weight * 0.15)
        if recent_failure_rate >= 0.5:
            return max(1.0, base_weight * 0.35)
        if recent_failure_rate >= 0.2:
            return max(1.0, base_weight * 0.6)
        return float(base_weight)

    @staticmethod
    def _should_probe_open_model(provider: Provider, provider_model: ProviderModel, recovery_interval_sec: int, now: datetime) -> bool:
        if not provider.auto_recover_enabled:
            return False
        if provider_model.circuit_opened_at is None:
            return True
        return provider_model.circuit_opened_at + timedelta(seconds=max(10, recovery_interval_sec)) <= now

    @staticmethod
    def _claim_half_open_probe(db: Session, provider_model: ProviderModel, now: datetime) -> bool:
        result = db.execute(
            update(ProviderModel)
            .where(
                ProviderModel.id == provider_model.id,
                ProviderModel.circuit_state == "open",
            )
            .values(
                circuit_state="half_open",
                circuit_opened_at=now,
                last_check_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        provider_model.circuit_state = "half_open"
        provider_model.circuit_opened_at = now
        provider_model.last_check_at = now
        return True

    @staticmethod
    def _weighted_shuffle(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
        remaining = list(candidates)
        ordered: list[RouteCandidate] = []
        while remaining:
            weighted_candidates = [item for item in remaining if item.dynamic_weight > 0]
            if not weighted_candidates:
                ordered.extend(remaining)
                break
            chosen = random.choices(
                weighted_candidates,
                weights=[item.dynamic_weight for item in weighted_candidates],
                k=1,
            )[0]
            ordered.append(chosen)
            remaining.remove(chosen)
        return ordered

    @staticmethod
    def _sticky_order(candidates: list[RouteCandidate], sticky_key: str | None) -> list[RouteCandidate]:
        if not candidates:
            return []
        if not sticky_key:
            return candidates
        return sorted(
            candidates,
            key=lambda item: (
                -RouterService._sticky_affinity_score(item, sticky_key),
                -item.route_score,
                item.provider_model.priority,
                item.provider.priority,
                item.provider.id,
                item.provider_model.id,
            ),
        )

    @staticmethod
    def _sticky_affinity_score(candidate: RouteCandidate, sticky_key: str) -> float:
        digest = hashlib.sha256(
            f"{sticky_key}:{candidate.provider.id}:{candidate.provider_model.id}".encode("utf-8")
        ).hexdigest()
        raw_value = int(digest[:16], 16)
        normalized = max(raw_value / 0xFFFFFFFFFFFFFFFF, 1e-12)
        effective_weight = max(1.0, candidate.dynamic_weight * max(candidate.route_score, 1.0))
        return effective_weight / -math.log(normalized)
