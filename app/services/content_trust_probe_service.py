from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.content_guard import ContentGuardRunRequest
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_guard_rule_service import ContentGuardRuleService


class ContentTrustProbeService:
    """预先防护：可信探针、自动检测与请求前路由可信决策。"""

    PROBE_LABELS = {
        "fixed_answer": "固定答案",
        "pollution_rules": "外链广告识别",
        "json": "严格 JSON",
        "sse": "流式污染检测",
        "tools": "工具调用",
    }
    REQUIRED_TRUST_PROBE_KEYS = ["fixed_answer", "pollution_rules", "sse"]
    MIN_CONTENT_INTEGRITY_SCORE = 20

    @staticmethod
    async def run_trust_probe(db: Session, payload: ContentGuardRunRequest) -> dict[str, Any]:
        probe_keys = ContentTrustProbeService.merge_required_probe_keys(payload.probe_keys)
        forced_payload = payload.model_copy(
            update={
                "probe_keys": probe_keys,
                "persist_internal_result": payload.target_type == "internal",
            }
        )
        return await ContentTrustProbeService.run_capability_probe(db, forced_payload)

    @staticmethod
    def merge_required_probe_keys(probe_keys: list[str]) -> list[str]:
        merged: list[str] = []
        for probe_key in list(probe_keys or []) + list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS):
            key = str(probe_key or "").strip()
            if key and key not in merged:
                merged.append(key)
        return merged

    @staticmethod
    async def run_capability_probe(db: Session, payload: ContentGuardRunRequest) -> dict[str, Any]:
        provider, provider_model, target = ContentTrustProbeService._resolve_probe_target(db, payload)
        endpoint_path = ContentTrustProbeService._resolve_endpoint_path(provider, provider_model, payload)
        probe_results: list[dict[str, Any]] = []
        for probe_key in payload.probe_keys:
            if probe_key == "sse" and not bool(getattr(provider_model, "supports_stream", True)):
                probe_results.append(
                    ContentTrustProbeService.skipped_probe(
                        probe_key=probe_key,
                        endpoint_path=endpoint_path,
                        message="模型未启用流式能力",
                    )
                )
                continue
            if probe_key == "tools" and not bool(getattr(provider_model, "supports_tools", True)):
                probe_results.append(
                    ContentTrustProbeService.skipped_probe(
                        probe_key=probe_key,
                        endpoint_path=endpoint_path,
                        message="模型未启用工具调用能力",
                    )
                )
                continue
            probe_results.append(await ContentTrustProbeService.run_single_probe(provider, provider_model, endpoint_path, probe_key))
        summary = ContentTrustProbeService.summarize_probe_results(probe_results)
        if payload.target_type == "internal" and payload.persist_internal_result:
            aggregate_guard = ContentTrustProbeService.aggregate_content_guard_result(probe_results)
            if aggregate_guard is not None:
                ContentTrustProbeService.update_provider_model_trust_status(
                    db,
                    provider,
                    provider_model,
                    content_guard_result=aggregate_guard,
                    endpoint_results=probe_results,
                )
        return {
            "target": target,
            "endpoint_path": endpoint_path,
            "summary": summary,
            "probe_results": probe_results,
            "checked_at": datetime.utcnow(),
        }

    @staticmethod
    def update_provider_model_trust_status(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        content_guard_result: dict[str, Any],
        endpoint_results: list[dict[str, Any]] | None = None,
    ) -> None:
        ContentGuardProbeService.apply_content_probe_health(
            db,
            provider,
            provider_model,
            content_guard_result=content_guard_result,
            endpoint_results=endpoint_results,
        )

    @staticmethod
    def get_trust_decision_for_route(
        provider: Provider,
        provider_model: ProviderModel | None = None,
        *,
        route_context: Any = None,
    ) -> dict[str, Any]:
        trust_level = str(getattr(provider, "trust_level", "standard") or "standard")
        integrity_status = str(getattr(provider, "content_integrity_status", "unknown") or "unknown")
        integrity_score = int(getattr(provider, "content_integrity_score", 80) or 0)
        require_trusted = bool(getattr(route_context, "require_trusted_provider", False)) if route_context is not None else False
        reason: str | None = None
        if trust_level == "blocked":
            reason = "provider_trust_blocked"
        elif integrity_status == "blocked":
            reason = "provider_content_integrity_blocked"
        elif integrity_score <= ContentTrustProbeService.MIN_CONTENT_INTEGRITY_SCORE:
            reason = "provider_content_integrity_score_too_low"
        elif require_trusted and trust_level not in {"official", "trusted"}:
            reason = "provider_trusted_required"
        if reason is None and provider_model is not None:
            model_status = str(getattr(provider_model, "content_integrity_status", "unknown") or "unknown")
            if model_status == "blocked":
                reason = "model_content_integrity_blocked"
        return {
            "allowed": reason is None,
            "reason": reason,
            "trust_level": trust_level,
            "content_integrity_status": integrity_status,
            "content_integrity_score": integrity_score,
        }

    @staticmethod
    def _resolve_probe_target(db: Session, payload: ContentGuardRunRequest) -> tuple[Provider, ProviderModel, dict[str, Any]]:
        if payload.target_type == "external":
            if payload.external is None:
                raise ValueError("外部渠道检测必须提供接口地址、密钥和模型名")
            external = payload.external
            provider = Provider(
                id=0,
                name="外部渠道",
                base_url=external.base_url,
                api_key=external.api_key,
                provider_type="openai_compatible",
                protocol_type="responses" if external.endpoint_path == "/responses" else "chat_completions",
                enabled=True,
                trust_level="standard",
                content_integrity_status="unknown",
                content_integrity_score=80,
                content_guard_enabled=True,
            )
            provider_model = ProviderModel(
                id=0,
                provider_id=0,
                model_name=external.model_name,
                enabled=True,
                supports_stream=True,
                supports_tools=True,
                supports_chat_completions=external.endpoint_path == "/chat/completions",
                supports_responses=external.endpoint_path == "/responses",
                protocol_type="responses" if external.endpoint_path == "/responses" else "chat_completions",
            )
            provider_model.provider = provider
            return provider, provider_model, {
                "type": "external",
                "name": "外部渠道",
                "base_url": external.base_url,
                "model_name": external.model_name,
            }
        if payload.provider_id is None:
            raise ValueError("本项目提供商检测必须选择提供商")
        provider = db.get(Provider, payload.provider_id)
        if provider is None:
            raise ValueError("提供商不存在")
        provider_model = None
        if payload.provider_model_id is not None:
            provider_model = db.get(ProviderModel, payload.provider_model_id)
            if provider_model is None or provider_model.provider_id != provider.id:
                raise ValueError("模型不属于当前提供商")
        if provider_model is None:
            provider_model = next((item for item in provider.provider_models if item.enabled), None)
        if provider_model is None:
            raise ValueError("当前提供商没有可检测的已启用模型")
        return provider, provider_model, {
            "type": "internal",
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
        }

    @staticmethod
    def _resolve_endpoint_path(provider: Provider, provider_model: ProviderModel, payload: ContentGuardRunRequest) -> str:
        if payload.target_type == "external" and payload.external is not None:
            return payload.external.endpoint_path
        endpoint_path = ContentGuardProbeService.content_probe_endpoint_path(provider, provider_model)
        if endpoint_path is None:
            raise ValueError("当前模型未启用 Chat Completions 或 Responses 端点")
        return endpoint_path

    @staticmethod
    async def run_single_probe(provider: Provider, provider_model: ProviderModel, endpoint_path: str, probe_key: str) -> dict[str, Any]:
        if probe_key == "fixed_answer":
            result = await ContentGuardProbeService.probe_fixed_answer(provider, provider_model, endpoint_path=endpoint_path)
        elif probe_key == "pollution_rules":
            result = await ContentGuardProbeService.probe_pollution_rules(provider, provider_model, endpoint_path=endpoint_path)
        elif probe_key == "json":
            result = await ContentGuardProbeService.probe_json(provider, provider_model, endpoint_path=endpoint_path)
        elif probe_key == "sse":
            result = await ContentGuardProbeService.probe_sse(provider, provider_model, endpoint_path=endpoint_path)
        elif probe_key == "tools":
            result = await ContentGuardProbeService.probe_tools(provider, provider_model, endpoint_path=endpoint_path)
        else:
            result = ContentTrustProbeService.invalid_probe(
                probe_key=probe_key,
                endpoint_path=endpoint_path,
                message="未知探针",
            )
        result["capability_key"] = f"content_{probe_key}"
        result["probe_key"] = probe_key
        result["probe_label"] = ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key)
        return result

    @staticmethod
    def skipped_probe(*, probe_key: str, endpoint_path: str, message: str) -> dict[str, Any]:
        return ContentGuardProbeService.mark_detection_result({
            "capability_key": f"content_{probe_key}",
            "probe_key": probe_key,
            "probe_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "endpoint_path": endpoint_path,
            "endpoint_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "skipped",
            "support_label": "已跳过",
            "latency_ms": 0,
            "status_code": None,
            "message": message,
            "trace": [],
            "retryable": False,
        })

    @staticmethod
    def invalid_probe(*, probe_key: str, endpoint_path: str, message: str) -> dict[str, Any]:
        return ContentGuardProbeService.mark_detection_result({
            "capability_key": f"content_{probe_key}",
            "probe_key": probe_key,
            "probe_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "endpoint_path": endpoint_path,
            "endpoint_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "invalid_probe",
            "support_label": "未知探针",
            "latency_ms": 0,
            "status_code": None,
            "message": message,
            "trace": [],
            "retryable": False,
            "content_guard": {
                "content_guard_result": ContentGuardRuleService.RESULT_REVIEW,
                "content_guard_risk_level": "medium",
                "content_guard_reason": message,
                "content_guard_action": "record",
            },
        })

    @staticmethod
    def summarize_probe_results(results: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(results)
        passed = sum(1 for item in results if item.get("success") is True)
        content_results = [
            str((item.get("content_guard") or {}).get("content_guard_result") or "")
            for item in results
            if isinstance(item.get("content_guard"), dict)
        ]
        aggregate_guard = ContentTrustProbeService.aggregate_content_guard_result(results) or {
            "content_guard_result": ContentGuardRuleService.RESULT_PASS,
            "content_guard_reason": "未返回内容防护结果",
        }
        decision = ContentGuardProbeService._content_probe_decision(
            [ContentGuardProbeService.summarize_probe_result(item) for item in results],
            aggregate_guard,
        )
        result = str(decision.get("content_guard_result") or "")
        if result == ContentGuardRuleService.RESULT_BLOCK or ContentGuardRuleService.RESULT_BLOCK in content_results:
            status = "blocked"
            result = ContentGuardRuleService.RESULT_BLOCK
        elif result == ContentGuardRuleService.RESULT_REVIEW or ContentGuardRuleService.RESULT_REVIEW in content_results or passed < total:
            status = "review"
            result = ContentGuardRuleService.RESULT_REVIEW
        else:
            status = "passed"
            result = ContentGuardRuleService.RESULT_PASS
        return {
            "status": status,
            "content_guard_result": result,
            "content_guard_reason": decision.get("content_guard_reason"),
            "total": total,
            "passed": passed,
            "failed": max(0, total - passed),
        }

    @staticmethod
    def aggregate_content_guard_result(results: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [
            item.get("content_guard")
            for item in results
            if isinstance(item.get("content_guard"), dict)
        ]
        if not candidates:
            return None
        priority = {
            ContentGuardRuleService.RESULT_BLOCK: 3,
            ContentGuardRuleService.RESULT_REVIEW: 2,
            ContentGuardRuleService.RESULT_ERROR: 2,
            ContentGuardRuleService.RESULT_PASS: 1,
        }
        return max(candidates, key=lambda item: priority.get(str(item.get("content_guard_result") or ""), 0))
