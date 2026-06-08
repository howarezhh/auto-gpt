import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacitySnapshot
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.content_trust_probe_service import ContentTrustProbeService


@dataclass(slots=True)
class RouteCandidate:
    """描述单个可参与路由的 provider/model 候选项。"""

    provider: Provider
    provider_model: ProviderModel
    recent_failure_rate: float = 0.0
    recent_success_rate: float = 1.0
    recent_avg_latency_ms: float | None = None
    route_score: float = 0.0
    dynamic_weight: float = 100.0
    health_tier: int = 1
    sticky_affinity: float = 0.0
    load_factor: float = 0.0


@dataclass(slots=True)
class RecentSessionRoute:
    """记录同一会话最近一次成功使用的模型与提供商。"""

    provider_id: int | None
    provider_model_id: int | None
    model_name: str | None


@dataclass(slots=True)
class RoutePolicyContext:
    """描述一次路由决策的策略上下文。"""

    allowed_provider_ids: list[int] | None = None
    forced_provider_id: int | None = None
    preferred_provider_ids: list[int] | None = None
    preferred_region_tags: list[str] | None = None
    latency_bias: int = 1
    success_rate_bias: int = 1
    cost_bias: int = 0
    require_trusted_provider: bool = False
    content_guard_required: bool = True

    def with_forced_provider_id(self, forced_provider_id: int | None) -> "RoutePolicyContext":
        """返回一个仅修改强制 provider 配置的新上下文对象。"""
        return RoutePolicyContext(
            allowed_provider_ids=list(self.allowed_provider_ids) if self.allowed_provider_ids is not None else None,
            forced_provider_id=forced_provider_id,
            preferred_provider_ids=list(self.preferred_provider_ids) if self.preferred_provider_ids is not None else None,
            preferred_region_tags=list(self.preferred_region_tags) if self.preferred_region_tags is not None else None,
            latency_bias=self.latency_bias,
            success_rate_bias=self.success_rate_bias,
            cost_bias=self.cost_bias,
            require_trusted_provider=self.require_trusted_provider,
            content_guard_required=self.content_guard_required,
        )


