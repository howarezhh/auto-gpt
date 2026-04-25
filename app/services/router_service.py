import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
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

    def with_forced_provider_id(self, forced_provider_id: int | None) -> "RoutePolicyContext":
        return RoutePolicyContext(
            route_mode=self.route_mode,
            default_provider_id=self.default_provider_id,
            manual_allow_fallback=self.manual_allow_fallback,
            allowed_provider_ids=list(self.allowed_provider_ids) if self.allowed_provider_ids is not None else None,
            forced_provider_id=forced_provider_id,
        )


class RouterService:
    RECENT_WINDOW_MINUTES = 5

    @staticmethod
    def get_available_candidates(
        db: Session,
        model_name: str | None = None,
        route_context: RoutePolicyContext | None = None,
        require_vision: bool = False,
    ) -> list[RouteCandidate]:
        providers = ProviderService.list_providers(db)
        setting = SettingService.get_or_create(db)
        now = datetime.utcnow()
        metrics = LogService.route_metric_summary(db, window_minutes=RouterService.RECENT_WINDOW_MINUTES, requested_model=model_name)
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)

        candidates: list[RouteCandidate] = []
        for provider in providers:
            if not provider.enabled or provider.circuit_state == "open":
                continue
            if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                continue
            for provider_model in provider.provider_models:
                if not provider_model.enabled:
                    continue
                if enabled_model_names and provider_model.model_name not in enabled_model_names:
                    continue
                if model_name and provider_model.model_name != model_name:
                    continue
                if require_vision and not provider_model.supports_vision:
                    continue
                if provider_model.circuit_state == "open":
                    if not RouterService._should_probe_open_model(provider_model, setting.recovery_probe_interval_sec, now):
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
    ) -> list[RouteCandidate]:
        setting = SettingService.get_or_create(db)
        candidates = RouterService.get_available_candidates(
            db,
            model_name=model_name,
            route_context=route_context,
            require_vision=require_vision,
        )
        effective_forced_provider_id = route_context.forced_provider_id if route_context and route_context.forced_provider_id is not None else forced_provider_id
        if effective_forced_provider_id is not None:
            candidates = [item for item in candidates if item.provider.id == effective_forced_provider_id]
        if not candidates:
            return []

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
            return ordered

        if route_mode == "failover":
            append_candidate(default_candidate)
            for candidate in sorted_candidates:
                append_candidate(candidate)
            return ordered

        if route_mode == "weighted":
            for candidate in RouterService._weighted_shuffle(sorted_candidates):
                append_candidate(candidate)
            return ordered

        if route_mode == "sticky":
            for candidate in RouterService._sticky_order(sorted_candidates, sticky_key):
                append_candidate(candidate)
            return ordered

        return sorted_candidates

    @staticmethod
    def _route_score(
        *,
        provider: Provider,
        provider_model: ProviderModel,
        recent_success_rate: float,
        recent_avg_latency_ms: float | None,
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
        latency_penalty = min(25.0, (recent_avg_latency_ms or provider_model.last_latency_ms or 0) / 100.0)

        return health_score + priority_score + provider_priority_score + (recent_success_rate * 40.0) - latency_penalty

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
    def _should_probe_open_model(provider_model: ProviderModel, recovery_interval_sec: int, now: datetime) -> bool:
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
