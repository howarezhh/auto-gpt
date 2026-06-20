from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.cache_service import CacheService
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.routing.context import RecentSessionRoute, RoutePolicyContext


CAPABILITY_HEALTH_CACHE_PREFIX = "health-capability-probe"
ROUTE_DIAGNOSTIC_SAMPLE_LIMIT = 8
RAW_CANDIDATE_LIMIT = 5000
ROUTE_SCORE_TIE_BUCKET = 10.0


def parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cache_list_digest(values: list[Any] | None) -> str:
    if values is None:
        return "*"
    if not values:
        return "-"
    normalized = ",".join(str(item) for item in values)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def capability_probe_cache_key(provider_id: int, provider_model_id: int, capability: str) -> str:
    return f"{CAPABILITY_HEALTH_CACHE_PREFIX}:{provider_id}:{provider_model_id}:{capability}"


def capability_probe_lookup_keys(capability: str, *, endpoint_path: str | None = None) -> list[str]:
    if capability not in {"tools", "vision"}:
        return [capability]
    if endpoint_path == "/chat/completions":
        return [f"{capability}_chat_completions"]
    if endpoint_path == "/responses":
        return [f"{capability}_responses"]
    return [capability]


def capability_probe_failed(
    provider: Provider,
    provider_model: ProviderModel,
    capability: str,
    *,
    endpoint_path: str | None = None,
) -> bool:
    capability_keys = capability_probe_lookup_keys(capability, endpoint_path=endpoint_path)
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
        payload = CacheService.get(capability_probe_cache_key(provider.id, provider_model.id, capability_key))
        if isinstance(payload, dict):
            return payload.get("success") is False
    return False


def required_endpoint_path(*, require_chat_completions: bool, require_responses: bool) -> str | None:
    if require_chat_completions and not require_responses:
        return "/chat/completions"
    if require_responses and not require_chat_completions:
        return "/responses"
    return None


def normalize_required_upstream_protocol_type(protocol_type: str | None) -> str | None:
    if protocol_type is None:
        return None
    normalized = str(protocol_type).strip().lower()
    if not normalized or normalized in {"openai", "openai_compatible", "both", "chat_completions", "responses"}:
        return None
    if normalized == "gemini":
        return "gemini"
    if normalized in {"claude", "anthropic", "claude_messages"}:
        return "claude_messages"
    return normalized


def provider_endpoint_protocol_allowed(
    provider: Provider,
    *,
    require_chat_completions: bool,
    require_responses: bool,
    required_upstream_protocol_type: str | None,
) -> bool:
    required_native = normalize_required_upstream_protocol_type(required_upstream_protocol_type)
    if required_native:
        return (
            ProviderService.provider_protocol_type(provider) == required_native
            or ProviderService.provider_uses_native_adapter(provider)
            or ProviderService.provider_protocol_type(provider) in {"both", "chat_completions", "responses"}
        )
    if require_chat_completions and not ProviderService.provider_supports_chat_completions(provider):
        return ProviderService.provider_uses_native_adapter(provider)
    if require_responses and not ProviderService.provider_supports_responses(provider):
        return ProviderService.provider_uses_native_adapter(provider)
    return True


def provider_model_endpoint_protocol_allowed(
    provider: Provider,
    provider_model: ProviderModel,
    *,
    require_chat_completions: bool,
    require_responses: bool,
    required_upstream_protocol_type: str | None,
) -> bool:
    required_native = normalize_required_upstream_protocol_type(required_upstream_protocol_type)
    actual_native = ProviderService.provider_or_model_native_protocol(provider, provider_model)
    if required_native:
        return actual_native == required_native
    if actual_native:
        return require_chat_completions or require_responses
    if require_chat_completions and not provider_model.supports_chat_completions:
        return False
    if require_responses and not provider_model.supports_responses:
        return False
    return True


def effective_health_gate_mode(route_context: RoutePolicyContext | None) -> str:
    raw_value = getattr(route_context, "health_gate_mode", None) if route_context is not None else None
    if raw_value is None:
        try:
            raw_value = getattr(SettingService.get_cached(), "route_health_gate_mode", None)
        except Exception:
            raw_value = None
    normalized = str(raw_value or "permissive").strip().lower()
    if normalized in {"healthy", "healthy_only", "only_healthy"}:
        return "healthy_only"
    if normalized in {"trusted_healthy", "trusted+healthy", "trusted_only_healthy"}:
        return "trusted_healthy"
    return "permissive"


def health_gate_statuses(provider: Provider, provider_model: ProviderModel) -> tuple[str, str]:
    model_health_state = ProviderHealthStateService.get_model_state(provider.id, provider_model.id) or {}
    provider_health_state = ProviderHealthStateService.get_provider_state(provider.id) or {}
    model_status = str(model_health_state.get("health_status") or provider_model.health_status or "unknown")
    provider_status = str(provider_health_state.get("health_status") or provider.health_status or "unknown")
    return provider_status, model_status


