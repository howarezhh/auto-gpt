from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.schemas.content_guard import ContentGuardRulesUpdate, ContentGuardRunRequest, ContentGuardSettingsUpdate, ContentGuardTextInspectRequest
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.provider_service import (
    CONTENT_INTEGRITY_STATUS_LABELS,
    MODEL_TRUST_STATUS_LABELS,
    PROVIDER_TRUST_LEVEL_LABELS,
    ProviderService,
)
from app.services.setting_service import SettingService
from app.utils.json_utils import loads_json


class ContentGuardModuleService:
    """内容完整性防护模块的统一配置、概览与检测入口。"""

    PROBE_LABELS = ContentTrustProbeService.PROBE_LABELS

    @staticmethod
    def build_overview(db: Session) -> dict[str, Any]:
        providers = list(
            db.scalars(
                select(Provider)
                .order_by(Provider.priority.asc(), Provider.id.asc())
            )
        )
        blocked_count = sum(
            1
            for item in providers
            if item.trust_level == "blocked" or item.content_integrity_status == "blocked"
        )
        low_trust_count = sum(1 for item in providers if item.trust_level == "low")
        review_count = sum(
            1
            for item in providers
            if item.content_integrity_status in {"unknown", "degraded"}
        )
        since = datetime.utcnow() - timedelta(hours=1)
        guard_latencies = list(
            db.scalars(
                select(RequestLog.content_guard_latency_ms)
                .where(
                    RequestLog.created_at >= since,
                    RequestLog.content_guard_latency_ms.is_not(None),
                )
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(5000)
            )
        )
        recent_events = list(
            db.scalars(
                select(RequestLog)
                .where(RequestLog.content_guard_result.is_not(None))
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(200)
            )
        )
        events_by_provider: dict[int, list[dict[str, Any]]] = {}
        for event in recent_events:
            provider_id = int(event.provider_id or 0)
            if provider_id <= 0:
                continue
            bucket = events_by_provider.setdefault(provider_id, [])
            if len(bucket) < 5:
                bucket.append(ContentGuardModuleService.serialize_runtime_event(event))
        return {
            "settings": ContentGuardModuleService.serialize_settings(SettingService.get_or_create(db)),
            "summary": {
                "provider_count": len(providers),
                "blocked_provider_count": blocked_count,
                "low_trust_provider_count": low_trust_count,
                "review_provider_count": review_count,
                "latency_p50_ms": ContentGuardModuleService.percentile(guard_latencies, 50),
                "latency_p95_ms": ContentGuardModuleService.percentile(guard_latencies, 95),
                "latency_p99_ms": ContentGuardModuleService.percentile(guard_latencies, 99),
            },
            "providers": [
                ContentGuardModuleService.serialize_provider(
                    item,
                    recent_events=events_by_provider.get(item.id, []),
                )
                for item in providers
            ],
            "rules": ContentGuardModuleService.serialize_rules(SettingService.get_or_create(db)),
            "rule_defaults": ContentGuardRuleService.default_rules(),
            "probe_options": [
                {"key": key, "label": label}
                for key, label in ContentGuardModuleService.PROBE_LABELS.items()
            ],
        }

    @staticmethod
    def percentile(values: list[int | float], percentile: int) -> float | None:
        cleaned = sorted(float(item) for item in values if item is not None)
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return round(cleaned[0], 2)
        rank = (len(cleaned) - 1) * (percentile / 100)
        lower = int(rank)
        upper = min(lower + 1, len(cleaned) - 1)
        weight = rank - lower
        return round(cleaned[lower] * (1 - weight) + cleaned[upper] * weight, 2)

    @staticmethod
    def serialize_settings(setting: Any) -> dict[str, Any]:
        return {
            "content_guard_enabled": bool(getattr(setting, "content_guard_enabled", True)),
            "content_guard_precheck_auto_enabled": bool(getattr(setting, "content_guard_precheck_auto_enabled", False)),
            "content_guard_block_on_high_risk": bool(getattr(setting, "content_guard_block_on_high_risk", True)),
            "content_guard_probe_interval_sec": int(getattr(setting, "content_guard_probe_interval_sec", 3600) or 3600),
            "content_guard_max_scan_bytes": int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384),
            "content_guard_stream_buffer_max_bytes": int(getattr(setting, "content_guard_stream_buffer_max_bytes", 16384) or 16384),
            "content_guard_low_trust_requires_buffer": bool(getattr(setting, "content_guard_low_trust_requires_buffer", True)),
            "content_guard_high_risk_strategy": str(getattr(setting, "content_guard_high_risk_strategy", "switch_provider") or "switch_provider"),
            "content_guard_max_detection_delay_ms": min(500, max(0, int(getattr(setting, "content_guard_max_detection_delay_ms", 300) or 300))),
            "content_guard_stream_mode": str(getattr(setting, "content_guard_stream_mode", "buffer_300ms") or "buffer_300ms"),
            "content_guard_url_check_enabled": bool(getattr(setting, "content_guard_url_check_enabled", True)),
            "content_guard_url_allowlist_json": str(getattr(setting, "content_guard_url_allowlist_json", "") or ""),
            "content_guard_async_review_enabled": bool(getattr(setting, "content_guard_async_review_enabled", True)),
            "content_guard_high_risk_confidence_threshold": int(getattr(setting, "content_guard_high_risk_confidence_threshold", 85) or 85),
        }

    @staticmethod
    def serialize_rules(setting: Any) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in ContentGuardRuleService.parse_rules_json(getattr(setting, "content_guard_rules_json", ""))]

    @staticmethod
    def update_settings(db: Session, payload: ContentGuardSettingsUpdate) -> Any:
        setting = SettingService.get_or_create(db)
        for field, value in payload.model_dump().items():
            setattr(setting, field, value)
        db.commit()
        db.refresh(setting)
        SettingService.invalidate_runtime_cache()
        return setting

    @staticmethod
    def update_rules(db: Session, payload: ContentGuardRulesUpdate) -> Any:
        setting = SettingService.get_or_create(db)
        setting.content_guard_rules_json = ContentGuardRuleService.serialize_rules_json(
            [item.model_dump() for item in payload.rules]
        )
        db.commit()
        db.refresh(setting)
        SettingService.invalidate_runtime_cache()
        return setting

    @staticmethod
    def reset_rules(db: Session) -> Any:
        setting = SettingService.get_or_create(db)
        setting.content_guard_rules_json = ""
        db.commit()
        db.refresh(setting)
        SettingService.invalidate_runtime_cache()
        return setting

    @staticmethod
    def inspect_text(db: Session, payload: ContentGuardTextInspectRequest) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        result = ContentGuardRuleService.inspect_response_text(
            payload.text,
            endpoint_path=payload.endpoint_path,
            request_payload=payload.request_payload,
            max_scan_bytes=payload.max_scan_bytes,
            rules_json=getattr(setting, "content_guard_rules_json", ""),
            url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
            url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
        )
        matched_rules = ContentGuardRuleService.match_text_rules(
            ContentGuardRuleService.clip_text(
                ContentGuardRuleService.normalize_scan_text(payload.text),
                max_scan_bytes=payload.max_scan_bytes,
            ),
            request_payload=payload.request_payload,
            url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
            url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
            rules=ContentGuardRuleService.parse_rules_json(getattr(setting, "content_guard_rules_json", "")),
        )
        return {
            "result": result.to_log_kwargs(),
            "risk_level": result.risk_level,
            "action": result.action,
            "confidence": result.confidence,
            "score_delta": result.score_delta,
            "matched_rules": [rule.to_dict() for rule in matched_rules],
        }

    @staticmethod
    def serialize_provider(provider: Provider, *, recent_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        models = [
            ContentGuardModuleService.serialize_provider_model(item)
            for item in sorted(provider.provider_models, key=lambda model: (model.priority, model.id))
            if item.enabled
        ]
        trust_summary = ProviderService.provider_trust_summary(provider)
        return {
            "id": provider.id,
            "name": provider.name,
            "enabled": provider.enabled,
            "base_url": provider.base_url,
            "trust_level": provider.trust_level,
            "trust_level_label": PROVIDER_TRUST_LEVEL_LABELS.get(provider.trust_level, provider.trust_level),
            "content_integrity_status": provider.content_integrity_status,
            "content_integrity_status_label": CONTENT_INTEGRITY_STATUS_LABELS.get(
                provider.content_integrity_status,
                provider.content_integrity_status,
            ),
            "content_integrity_score": provider.content_integrity_score,
            "content_violation_count": provider.content_violation_count,
            "last_content_violation_at": provider.last_content_violation_at,
            "content_guard_enabled": provider.content_guard_enabled,
            "buffer_stream_for_guard": provider.buffer_stream_for_guard,
            "computed_trust_status": trust_summary.get("status"),
            "computed_trust_status_label": trust_summary.get("label"),
            "computed_trust_status_reason": trust_summary.get("reason"),
            "trust_status": trust_summary.get("status"),
            "trust_status_label": trust_summary.get("label"),
            "trust_status_reason": trust_summary.get("reason"),
            "recent_events": list(recent_events or []),
            "models": models,
        }

    @staticmethod
    def serialize_runtime_event(event: RequestLog) -> dict[str, Any]:
        return {
            "id": event.id,
            "created_at": event.created_at,
            "trace_id": event.trace_id,
            "model_name": event.model_name,
            "request_path": event.request_path,
            "content_guard_result": event.content_guard_result,
            "content_guard_risk_level": event.content_guard_risk_level,
            "content_guard_reason": event.content_guard_reason,
            "content_guard_action": event.content_guard_action,
            "content_guard_final_strategy": event.content_guard_final_strategy,
        }

    @staticmethod
    def set_provider_content_integrity_status(db: Session, provider_id: int, *, status: str) -> Provider:
        if status not in {"passed", "blocked"}:
            raise ValueError("内容防护处置状态仅支持 passed 或 blocked")
        provider = db.get(Provider, provider_id)
        if provider is None:
            raise ValueError("提供商不存在")
        enabled_models = [item for item in provider.provider_models if item.enabled]
        if not enabled_models:
            raise ValueError("当前提供商没有可处置的已启用模型")
        for provider_model in enabled_models:
            provider_model.content_integrity_status = status
            ProviderService._ensure_manual_content_probe_reason(provider_model)
        ProviderService.refresh_provider_state(provider)
        db.commit()
        db.refresh(provider)
        ProviderService.invalidate_provider_runtime_cache()
        return provider

    @staticmethod
    def serialize_provider_model(provider_model: ProviderModel) -> dict[str, Any]:
        trust_status = ProviderService.provider_model_trust_status(provider_model)
        trust_reason = ProviderService._content_probe_reason(provider_model)
        return {
            "id": provider_model.id,
            "model_name": provider_model.model_name,
            "protocol_type": provider_model.protocol_type,
            "supports_stream": provider_model.supports_stream,
            "supports_tools": provider_model.supports_tools,
            "supports_chat_completions": provider_model.supports_chat_completions,
            "supports_responses": provider_model.supports_responses,
            "content_integrity_status": provider_model.content_integrity_status,
            "content_integrity_status_label": CONTENT_INTEGRITY_STATUS_LABELS.get(
                provider_model.content_integrity_status,
                provider_model.content_integrity_status,
            ),
            "content_probe_last_passed_at": provider_model.content_probe_last_passed_at,
            "content_probe_last_failed_at": provider_model.content_probe_last_failed_at,
            "content_probe_failure_count": provider_model.content_probe_failure_count,
            "content_probe_results": loads_json(provider_model.content_probe_results_json, None),
            "trust_status": trust_status,
            "trust_status_label": MODEL_TRUST_STATUS_LABELS.get(trust_status, trust_status),
            "trust_status_reason": trust_reason,
        }

    @staticmethod
    async def run_probe(db: Session, payload: ContentGuardRunRequest) -> dict[str, Any]:
        return await ContentTrustProbeService.run_capability_probe(db, payload)

    @staticmethod
    async def run_trust_probe(db: Session, payload: ContentGuardRunRequest) -> dict[str, Any]:
        return await ContentTrustProbeService.run_trust_probe(db, payload)