class RouterService:
    """负责候选 provider 过滤、打分、排序和不可用诊断。"""

    RECENT_WINDOW_MINUTES = 5
    ROUTE_DIAGNOSTIC_SAMPLE_LIMIT = 8
    MIN_CONTENT_INTEGRITY_SCORE = 20
    CAPABILITY_HEALTH_CACHE_PREFIX = "health-capability-probe"
    _candidate_cache_locks: dict[str, Lock] = {}
    _candidate_cache_locks_guard = Lock()

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
        """筛选满足能力、状态与策略约束的候选路由项。"""
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
        cached_candidates = CacheService.get(cache_key)
        if (
            isinstance(cached_candidates, list)
            and all(isinstance(item, RouteCandidate) for item in cached_candidates)
        ):
            return cached_candidates
        with RouterService._candidate_cache_lock(cache_key):
            cached_candidates = CacheService.get(cache_key)
            if (
                isinstance(cached_candidates, list)
                and all(isinstance(item, RouteCandidate) for item in cached_candidates)
            ):
                return cached_candidates
            return RouterService._load_available_candidates_uncached(
                db,
                cache_key=cache_key,
                model_name=model_name,
                route_context=route_context,
                require_vision=require_vision,
                require_stream=require_stream,
                require_tools=require_tools,
                require_image_generation=require_image_generation,
                require_chat_completions=require_chat_completions,
                require_responses=require_responses,
            )

    @staticmethod
    def _candidate_cache_lock(cache_key: str) -> Lock:
        with RouterService._candidate_cache_locks_guard:
            lock = RouterService._candidate_cache_locks.get(cache_key)
            if lock is None:
                lock = Lock()
                RouterService._candidate_cache_locks[cache_key] = lock
            return lock

    @staticmethod
    def _capability_probe_cache_key(provider_id: int, provider_model_id: int, capability: str) -> str:
        return f"{RouterService.CAPABILITY_HEALTH_CACHE_PREFIX}:{provider_id}:{provider_model_id}:{capability}"

    @staticmethod
    def _capability_probe_failed(
        provider: Provider,
        provider_model: ProviderModel,
        capability: str,
        *,
        endpoint_path: str | None = None,
    ) -> bool:
        capability_keys = RouterService._capability_probe_lookup_keys(capability, endpoint_path=endpoint_path)
        capability_state = ProviderHealthStateService.get_model_capability_state(provider.id, provider_model.id)
        if isinstance(capability_state, dict):
            for capability_key in capability_keys:
                payload = capability_state.get(capability_key)
                if not isinstance(payload, dict):
                    continue
                if capability == "tools":
                    return payload.get("native_ok") is False
                return payload.get("success") is False
        for capability_key in capability_keys:
            payload = CacheService.get(RouterService._capability_probe_cache_key(provider.id, provider_model.id, capability_key))
            if isinstance(payload, dict):
                return payload.get("success") is False
        return False

    @staticmethod
    def _capability_probe_lookup_keys(capability: str, *, endpoint_path: str | None = None) -> list[str]:
        if capability not in {"tools", "vision"}:
            return [capability]
        if endpoint_path == "/chat/completions":
            return [f"{capability}_chat_completions"]
        if endpoint_path == "/responses":
            return [f"{capability}_responses"]
        return [capability]

    @staticmethod
    def _required_endpoint_path(*, require_chat_completions: bool, require_responses: bool) -> str | None:
        if require_chat_completions and not require_responses:
            return "/chat/completions"
        if require_responses and not require_chat_completions:
            return "/responses"
        return None

    @staticmethod
    def _load_available_candidates_uncached(
        db: Session,
        *,
        cache_key: str,
        model_name: str | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
        require_stream: bool = False,
        require_tools: bool = False,
        require_image_generation: bool = False,
        require_chat_completions: bool = False,
        require_responses: bool = False,
    ) -> list[RouteCandidate]:
        providers = ProviderService.list_runtime_providers(db)
        now = datetime.utcnow()
        metrics = LogService.route_metric_summary(db, window_minutes=RouterService.RECENT_WINDOW_MINUTES, requested_model=model_name)
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)
        try:
            capacity_snapshots = ProviderCapacityService.snapshots({provider.id for provider in providers})
        except Exception:
            capacity_snapshots = {}

        required_endpoint_path = RouterService._required_endpoint_path(
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        candidates: list[RouteCandidate] = []
        for provider in providers:
            if not provider.enabled or provider.circuit_state == "open" or provider.maintenance_mode_enabled:
                continue
            if RouterService._provider_blocked_by_content_policy(provider, route_context=route_context):
                continue
            if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                continue
            if require_chat_completions and not ProviderService.provider_supports_chat_completions(provider):
                continue
            if require_responses and not ProviderService.provider_supports_responses(provider):
                continue
            for provider_model in provider.provider_models:
                if not provider_model.enabled:
                    continue
                if RouterService._provider_model_blocked_by_content_policy(provider_model, provider=provider, route_context=route_context):
                    continue
                if provider_model.model_name not in enabled_model_names:
                    continue
                if model_name and provider_model.model_name != model_name:
                    continue
                if require_stream and not provider_model.supports_stream:
                    continue
                if require_chat_completions and not provider_model.supports_chat_completions:
                    continue
                if require_responses and not provider_model.supports_responses:
                    continue
                if require_vision and not provider_model.supports_vision:
                    continue
                if require_image_generation and not ProviderService.provider_model_supports_image_generation(provider_model):
                    continue
                if require_vision and RouterService._capability_probe_failed(
                    provider,
                    provider_model,
                    "vision",
                    endpoint_path=required_endpoint_path,
                ):
                    continue
                if require_image_generation and RouterService._capability_probe_failed(provider, provider_model, "image_generation"):
                    continue
                if provider_model.circuit_state == "open":
                    if not RouterService._should_probe_open_model(
                        provider=provider,
                        provider_model=provider_model,
                        recovery_interval_sec=ProviderService.get_effective_recovery_probe_interval_sec(db, provider),
                        now=now,
                    ):
                        continue
                    # 打开熔断后仅允许极少量探测流量进入 half-open 探针流程。
                    if not RouterService._claim_half_open_probe(db, provider_model, now):
                        continue
                metric = metrics.get((provider.id, provider_model.model_name), {})
                recent_failure_rate = float(metric.get("failure_rate", 0.0))
                recent_success_rate = float(metric.get("success_rate", 1.0))
                recent_avg_latency_ms = metric.get("avg_latency_ms")
                model_health_state = ProviderHealthStateService.get_model_state(provider.id, provider_model.id)
                provider_health_state = ProviderHealthStateService.get_provider_state(provider.id)
                health_tier = RouterService._health_tier(
                    provider=provider,
                    provider_model=provider_model,
                    model_health_state=model_health_state,
                    provider_health_state=provider_health_state,
                )
                route_score = RouterService._route_score(
                    provider=provider,
                    provider_model=provider_model,
                    recent_success_rate=recent_success_rate,
                    recent_avg_latency_ms=recent_avg_latency_ms,
                    is_stream=require_stream,
                    model_health_state=model_health_state,
                    provider_health_state=provider_health_state,
                    capacity_snapshot=capacity_snapshots.get(provider.id),
                    route_context=route_context,
                )
                candidates.append(
                    RouteCandidate(
                        provider=provider,
                        provider_model=provider_model,
                        recent_failure_rate=recent_failure_rate,
                        recent_success_rate=recent_success_rate,
                        recent_avg_latency_ms=recent_avg_latency_ms,
                        route_score=route_score,
                        health_tier=health_tier,
                    )
                )
        return CacheService.set(cache_key, candidates, ttl_seconds=RouterService._route_candidate_cache_ttl_seconds())

    @staticmethod
    def _route_candidate_cache_ttl_seconds() -> int:
        try:
            setting = SettingService.get_cached()
            return max(1, min(int(getattr(setting, "route_candidate_cache_ttl_sec", 10) or 10), 60))
        except Exception:
            return 10

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
        """返回经过过滤和排序后的最终候选列表。"""
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
        """异步版本的候选排序逻辑。"""
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
        """异步分析为什么没有可用路由候选。"""
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
        required_endpoint_path = RouterService._required_endpoint_path(
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
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
            content_policy_reason = RouterService._content_policy_diagnostic_reason(provider, route_context=route_context)
            if content_policy_reason:
                RouterService._record_diagnostic_reason(diagnostics, content_policy_reason, provider=provider)
                continue
            if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                RouterService._record_diagnostic_reason(diagnostics, "provider_not_authorized", provider=provider)
                continue
            if require_chat_completions and not ProviderService.provider_supports_chat_completions(provider):
                RouterService._record_diagnostic_reason(diagnostics, "provider_chat_protocol_not_supported", provider=provider)
                continue
            if require_responses and not ProviderService.provider_supports_responses(provider):
                RouterService._record_diagnostic_reason(diagnostics, "provider_responses_protocol_not_supported", provider=provider)
                continue
            for provider_model in provider.provider_models:
                diagnostics["mounted_model_total"] += 1
                if model_name and provider_model.model_name == model_name:
                    diagnostics["matching_model_mount_count"] += 1
                if not provider_model.enabled:
                    RouterService._record_diagnostic_reason(diagnostics, "model_disabled", provider=provider, provider_model=provider_model)
                    continue
                if RouterService._provider_model_blocked_by_content_policy(provider_model, provider=provider, route_context=route_context):
                    RouterService._record_diagnostic_reason(diagnostics, "model_content_integrity_blocked", provider=provider, provider_model=provider_model)
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
                if require_chat_completions and not provider_model.supports_chat_completions:
                    RouterService._record_diagnostic_reason(diagnostics, "chat_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_responses and not provider_model.supports_responses:
                    RouterService._record_diagnostic_reason(diagnostics, "responses_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_vision and not provider_model.supports_vision:
                    RouterService._record_diagnostic_reason(diagnostics, "vision_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_image_generation and not ProviderService.provider_model_supports_image_generation(provider_model):
                    RouterService._record_diagnostic_reason(diagnostics, "image_generation_not_supported", provider=provider, provider_model=provider_model)
                    continue
                if require_vision and RouterService._capability_probe_failed(
                    provider,
                    provider_model,
                    "vision",
                    endpoint_path=required_endpoint_path,
                ):
                    RouterService._record_diagnostic_reason(diagnostics, "vision_probe_unhealthy", provider=provider, provider_model=provider_model)
                    continue
                if require_image_generation and RouterService._capability_probe_failed(provider, provider_model, "image_generation"):
                    RouterService._record_diagnostic_reason(diagnostics, "image_generation_probe_unhealthy", provider=provider, provider_model=provider_model)
                    continue
                if provider_model.circuit_state == "open" and not RouterService._should_probe_open_model(
                    provider=provider,
                    provider_model=provider_model,
                    recovery_interval_sec=ProviderService.get_effective_recovery_probe_interval_sec(db, provider),
                    now=now,
                ):
                    RouterService._record_diagnostic_reason(diagnostics, "model_circuit_open", provider=provider, provider_model=provider_model)
                    continue
                pre_capacity_candidates.append(
                    RouteCandidate(
                        provider=provider,
                        provider_model=provider_model,
                        recent_failure_rate=float(metrics.get((provider.id, provider_model.model_name), {}).get("failure_rate", 0.0)),
                        health_tier=RouterService._health_tier(provider=provider, provider_model=provider_model),
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
        db: Session | None,
        candidates: list[RouteCandidate],
        *,
        sticky_key: str | None,
        route_context: RoutePolicyContext | None,
    ) -> list[RouteCandidate]:
        setting = None if route_context is not None else (
            SettingService.get_or_create(db) if db is not None else SettingService.get_cached()
        )
        if not candidates:
            return []

        if route_context and route_context.preferred_provider_ids:
            preferred_set = set(route_context.preferred_provider_ids)
            preferred = [item for item in candidates if item.provider.id in preferred_set]
            others = [item for item in candidates if item.provider.id not in preferred_set]
            candidates = preferred + others

        recent_route = RouterService.load_recent_session_route(db, sticky_key)
        sorted_candidates = RouterService._primary_route_order(
            candidates,
            sticky_key=sticky_key,
            recent_route=recent_route,
        )
        return RouterService._trim_candidates(sorted_candidates, route_context=route_context)

    @staticmethod
    def load_recent_session_route(db: Session | None, sticky_key: str | None) -> RecentSessionRoute | None:
        """只读取同一会话最近一次成功路由结果，新会话或无会话标识不做粘性查询。"""
        if not isinstance(sticky_key, str) or not sticky_key.strip():
            return None
        key = sticky_key.strip()
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        try:
            row = db.execute(
                select(
                    RequestLog.provider_id,
                    RequestLog.resolved_provider_model_id,
                    RequestLog.model_name,
                    RequestLog.requested_model,
                )
                .where(
                    RequestLog.success.is_(True),
                    RequestLog.provider_id.is_not(None),
                    or_(RequestLog.session_id == key, RequestLog.conversation_key == key),
                )
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            provider_id, provider_model_id, model_name, requested_model = row
            return RecentSessionRoute(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                model_name=model_name or requested_model,
            )
        finally:
            if close_db:
                db.close()

    @staticmethod
    def _primary_route_order(
        candidates: list[RouteCandidate],
        *,
        sticky_key: str | None,
        recent_route: RecentSessionRoute | None,
    ) -> list[RouteCandidate]:
        """统一主路由策略：硬筛选后优先复用同会话最近成功目标，再做健康优先分发。"""
        ordered: list[RouteCandidate] = []
        seen: set[tuple[int, int]] = set()
        recent_candidate = RouterService._recent_route_candidate(candidates, recent_route)
        if recent_candidate is not None:
            key = (recent_candidate.provider.id, recent_candidate.provider_model.id)
            ordered.append(recent_candidate)
            seen.add(key)
        for health_tier in sorted({candidate.health_tier for candidate in candidates}):
            tier_candidates = [
                candidate
                for candidate in candidates
                if candidate.health_tier == health_tier
                and (candidate.provider.id, candidate.provider_model.id) not in seen
            ]
            balanced = RouterService._balanced_order(tier_candidates)
            for candidate in balanced:
                key = (candidate.provider.id, candidate.provider_model.id)
                if key in seen:
                    continue
                ordered.append(candidate)
                seen.add(key)
        return ordered

    @staticmethod
    def _recent_route_candidate(
        candidates: list[RouteCandidate],
        recent_route: RecentSessionRoute | None,
    ) -> RouteCandidate | None:
        if recent_route is None:
            return None
        if recent_route.provider_model_id is not None:
            exact = next(
                (item for item in candidates if item.provider_model.id == recent_route.provider_model_id),
                None,
            )
            if exact is not None:
                exact.sticky_affinity = max(exact.sticky_affinity, 1_000_000.0)
                return exact
        if recent_route.provider_id is not None:
            same_provider = next(
                (item for item in candidates if item.provider.id == recent_route.provider_id),
                None,
            )
            if same_provider is not None:
                same_provider.sticky_affinity = max(same_provider.sticky_affinity, 500_000.0)
                return same_provider
        return None

    @staticmethod
    def _trim_candidates(candidates: list[RouteCandidate], *, route_context: RoutePolicyContext | None) -> list[RouteCandidate]:
        return candidates[: RouterService._effective_max_candidate_count(route_context)]

    @staticmethod
    def _effective_max_candidate_count(route_context: RoutePolicyContext | None) -> int:
        try:
            setting = SettingService.get_cached()
            value = getattr(setting, "max_candidate_count", 10)
        except Exception:
            value = 10
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 500))

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
            candidate.load_factor = RouterService._capacity_load_factor(candidate.provider, snapshot, is_stream=is_stream)
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
            candidate.load_factor = RouterService._capacity_load_factor(candidate.provider, snapshot, is_stream=is_stream)
            if (
                candidate.provider.max_error_rate is not None
                and candidate.provider.max_error_rate > 0
                and candidate.recent_failure_rate * 100 >= candidate.provider.max_error_rate
            ):
                continue
            filtered.append(candidate)
        return filtered

    @staticmethod
    def _filter_failure_rate_candidates(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
        return [
            candidate
            for candidate in candidates
            if not (
                candidate.provider.max_error_rate is not None
                and candidate.provider.max_error_rate > 0
                and candidate.recent_failure_rate * 100 >= candidate.provider.max_error_rate
            )
        ]

    @staticmethod
    def _health_tier(
        *,
        provider: Provider,
        provider_model: ProviderModel,
        model_health_state: dict[str, Any] | None = None,
        provider_health_state: dict[str, Any] | None = None,
    ) -> int:
        model_health_state = model_health_state or {}
        provider_health_state = provider_health_state or {}
        circuit_state = str(model_health_state.get("circuit_state") or provider_model.circuit_state or "closed")
        if circuit_state == "open":
            return 3
        if circuit_state == "half_open":
            return 1
        model_status = str(model_health_state.get("health_status") or provider_model.health_status or "unknown")
        provider_status = str(provider_health_state.get("health_status") or provider.health_status or "unknown")
        statuses = {model_status, provider_status}
        if "unhealthy" in statuses:
            return 2
        if "healthy" in statuses or "degraded" in statuses:
            return 0
        return 1

    @staticmethod
    def _sort_by_health_and_sticky(candidates: list[RouteCandidate], *, sticky_key: str | None) -> list[RouteCandidate]:
        for candidate in candidates:
            candidate.sticky_affinity = RouterService._sticky_affinity_score(candidate, sticky_key) if sticky_key else 0.0
        return sorted(
            candidates,
            key=lambda item: (
                item.health_tier,
                -item.sticky_affinity,
                item.load_factor,
                -item.route_score,
                item.provider_model.priority,
                item.provider.priority,
                item.provider.id,
                item.provider_model.id,
            ),
        )

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
            "forced_provider_not_found": "指定提供商不存在",
            "provider_without_models": "提供商未挂载模型",
            "provider_disabled": "提供商已禁用",
            "provider_circuit_open": "提供商已熔断",
            "provider_maintenance_mode": "提供商维护中",
            "provider_not_authorized": "当前密钥未授权该提供商",
            "provider_chat_protocol_not_supported": "提供商不支持 Chat Completions API",
            "provider_responses_protocol_not_supported": "提供商不支持 Responses API",
            "model_disabled": "提供商模型已禁用",
            "model_globally_disabled": "模型管理中已禁用",
            "model_name_mismatch": "模型名不匹配",
            "stream_not_supported": "模型不支持流式",
            "vision_not_supported": "模型不支持图像",
            "tools_not_supported": "模型不支持工具调用",
            "image_generation_not_supported": "模型不支持图片生成工具",
            "tools_probe_unhealthy": "工具调用探针不可用",
            "vision_probe_unhealthy": "图像理解探针不可用",
            "image_generation_probe_unhealthy": "图片生成探针不可用",
            "chat_not_supported": "模型不支持 chat/completions",
            "responses_not_supported": "模型不支持 responses",
            "model_circuit_open": "模型已熔断",
            "model_unhealthy": "模型健康状态异常",
            "capacity_snapshot_unavailable": "未获取到容量快照",
            "provider_capacity_exceeded": "提供商容量已满",
            "provider_failure_rate_limited": "提供商失败率超限",
            "provider_trust_blocked": "提供商信任等级已阻断",
            "provider_content_integrity_blocked": "提供商内容完整性已隔离",
            "provider_content_integrity_score_too_low": "提供商内容完整性评分过低",
            "provider_trusted_required": "当前策略要求可信及以上提供商",
            "model_content_integrity_blocked": "模型内容完整性已隔离",
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
                    ",".join(str(item) for item in (route_context.allowed_provider_ids or [])),
                    ",".join(str(item) for item in (route_context.preferred_provider_ids or [])),
                    ",".join(item for item in (route_context.preferred_region_tags or [])),
                    str(RouterService._effective_max_candidate_count(route_context)),
                    str(route_context.latency_bias),
                    str(route_context.success_rate_bias),
                    str(route_context.cost_bias),
                    "trusted" if route_context.require_trusted_provider else "any-trust",
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
        is_stream: bool = False,
        model_health_state: dict[str, Any] | None = None,
        provider_health_state: dict[str, Any] | None = None,
        capacity_snapshot: ProviderCapacitySnapshot | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> float:
        model_health_state = model_health_state or {}
        provider_health_state = provider_health_state or {}
        effective_health_status = str(model_health_state.get("health_status") or provider_model.health_status or "unknown")
        effective_circuit_state = str(model_health_state.get("circuit_state") or provider_model.circuit_state or "closed")
        health_score = {
            "healthy": 100.0,
            "degraded": 60.0,
            "unknown": 55.0,
            "unhealthy": 0.0,
        }.get(effective_health_status, 50.0)
        if effective_circuit_state == "half_open":
            health_score = min(health_score, 35.0)
        elif effective_circuit_state == "open":
            health_score = 0.0

        priority_score = max(0.0, 30.0 - float(provider_model.priority))
        provider_priority_score = max(0.0, 20.0 - float(provider.priority))
        latency_bias = route_context.latency_bias if route_context is not None else 1
        success_rate_bias = route_context.success_rate_bias if route_context is not None else 1
        cost_bias = route_context.cost_bias if route_context is not None else 0
        success_rate = float(model_health_state.get("success_rate_5m") if model_health_state.get("success_rate_5m") is not None else recent_success_rate)
        failure_rate = float(model_health_state.get("failure_rate_5m") or max(0.0, 1.0 - success_rate))
        ewma_latency = model_health_state.get("ewma_latency_ms")
        ewma_ttfb = model_health_state.get("ewma_ttfb_ms")
        latency_source = ewma_latency if ewma_latency is not None else (recent_avg_latency_ms or provider_model.last_latency_ms or provider_health_state.get("ewma_latency_ms") or 0)
        latency_penalty = min(30.0, float(latency_source or 0) / 100.0) * max(latency_bias, 0)
        ttfb_penalty = min(25.0, float(ewma_ttfb or 0) / 100.0) if is_stream else 0.0
        success_score = (success_rate * 40.0) * max(success_rate_bias, 0)
        recent_error_penalty = min(45.0, failure_rate * 100.0)
        saturation_penalty = RouterService._saturation_penalty(provider, capacity_snapshot, is_stream=is_stream)
        cost_score = 0.0
        if cost_bias > 0:
            effective_cost = RouterService._effective_model_cost(provider_model)
            if effective_cost is not None:
                cost_score = max(0.0, 30.0 - effective_cost) * cost_bias
        region_bonus = 0.0
        if route_context and route_context.preferred_region_tags and provider.region_tag in set(route_context.preferred_region_tags):
            region_bonus = 20.0
        return (
            health_score
            + priority_score
            + provider_priority_score
            + success_score
            + cost_score
            + region_bonus
            - latency_penalty
            - ttfb_penalty
            - saturation_penalty
            - recent_error_penalty
        )

    @staticmethod
    def _provider_blocked_by_content_policy(provider: Provider, *, route_context: RoutePolicyContext | None) -> bool:
        return RouterService._content_policy_diagnostic_reason(provider, route_context=route_context) is not None

    @staticmethod
    def _content_policy_diagnostic_reason(provider: Provider, *, route_context: RoutePolicyContext | None) -> str | None:
        decision = ContentTrustProbeService.get_trust_decision_for_route(
            provider,
            route_context=route_context,
        )
        return None if decision.get("allowed") else str(decision.get("reason") or "provider_content_integrity_blocked")

    @staticmethod
    def _provider_model_blocked_by_content_policy(
        provider_model: ProviderModel,
        *,
        provider: Provider | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> bool:
        if provider is not None:
            decision = ContentTrustProbeService.get_trust_decision_for_route(
                provider,
                provider_model=provider_model,
                route_context=route_context,
            )
            return not bool(decision.get("allowed"))
        return str(getattr(provider_model, "content_integrity_status", "unknown") or "unknown") == "blocked"

    @staticmethod
    def _saturation_penalty(
        provider: Provider,
        capacity_snapshot: ProviderCapacitySnapshot | None,
        *,
        is_stream: bool,
    ) -> float:
        if capacity_snapshot is None:
            return 0.0
        ratios: list[float] = []
        if provider.max_active_requests and provider.max_active_requests > 0:
            ratios.append(capacity_snapshot.active_requests / provider.max_active_requests)
        if is_stream and provider.max_active_streams and provider.max_active_streams > 0:
            ratios.append(capacity_snapshot.active_streams / provider.max_active_streams)
        if provider.max_qps and provider.max_qps > 0:
            ratios.append(capacity_snapshot.current_qps / provider.max_qps)
        if provider.max_rpm and provider.max_rpm > 0:
            ratios.append(capacity_snapshot.current_rpm / provider.max_rpm)
        if not ratios:
            return 0.0
        return min(40.0, max(ratios) * 40.0)

    @staticmethod
    def _capacity_load_factor(
        provider: Provider,
        capacity_snapshot: ProviderCapacitySnapshot | None,
        *,
        is_stream: bool,
    ) -> float:
        if capacity_snapshot is None:
            return 0.0
        ratios: list[float] = []
        if provider.max_active_requests and provider.max_active_requests > 0:
            ratios.append(capacity_snapshot.active_requests / provider.max_active_requests)
        if is_stream and provider.max_active_streams and provider.max_active_streams > 0:
            ratios.append(capacity_snapshot.active_streams / provider.max_active_streams)
        if provider.max_qps and provider.max_qps > 0:
            ratios.append(capacity_snapshot.current_qps / provider.max_qps)
        if provider.max_rpm and provider.max_rpm > 0:
            ratios.append(capacity_snapshot.current_rpm / provider.max_rpm)
        if not ratios:
            return 0.0
        return max(0.0, min(1.0, max(ratios)))

    @staticmethod
    def _effective_model_cost(provider_model: ProviderModel) -> float | None:
        values = [item for item in (provider_model.input_price_per_1k, provider_model.output_price_per_1k) if item is not None]
        if not values:
            return None
        return sum(values) / len(values)

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
    def _balanced_order(candidates: list[RouteCandidate]) -> list[RouteCandidate]:
        return sorted(
            candidates,
            key=lambda item: (
                item.load_factor,
                -item.route_score,
                item.provider_model.priority,
                item.provider.priority,
                item.provider.id,
                item.provider_model.id,
            ),
        )

    @staticmethod
    def _route_selection_weight(candidate: RouteCandidate) -> float:
        health_multiplier = {
            0: 1.0,
            1: 0.35,
            2: 0.05,
            3: 0.01,
        }.get(int(candidate.health_tier or 0), 0.05)
        load_multiplier = max(0.01, 1.0 - max(0.0, min(1.0, float(candidate.load_factor or 0.0))))
        dynamic_weight = max(0.0, float(candidate.dynamic_weight or 0.0))
        route_score = max(1.0, float(candidate.route_score or 0.0))
        return dynamic_weight * route_score * health_multiplier * load_multiplier

    @staticmethod
    def _sticky_order(candidates: list[RouteCandidate], sticky_key: str | None) -> list[RouteCandidate]:
        if not candidates:
            return []
        if not sticky_key:
            return RouterService._sort_by_health_and_sticky(candidates, sticky_key=None)
        return sorted(
            candidates,
            key=lambda item: (
                item.health_tier,
                -RouterService._sticky_affinity_score(item, sticky_key),
                item.load_factor,
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
        effective_score = max(candidate.route_score, 1.0)
        return effective_score / -math.log(normalized)
