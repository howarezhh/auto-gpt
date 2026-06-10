from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.logging_events import RequestContentGuardEvent
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
from app.services.cache_service import CacheService
from app.services.setting_service import SettingService
from app.utils.json_utils import loads_json


class ContentGuardModuleService:
    """内容完整性防护模块的统一配置、概览与检测入口。"""

    PROBE_LABELS = ContentTrustProbeService.PROBE_LABELS
    OVERVIEW_PROVIDER_LIMIT = 50
    OVERVIEW_PROVIDER_MAX_PAGE_SIZE = 100
    OVERVIEW_MODEL_LIMIT = 30
    OVERVIEW_LATENCY_SAMPLE_LIMIT = 1000
    OVERVIEW_RECENT_EVENT_LIMIT = 100
    ROUTE_TRAFFIC_LOG_TYPES = ("chat", "responses", "moderations", "files")

    @staticmethod
    def invalidate_runtime_caches() -> None:
        SettingService.invalidate_runtime_cache()
        ProviderService.invalidate_provider_runtime_cache()
        CacheService.invalidate_prefix("providers-runtime")
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")

    @staticmethod
    def _provider_summary_counts(db: Session) -> dict[str, int]:
        row = db.execute(
            select(
                func.count(Provider.id).label("provider_count"),
                func.sum(
                    case((or_(Provider.trust_level == "blocked", Provider.content_integrity_status == "blocked"), 1), else_=0)
                ).label("blocked_count"),
                func.sum(case((Provider.trust_level == "low", 1), else_=0)).label("low_trust_count"),
                func.sum(case((Provider.content_integrity_status.in_(["unknown", "degraded"]), 1), else_=0)).label("review_count"),
            ).where(Provider.enabled.is_(True))
        ).one()
        return {
            "provider_count": int(row.provider_count or 0),
            "blocked_count": int(row.blocked_count or 0),
            "low_trust_count": int(row.low_trust_count or 0),
            "review_count": int(row.review_count or 0),
        }

    @staticmethod
    def build_overview(
        db: Session,
        *,
        provider_keyword: str = "",
        provider_page: int = 1,
        provider_page_size: int | None = None,
    ) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        enabled_provider_filter = Provider.enabled.is_(True)
        summary_counts = ContentGuardModuleService._provider_summary_counts(db)
        safe_page_size = min(
            ContentGuardModuleService.OVERVIEW_PROVIDER_MAX_PAGE_SIZE,
            max(1, int(provider_page_size or ContentGuardModuleService.OVERVIEW_PROVIDER_LIMIT)),
        )
        safe_page = max(1, int(provider_page or 1))
        normalized_keyword = str(provider_keyword or "").strip()
        provider_filters = [enabled_provider_filter]
        if normalized_keyword:
            keyword_like = f"%{normalized_keyword}%"
            provider_filters.append(
                or_(
                    Provider.name.ilike(keyword_like),
                    Provider.base_url.ilike(keyword_like),
                    Provider.provider_type.ilike(keyword_like),
                    Provider.provider_models.any(ProviderModel.model_name.ilike(keyword_like)),
                )
            )
        filtered_provider_count = int(db.scalar(select(func.count()).select_from(Provider).where(*provider_filters)) or 0)
        providers = list(
            db.scalars(
                select(Provider)
                .where(*provider_filters)
                .order_by(Provider.priority.asc(), Provider.id.asc())
                .offset((safe_page - 1) * safe_page_size)
                .limit(safe_page_size)
            )
        )
        since = datetime.utcnow() - timedelta(hours=1)
        guard_latencies = [
            int(item or 0)
            for item in db.scalars(
                select(RequestLog.content_guard_latency_ms)
                .select_from(RequestContentGuardEvent)
                .join(RequestLog, RequestContentGuardEvent.request_log_id == RequestLog.id)
                .where(
                    RequestContentGuardEvent.created_at >= since,
                    RequestLog.log_type.in_(ContentGuardModuleService.ROUTE_TRAFFIC_LOG_TYPES),
                    RequestLog.content_guard_latency_ms.is_not(None),
                )
                .order_by(RequestContentGuardEvent.created_at.desc(), RequestContentGuardEvent.id.desc())
                .limit(ContentGuardModuleService.OVERVIEW_LATENCY_SAMPLE_LIMIT)
            )
        ]
        recent_events = list(
            db.scalars(
                select(RequestLog)
                .where(RequestLog.content_guard_result.is_not(None))
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(ContentGuardModuleService.OVERVIEW_RECENT_EVENT_LIMIT)
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
            "settings": ContentGuardModuleService.serialize_settings(setting),
            "summary": {
                "provider_count": summary_counts["provider_count"],
                "provider_display_limit": safe_page_size,
                "provider_filtered_count": filtered_provider_count,
                "provider_page": safe_page,
                "provider_page_size": safe_page_size,
                "provider_total_pages": max(1, (filtered_provider_count + safe_page_size - 1) // safe_page_size),
                "blocked_provider_count": summary_counts["blocked_count"],
                "low_trust_provider_count": summary_counts["low_trust_count"],
                "review_provider_count": summary_counts["review_count"],
                "latency_p50_ms": ContentGuardModuleService.percentile(guard_latencies, 50),
                "latency_p95_ms": ContentGuardModuleService.percentile(guard_latencies, 95),
                "latency_p99_ms": ContentGuardModuleService.percentile(guard_latencies, 99),
            },
            "providers": [
                ContentGuardModuleService.serialize_provider(
                    item,
                    recent_events=events_by_provider.get(item.id, []),
                    model_limit=ContentGuardModuleService.OVERVIEW_MODEL_LIMIT,
                )
                for item in providers
            ],
            "rule_defaults": ContentGuardRuleService.default_rules(),
            "rules_configuration": ContentGuardModuleService.rules_configuration_state(setting),
            "probe_options": ContentGuardModuleService.probe_options(),
        }

    @staticmethod
    def build_precheck_status(db: Session) -> dict[str, Any]:
        providers = list(
            db.scalars(
                select(Provider)
                .where(Provider.enabled.is_(True))
                .order_by(Provider.priority.asc(), Provider.id.asc())
                .limit(ContentGuardModuleService.OVERVIEW_PROVIDER_LIMIT)
            )
        )
        return {
            "settings": ContentGuardModuleService.serialize_settings(SettingService.get_or_create(db)),
            "summary": ContentGuardModuleService.build_summary(db),
            "providers": [
                ContentGuardModuleService.serialize_provider(
                    item,
                    model_limit=ContentGuardModuleService.OVERVIEW_MODEL_LIMIT,
                )
                for item in providers
            ],
            "probe_options": ContentGuardModuleService.probe_options(),
        }

    @staticmethod
    def build_runtime_settings(db: Session) -> dict[str, Any]:
        return {
            "settings": ContentGuardModuleService.serialize_settings(SettingService.get_or_create(db)),
            "probe_options": ContentGuardModuleService.probe_options(),
        }

    @staticmethod
    def build_summary(db: Session) -> dict[str, Any]:
        summary_counts = ContentGuardModuleService._provider_summary_counts(db)
        return {
            "provider_count": summary_counts["provider_count"],
            "provider_display_limit": ContentGuardModuleService.OVERVIEW_PROVIDER_LIMIT,
            "blocked_provider_count": summary_counts["blocked_count"],
            "low_trust_provider_count": summary_counts["low_trust_count"],
            "review_provider_count": summary_counts["review_count"],
        }

    @staticmethod
    def probe_options() -> list[dict[str, str]]:
        json_enabled = ContentTrustProbeService.json_probe_enabled()
        return [
            {"key": key, "label": label}
            for key, label in ContentGuardModuleService.PROBE_LABELS.items()
            if key != "json" or json_enabled
        ]

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
            "content_guard_json_probe_enabled": bool(getattr(setting, "content_guard_json_probe_enabled", False)),
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
            "content_guard_enhanced_detection_enabled": bool(getattr(setting, "content_guard_enhanced_detection_enabled", True)),
            "content_guard_enhanced_illegal_enabled": bool(getattr(setting, "content_guard_enhanced_illegal_enabled", True)),
            "content_guard_enhanced_ad_enabled": bool(getattr(setting, "content_guard_enhanced_ad_enabled", True)),
            "content_guard_enhanced_custom_enabled": bool(getattr(setting, "content_guard_enhanced_custom_enabled", True)),
            "content_guard_enhanced_obfuscation_enabled": bool(getattr(setting, "content_guard_enhanced_obfuscation_enabled", True)),
            "content_guard_enhanced_threshold": int(getattr(setting, "content_guard_enhanced_threshold", 70) or 70),
            "content_guard_enhanced_context_window_chars": int(getattr(setting, "content_guard_enhanced_context_window_chars", 96) or 96),
        }

    @staticmethod
    def serialize_rules(setting: Any) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in ContentGuardRuleService.parse_rules_json(getattr(setting, "content_guard_rules_json", ""))]

    @staticmethod
    def rules_configuration_state(setting: Any, parsed_rules: list[Any] | None = None) -> dict[str, Any]:
        if parsed_rules is None:
            raw_rules_json = str(getattr(setting, "content_guard_rules_json", "") or "")
            parsed_rules = ContentGuardRuleService.parse_rules_json(raw_rules_json)
        errors = [
            rule.to_dict()
            for rule in parsed_rules
            if rule.category == "rule_configuration_error"
        ]
        return {
            "valid": not errors,
            "error_count": len(errors),
            "errors": errors[:10],
        }

    @staticmethod
    def list_rules(
        db: Session,
        *,
        keyword: str = "",
        category: str = "",
        enabled: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        parsed_rules = ContentGuardRuleService.parse_rules_json(getattr(setting, "content_guard_rules_json", ""))
        base_rules = [
            {**rule, "source_index": index}
            for index, rule in enumerate(rule.to_dict() for rule in parsed_rules)
        ]
        rules = list(base_rules)
        normalized_keyword = keyword.strip().lower()
        normalized_category = category.strip()
        if normalized_keyword:
            rules = [
                rule for rule in rules
                if normalized_keyword in " ".join(
                    [
                        str(rule.get("id") or ""),
                        str(rule.get("name") or ""),
                        str(rule.get("reason") or ""),
                        " ".join(str(item) for item in (rule.get("patterns") or [])),
                    ]
                ).lower()
            ]
        if normalized_category:
            rules = [rule for rule in rules if str(rule.get("category") or "") == normalized_category]
        if enabled is not None:
            rules = [rule for rule in rules if bool(rule.get("enabled", True)) is enabled]
        total = len(rules)
        safe_page_size = min(50, max(1, int(page_size or 20)))
        total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
        safe_page = min(max(1, int(page or 1)), total_pages)
        start = (safe_page - 1) * safe_page_size
        rules_configuration = ContentGuardModuleService.rules_configuration_state(setting, parsed_rules=parsed_rules)
        categories = sorted({str(rule.get("category") or "") for rule in base_rules if rule.get("category")})
        return {
            "items": rules[start:start + safe_page_size],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "total_pages": total_pages,
            "categories": categories,
            "rules_configuration": rules_configuration,
        }

    @staticmethod
    def update_settings(db: Session, payload: ContentGuardSettingsUpdate) -> Any:
        setting = SettingService.get_or_create(db)
        for field, value in payload.model_dump().items():
            setattr(setting, field, value)
        db.commit()
        db.refresh(setting)
        ContentGuardModuleService.invalidate_runtime_caches()
        return setting

    @staticmethod
    def update_rules(db: Session, payload: ContentGuardRulesUpdate) -> Any:
        setting = SettingService.get_or_create(db)
        setting.content_guard_rules_json = ContentGuardRuleService.serialize_rules_json(
            [item.model_dump() for item in payload.rules]
        )
        db.commit()
        db.refresh(setting)
        ContentGuardModuleService.invalidate_runtime_caches()
        return setting

    @staticmethod
    def reset_rules(db: Session) -> Any:
        setting = SettingService.get_or_create(db)
        setting.content_guard_rules_json = ""
        db.commit()
        db.refresh(setting)
        ContentGuardModuleService.invalidate_runtime_caches()
        return setting

    @staticmethod
    def inspect_text(db: Session, payload: ContentGuardTextInspectRequest) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        max_scan_bytes = max(1024, min(262144, int(payload.max_scan_bytes or 16384)))
        result = ContentGuardRuleService.inspect_response_text(
            payload.text,
            endpoint_path=payload.endpoint_path,
            request_payload=payload.request_payload,
            max_scan_bytes=max_scan_bytes,
            rules_json=getattr(setting, "content_guard_rules_json", ""),
            url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
            url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
            enhanced_detection_enabled=bool(getattr(setting, "content_guard_enhanced_detection_enabled", True)),
            enhanced_illegal_enabled=bool(getattr(setting, "content_guard_enhanced_illegal_enabled", True)),
            enhanced_ad_enabled=bool(getattr(setting, "content_guard_enhanced_ad_enabled", True)),
            enhanced_custom_enabled=bool(getattr(setting, "content_guard_enhanced_custom_enabled", True)),
            enhanced_obfuscation_enabled=bool(getattr(setting, "content_guard_enhanced_obfuscation_enabled", True)),
            enhanced_threshold=int(getattr(setting, "content_guard_enhanced_threshold", 70) or 70),
            enhanced_context_window_chars=int(getattr(setting, "content_guard_enhanced_context_window_chars", 96) or 96),
        )
        return {
            "result": result.to_log_kwargs(),
            "risk_level": result.risk_level,
            "action": result.action,
            "confidence": result.confidence,
            "score_delta": result.score_delta,
            "matched_rules": result.matched_rules,
        }

    @staticmethod
    def serialize_provider(
        provider: Provider,
        *,
        recent_events: list[dict[str, Any]] | None = None,
        model_limit: int | None = None,
    ) -> dict[str, Any]:
        enabled_models = [
            ContentGuardModuleService.serialize_provider_model(item)
            for item in sorted(provider.provider_models, key=lambda model: (model.priority, model.id))
            if item.enabled
        ]
        safe_model_limit = max(1, int(model_limit or len(enabled_models) or 1))
        models = enabled_models[:safe_model_limit]
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
            "model_count": len(enabled_models),
            "model_display_limit": safe_model_limit,
            "model_has_more": len(enabled_models) > len(models),
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
        return await ContentTrustProbeService.run_capability_probe(
            db,
            payload,
            detection_source="manual_precheck_capability_probe",
        )

    @staticmethod
    async def run_trust_probe(db: Session, payload: ContentGuardRunRequest) -> dict[str, Any]:
        return await ContentTrustProbeService.run_trust_probe(
            db,
            payload,
            detection_source="manual_trust_probe",
        )