def health_gate_diagnostic_reason(
    provider: Provider,
    provider_model: ProviderModel,
    *,
    route_context: RoutePolicyContext | None,
) -> str | None:
    mode = effective_health_gate_mode(route_context)
    if mode == "permissive":
        return None
    provider_status, model_status = health_gate_statuses(provider, provider_model)
    if provider_status != "healthy":
        return "provider_health_gate_not_healthy"
    if model_status != "healthy":
        return "model_health_gate_not_healthy"
    if mode == "trusted_healthy" and str(getattr(provider, "trust_level", "standard") or "standard") not in {"official", "trusted"}:
        return "provider_health_gate_not_trusted"
    return None


def content_policy_diagnostic_reason(provider: Provider, *, route_context: RoutePolicyContext | None) -> str | None:
    decision = ContentTrustProbeService.get_trust_decision_for_route(provider, route_context=route_context)
    return None if decision.get("allowed") else str(decision.get("reason") or "provider_content_integrity_blocked")


def provider_model_blocked_by_content_policy(
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


def capacity_load_factor(
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


def saturation_penalty(
    provider: Provider,
    capacity_snapshot: ProviderCapacitySnapshot | None,
    *,
    is_stream: bool,
) -> float:
    return min(40.0, capacity_load_factor(provider, capacity_snapshot, is_stream=is_stream) * 40.0)


def should_probe_open_model(provider: Provider, provider_model: ProviderModel, recovery_interval_sec: int, now: datetime) -> bool:
    if not provider.auto_recover_enabled:
        return False
    if provider_model.circuit_opened_at is None:
        return True
    return provider_model.circuit_opened_at + timedelta(seconds=max(10, recovery_interval_sec)) <= now


def claim_half_open_probe(db: Session, provider_model: ProviderModel, now: datetime) -> bool:
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


def health_tier(
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


def effective_max_candidate_count(route_context: RoutePolicyContext | None) -> int:
    try:
        value = getattr(SettingService.get_cached(), "max_candidate_count", 10)
    except Exception:
        value = 10
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 10
    return max(1, min(parsed, 500))


def effective_candidate_expand_count(route_context: RoutePolicyContext | None) -> int:
    try:
        value = getattr(SettingService.get_cached(), "route_candidate_expand_count", 5)
    except Exception:
        value = 5
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return max(0, min(parsed, 100))


def effective_candidate_attempt_count(route_context: RoutePolicyContext | None) -> int:
    return min(
        RAW_CANDIDATE_LIMIT,
        effective_max_candidate_count(route_context) + effective_candidate_expand_count(route_context),
    )


def diagnostic_reason_label(reason_code: str) -> str:
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
        "model_name_mismatch": "模型ID不匹配",
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
        "model_unhealthy": "模型不可用",
        "capacity_snapshot_unavailable": "未获取到容量快照",
        "provider_capacity_exceeded": "提供商容量已满",
        "provider_trust_blocked": "提供商信任等级已阻断",
        "provider_content_integrity_blocked": "提供商内容完整性已隔离",
        "provider_content_integrity_score_too_low": "提供商内容完整性评分过低",
        "provider_trusted_required": "当前策略要求可信及以上提供商",
        "model_content_integrity_blocked": "模型内容完整性已隔离",
        "model_content_integrity_score_too_low": "模型内容完整性评分过低",
        "protocol_mismatch": "上游原生协议不匹配",
        "provider_health_gate_not_healthy": "提供商未达到可用性门槛",
        "model_health_gate_not_healthy": "模型未达到可用性门槛",
        "provider_health_gate_not_trusted": "提供商未达到可信且可用性门槛",
    }.get(reason_code, reason_code)


def record_diagnostic_reason(
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
    if len(samples) >= ROUTE_DIAGNOSTIC_SAMPLE_LIMIT:
        return
    sample: dict[str, Any] = {
        "reason": reason_code,
        "reason_label": diagnostic_reason_label(reason_code),
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


def build_diagnostic_summary(diagnostics: dict[str, Any]) -> str:
    counts = diagnostics.get("reason_counts") or {}
    if not counts:
        return "当前未记录到明确的候选筛除原因"
    ordered = sorted(counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))
    return "；".join(f"{diagnostic_reason_label(reason_code)} {count}" for reason_code, count in ordered[:5])


def load_recent_session_route(db: Session | None, sticky_key: str | None) -> RecentSessionRoute | None:
    if db is None or not sticky_key:
        return None
    row = db.execute(
        select(RequestLog.provider_id, RequestLog.provider_model_id, RequestLog.model_name)
        .where(
            RequestLog.session_sticky_key == sticky_key,
            RequestLog.success.is_(True),
            RequestLog.provider_id.is_not(None),
        )
        .order_by(RequestLog.created_at.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    provider_id, provider_model_id, model_name = row
    return RecentSessionRoute(provider_id=provider_id, provider_model_id=provider_model_id, model_name=model_name)
