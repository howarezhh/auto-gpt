from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.content_guard import ContentGuardRunRequest
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.probe_rate_limit_service import ProbeRateLimitService
from app.services.setting_service import SettingService
from app.utils.json_utils import loads_json


class ContentTrustProbeService:
    """预先防护：可信探针、自动检测与请求前路由可信决策。"""

    PROBE_LABELS = {
        "fixed_answer": "固定答案",
        "pollution_rules": "外链广告识别",
        "json": "严格 JSON",
        "sse": "流式污染检测",
    }
    REQUIRED_TRUST_PROBE_KEYS = ["fixed_answer", "pollution_rules", "sse"]
    MIN_CONTENT_INTEGRITY_SCORE = 20
    PROBE_TIMEOUT_SECONDS = {
        "fixed_answer": 12,
        "json": 12,
        "sse": ContentGuardProbeService.STREAM_CONNECT_TIMEOUT_SECONDS
        + ContentGuardProbeService.STREAM_FIRST_TOKEN_TIMEOUT_SECONDS
        + ContentGuardProbeService.SSE_PROBE_MAX_DURATION_SECONDS
        + 2,
        "pollution_rules": ContentGuardProbeService.POLLUTION_PROBE_TIMEOUT_SECONDS + 2,
    }
    COMBINABLE_TEXT_PROBE_KEYS = ("fixed_answer", "pollution_rules")

    @staticmethod
    def _probe_timeout_seconds(provider: Provider, probe_key: str, fallback_seconds: float) -> float:
        timeout_seconds = ContentGuardProbeService.provider_probe_timeout_seconds(provider, fallback_seconds)
        if probe_key == "sse":
            stream_timeout_seconds = (
                ContentGuardProbeService.provider_stream_connect_timeout_seconds(provider)
                + ContentGuardProbeService.provider_stream_first_token_timeout_seconds(provider)
                + ContentGuardProbeService.SSE_PROBE_MAX_DURATION_SECONDS
                + 2
            )
            return max(timeout_seconds, float(stream_timeout_seconds))
        return timeout_seconds

    @staticmethod
    def _required_probe_phase_keys() -> set[str]:
        return {f"content_{key}" for key in ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS}

    @staticmethod
    def _model_content_integrity_score(provider_model: ProviderModel | None) -> int | None:
        if provider_model is None:
            return None
        status = str(getattr(provider_model, "content_integrity_status", "unknown") or "unknown")
        if status == "blocked":
            return 0
        if status == "passed":
            return 100
        probe_results = loads_json(getattr(provider_model, "content_probe_results_json", None), {})
        if isinstance(probe_results, dict):
            last_result = probe_results.get("last_result")
            if isinstance(last_result, dict):
                guard_result = str(last_result.get("content_guard_result") or "")
                if guard_result == ContentGuardRuleService.RESULT_BLOCK:
                    return 0
                if guard_result == ContentGuardRuleService.RESULT_REVIEW:
                    return 60
                if guard_result == ContentGuardRuleService.RESULT_PASS:
                    return 100
            serialized_status = str(probe_results.get("status") or "")
            if serialized_status == "blocked":
                return 0
            if serialized_status == "passed":
                return 100
        if status == "degraded":
            failure_count = int(getattr(provider_model, "content_probe_failure_count", 0) or 0)
            return max(21, 60 - min(failure_count, 2) * 15)
        return None

    @staticmethod
    async def run_trust_probe(
        db: Session,
        payload: ContentGuardRunRequest,
        *,
        detection_source: str = "manual_trust_probe",
    ) -> dict[str, Any]:
        probe_keys = ContentTrustProbeService.merge_required_probe_keys(payload.probe_keys)
        forced_payload = payload.model_copy(
            update={
                "probe_keys": probe_keys,
                "persist_internal_result": payload.target_type == "internal",
            }
        )
        return await ContentTrustProbeService.run_capability_probe(
            db,
            forced_payload,
            detection_source=detection_source,
        )

    @staticmethod
    def merge_required_probe_keys(probe_keys: list[str]) -> list[str]:
        merged: list[str] = []
        for probe_key in list(probe_keys or []) + list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS):
            key = str(probe_key or "").strip()
            if key and key not in merged:
                merged.append(key)
        return merged

    @staticmethod
    def describe_probe_execution_plan(probe_keys: list[str], provider_model: ProviderModel) -> dict[str, Any]:
        ordered_keys: list[str] = []
        for probe_key in probe_keys or []:
            key = str(probe_key or "").strip()
            if key and key not in ordered_keys:
                ordered_keys.append(key)

        skipped: list[dict[str, str]] = []
        runnable: list[str] = []
        json_enabled = ContentTrustProbeService.json_probe_enabled()
        supports_stream = bool(getattr(provider_model, "supports_stream", True))
        for key in ordered_keys:
            if key == "json" and not json_enabled:
                skipped.append({
                    "probe_key": key,
                    "probe_label": ContentTrustProbeService.PROBE_LABELS.get(key, key),
                    "reason": "严格 JSON 探针未启用",
                })
                continue
            if key == "sse" and not supports_stream:
                skipped.append({
                    "probe_key": key,
                    "probe_label": ContentTrustProbeService.PROBE_LABELS.get(key, key),
                    "reason": "模型未启用流式能力",
                })
                continue
            if key not in ContentTrustProbeService.PROBE_LABELS:
                skipped.append({
                    "probe_key": key,
                    "probe_label": key,
                    "reason": "未知探针不会发起上游请求",
                })
                continue
            runnable.append(key)

        request_groups: list[dict[str, Any]] = []
        remaining = list(runnable)
        if all(key in remaining for key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS):
            group_keys = list(ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS)
            request_groups.append({
                "request_key": "combined_text",
                "request_label": "文本内容完整性组合请求",
                "probe_keys": group_keys,
                "probe_labels": [ContentTrustProbeService.PROBE_LABELS.get(key, key) for key in group_keys],
                "stream": False,
                "timeout_seconds": ContentGuardProbeService.COMBINED_TEXT_PROBE_TIMEOUT_SECONDS,
            })
            remaining = [key for key in remaining if key not in group_keys]

        for key in remaining:
            timeout_seconds = ContentTrustProbeService.PROBE_TIMEOUT_SECONDS.get(key, 12)
            request_groups.append({
                "request_key": key,
                "request_label": ContentTrustProbeService.PROBE_LABELS.get(key, key),
                "probe_keys": [key],
                "probe_labels": [ContentTrustProbeService.PROBE_LABELS.get(key, key)],
                "stream": key == "sse",
                "timeout_seconds": timeout_seconds,
            })

        return {
            "logical_probe_count": len(ordered_keys),
            "runnable_probe_count": len(runnable),
            "skipped_probe_count": len(skipped),
            "upstream_request_count": len(request_groups),
            "probe_keys": ordered_keys,
            "required_probe_keys": list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS),
            "request_groups": request_groups,
            "skipped_probes": skipped,
        }

    @staticmethod
    async def run_capability_probe(
        db: Session,
        payload: ContentGuardRunRequest,
        *,
        detection_source: str = "manual_probe",
    ) -> dict[str, Any]:
        provider, provider_model, target = ContentTrustProbeService._resolve_probe_target(db, payload)
        endpoint_path = ContentTrustProbeService._resolve_endpoint_path(provider, provider_model, payload)
        execution_plan = ContentTrustProbeService.describe_probe_execution_plan(payload.probe_keys, provider_model)
        if (
            payload.target_type == "internal"
            and ContentTrustProbeService._automatic_detection_source(detection_source)
            and bool(getattr(provider, "maintenance_mode_enabled", False))
        ):
            probe_results = [
                ContentTrustProbeService.maintenance_probe(
                    probe_key=probe_key,
                    endpoint_path=endpoint_path,
                    provider=provider,
                )
                for probe_key in payload.probe_keys
            ]
            message = ContentTrustProbeService._provider_maintenance_message(provider)
            return {
                "target": target,
                "endpoint_path": endpoint_path,
                "execution_plan": execution_plan,
                "summary": {
                    "status": "skipped",
                    "content_guard_result": "maintenance",
                    "content_guard_reason": message,
                    "error_code": "provider_maintenance_mode",
                    "provider_maintenance_mode": True,
                },
                "probe_results": probe_results,
                "checked_at": now_beijing(),
            }
        ordered_probe_slots: list[tuple[int, dict[str, Any] | None]] = []
        runnable_probes: list[tuple[int, str]] = []
        for index, probe_key in enumerate(payload.probe_keys):
            if probe_key == "json" and not ContentTrustProbeService.json_probe_enabled():
                ordered_probe_slots.append((
                    index,
                    ContentTrustProbeService.skipped_probe(
                        probe_key=probe_key,
                        endpoint_path=endpoint_path,
                        message="严格 JSON 探针未在内容防护配置中启用",
                    ),
                ))
                continue
            if probe_key == "sse" and not bool(getattr(provider_model, "supports_stream", True)):
                ordered_probe_slots.append((
                    index,
                    ContentTrustProbeService.skipped_probe(
                        probe_key=probe_key,
                        endpoint_path=endpoint_path,
                        message="模型未启用流式能力",
                    ),
                ))
                continue
            runnable_probes.append((index, probe_key))
        runnable_index_by_key = {probe_key: index for index, probe_key in runnable_probes}
        combined_text_indexes: dict[str, int] = {}
        if all(key in runnable_index_by_key for key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS):
            combined_text_indexes = {
                key: runnable_index_by_key[key]
                for key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS
            }
            combined_keys = set(combined_text_indexes)
            runnable_probes = [(index, key) for index, key in runnable_probes if key not in combined_keys]
        runnable_tasks = [
            asyncio.create_task(
                ContentTrustProbeService.run_single_probe_with_boundary(
                    provider,
                    provider_model,
                    endpoint_path,
                    probe_key,
                    order_index=index,
                )
            )
            for index, probe_key in runnable_probes
        ]
        if combined_text_indexes:
            runnable_tasks.append(
                asyncio.create_task(
                    ContentTrustProbeService.run_combined_text_probe_with_boundary(
                        provider,
                        provider_model,
                        endpoint_path,
                        order_indexes=combined_text_indexes,
                    )
                )
            )
        runnable_results = await asyncio.gather(*runnable_tasks, return_exceptions=False) if runnable_tasks else []
        for item in runnable_results:
            if isinstance(item, tuple):
                ordered_probe_slots.append(item)
            elif isinstance(item, list):
                ordered_probe_slots.extend(item)
        probe_results = [
            result
            for _, result in sorted(ordered_probe_slots, key=lambda item: item[0])
            if result is not None
        ]
        summary = ContentTrustProbeService.summarize_probe_results(probe_results)
        has_rate_limited_probe = ProbeRateLimitService.contains_rate_limited_result(probe_results)
        has_transient_probe_failure = ContentTrustProbeService._has_transient_probe_failure(probe_results)
        if has_rate_limited_probe:
            limited_result = next(item for item in probe_results if ProbeRateLimitService.is_rate_limited_result(item))
            summary = {
                **summary,
                "status": "rate_limited",
                "content_guard_result": "rate_limited",
                "content_guard_reason": limited_result.get("message") or "探针频率限制，未更新可信状态",
                "error_code": ProbeRateLimitService.ERROR_CODE,
                "probe_rate_limited": True,
                "rate_limit": limited_result.get("rate_limit"),
            }
        elif has_transient_probe_failure:
            transient_result = ContentTrustProbeService._first_transient_probe_failure(probe_results) or {}
            reason = (
                transient_result.get("content_guard_reason")
                or transient_result.get("message")
                or transient_result.get("support_label")
                or "上游当前不可用或传输异常，未更新可信状态"
            )
            summary = {
                **summary,
                "status": "upstream_unavailable",
                "content_guard_result": "upstream_unavailable",
                "content_guard_reason": str(reason),
                "probe_transient_failure": True,
            }
        selected_probe_keys = {str(item) for item in payload.probe_keys or []}
        can_persist_trust_status = set(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS).issubset(selected_probe_keys)
        if (
            payload.target_type == "internal"
            and payload.persist_internal_result
            and can_persist_trust_status
            and not has_rate_limited_probe
            and not has_transient_probe_failure
        ):
            aggregate_guard = ContentTrustProbeService.aggregate_content_guard_result(probe_results)
            if aggregate_guard is not None:
                ContentTrustProbeService.update_provider_model_trust_status(
                    db,
                    provider,
                    provider_model,
                    content_guard_result=aggregate_guard,
                    endpoint_results=probe_results,
                    detection_source=detection_source,
                )
        return {
            "target": target,
            "endpoint_path": endpoint_path,
            "execution_plan": execution_plan,
            "summary": summary,
            "probe_results": probe_results,
            "checked_at": now_beijing(),
        }

    @staticmethod
    def _automatic_detection_source(detection_source: str | None) -> bool:
        normalized = str(detection_source or "").strip().lower()
        return normalized.startswith("automatic") or normalized.startswith("scheduled")

    @staticmethod
    def _provider_maintenance_message(provider: Provider | Any) -> str:
        maintenance_window = str(getattr(provider, "maintenance_window", "") or "").strip()
        if maintenance_window:
            return f"提供商当前处于维护模式（{maintenance_window}），自动可信检测不执行；请在维护结束后重试，或由用户手动检测。"
        return "提供商当前处于维护模式，自动可信检测不执行；请在维护结束后重试，或由用户手动检测。"

    @staticmethod
    def _has_transient_probe_failure(probe_results: list[dict[str, Any]]) -> bool:
        return ContentTrustProbeService._first_transient_probe_failure(probe_results) is not None

    @staticmethod
    def _first_transient_probe_failure(probe_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        transient_status_codes = {408, 429, 500, 502, 503, 504}
        for item in probe_results:
            if ProbeRateLimitService.is_rate_limited_result(item):
                continue
            if item.get("success") is True:
                continue
            if item.get("retryable") is True:
                return item
            try:
                status_code = int(item.get("status_code")) if item.get("status_code") is not None else None
            except (TypeError, ValueError):
                status_code = None
            if status_code in transient_status_codes:
                return item
            text = " ".join(
                str(item.get(key) or "")
                for key in ("message", "content_guard_reason", "support_label", "error_code")
            ).lower()
            if any(
                marker in text
                for marker in (
                    "incorrect header check",
                    "decompress",
                    "service_unavailable",
                    "insufficient_quota",
                    "额度不足",
                    "全部渠道不可提供",
                    "temporarily",
                    "timeout",
                )
            ):
                return item
        return None

    @staticmethod
    def update_provider_model_trust_status(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        content_guard_result: dict[str, Any],
        endpoint_results: list[dict[str, Any]] | None = None,
        detection_source: str = "automatic_probe",
    ) -> None:
        ContentGuardProbeService.apply_content_probe_health(
            db,
            provider,
            provider_model,
            content_guard_result=content_guard_result,
            endpoint_results=endpoint_results,
            detection_source=detection_source,
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
        model_status: str | None = None
        model_integrity_score: int | None = None
        if reason is None and provider_model is not None:
            model_status = str(getattr(provider_model, "content_integrity_status", "unknown") or "unknown")
            model_integrity_score = ContentTrustProbeService._model_content_integrity_score(provider_model)
            if model_status == "blocked":
                reason = "model_content_integrity_blocked"
            elif model_integrity_score is not None and model_integrity_score <= ContentTrustProbeService.MIN_CONTENT_INTEGRITY_SCORE:
                reason = "model_content_integrity_score_too_low"
        elif provider_model is not None:
            model_status = str(getattr(provider_model, "content_integrity_status", "unknown") or "unknown")
            model_integrity_score = ContentTrustProbeService._model_content_integrity_score(provider_model)
        return {
            "allowed": reason is None,
            "reason": reason,
            "trust_level": trust_level,
            "content_integrity_status": integrity_status,
            "content_integrity_score": integrity_score,
            "model_content_integrity_status": model_status,
            "model_content_integrity_score": model_integrity_score,
        }

    @staticmethod
    def _resolve_probe_target(db: Session, payload: ContentGuardRunRequest) -> tuple[Provider, ProviderModel, dict[str, Any]]:
        if payload.target_type == "external":
            if payload.external is None:
                raise ValueError("外部提供商检测必须提供接口地址、密钥和模型名")
            return ContentTrustProbeService.resolve_external_probe_target(payload.external)
        if payload.provider_id is None:
            raise ValueError("本项目提供商检测必须选择提供商")
        provider = db.get(Provider, payload.provider_id)
        if provider is None:
            raise ValueError("提供商不存在")
        if not bool(getattr(provider, "enabled", True)):
            raise ValueError("已停用的提供商不能执行内容防护探针")
        provider_model = None
        if payload.provider_model_id is not None:
            provider_model = db.get(ProviderModel, payload.provider_model_id)
            if provider_model is None or provider_model.provider_id != provider.id:
                raise ValueError("模型不属于当前提供商")
            if not bool(getattr(provider_model, "enabled", True)):
                raise ValueError("已停用的模型不能执行内容防护探针")
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
    def resolve_external_probe_target(external: Any) -> tuple[Provider, ProviderModel, dict[str, Any]]:
        target_identity = ContentTrustProbeService._external_target_identity(
            base_url=external.base_url,
            endpoint_path=external.endpoint_path,
            model_name=external.model_name,
        )
        protocol_type = "responses" if external.endpoint_path == "/responses" else "chat_completions"
        provider = Provider(
            id=target_identity["provider_id"],
            name=target_identity["provider_name"],
            base_url=external.base_url,
            api_key=external.api_key,
            provider_type="external_probe",
            protocol_type=protocol_type,
            enabled=True,
            trust_level="standard",
            content_integrity_status="unknown",
            content_integrity_score=80,
            content_guard_enabled=True,
        )
        provider_model = ProviderModel(
            id=target_identity["provider_model_id"],
            provider_id=target_identity["provider_id"],
            model_name=external.model_name,
            enabled=True,
            supports_stream=False,
            supports_tools=False,
            supports_chat_completions=external.endpoint_path == "/chat/completions",
            supports_responses=external.endpoint_path == "/responses",
            protocol_type=protocol_type,
        )
        provider_model.provider = provider
        target = {
            "type": "external",
            "name": target_identity["provider_name"],
            "target_id": target_identity["target_id"],
            "endpoint_path": external.endpoint_path,
            "model_name": external.model_name,
        }
        return provider, provider_model, target

    @staticmethod
    def _external_target_identity(*, base_url: str, endpoint_path: str, model_name: str) -> dict[str, Any]:
        parsed = urlparse(str(base_url or ""))
        host = parsed.hostname or "external"
        fingerprint = hashlib.sha256(f"{host}|{endpoint_path}|{model_name}".encode("utf-8")).hexdigest()
        numeric = int(fingerprint[:8], 16)
        provider_id = -(numeric % 900_000_000 + 1)
        provider_model_id = provider_id - 900_000_000
        return {
            "target_id": fingerprint[:16],
            "host": host,
            "provider_id": provider_id,
            "provider_model_id": provider_model_id,
            "provider_name": f"外部提供商 {fingerprint[:8]}",
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
    def json_probe_enabled() -> bool:
        try:
            return bool(getattr(SettingService.get_cached(), "content_guard_json_probe_enabled", False))
        except Exception:
            return False

    @staticmethod
    async def run_combined_text_probe_with_boundary(
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        *,
        order_indexes: dict[str, int],
        ) -> list[tuple[int, dict[str, Any]]]:
        started = time.perf_counter()
        timeout_seconds = ContentTrustProbeService._probe_timeout_seconds(
            provider,
            "combined_text",
            ContentGuardProbeService.COMBINED_TEXT_PROBE_TIMEOUT_SECONDS,
        )
        try:
            result_map = await asyncio.wait_for(
                ContentGuardProbeService.probe_fixed_answer_and_pollution_rules(
                    provider,
                    provider_model,
                    endpoint_path=endpoint_path,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            result_map = {
                probe_key: ContentTrustProbeService.probe_error(
                    probe_key=probe_key,
                    endpoint_path=endpoint_path,
                    message=f"{ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key)}组合探针超过 {timeout_seconds:.1f}s 限制",
                    category="content_trust_probe_timeout",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                for probe_key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS
            }
        except Exception as exc:
            result_map = {
                probe_key: ContentTrustProbeService.probe_error(
                    probe_key=probe_key,
                    endpoint_path=endpoint_path,
                    message=str(exc) or "组合探针执行异常",
                    category="content_trust_probe_exception",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                for probe_key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS
            }
        results: list[tuple[int, dict[str, Any]]] = []
        for probe_key in ContentTrustProbeService.COMBINABLE_TEXT_PROBE_KEYS:
            result = dict(result_map.get(probe_key) or {})
            result["capability_key"] = f"content_{probe_key}"
            result["probe_key"] = probe_key
            result["probe_label"] = ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key)
            result["combined_probe_key"] = "fixed_answer_pollution_rules"
            result["combined_probe_label"] = "固定答案 + 外链广告识别"
            results.append((order_indexes.get(probe_key, 999), result))
        return results

    @staticmethod
    async def run_single_probe_with_boundary(
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        probe_key: str,
        *,
        order_index: int,
    ) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        timeout_seconds = ContentTrustProbeService._probe_timeout_seconds(
            provider,
            str(probe_key),
            ContentTrustProbeService.PROBE_TIMEOUT_SECONDS.get(str(probe_key), 12),
        )
        try:
            result = await asyncio.wait_for(
                ContentTrustProbeService.run_single_probe(provider, provider_model, endpoint_path, probe_key),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = ContentTrustProbeService.probe_error(
                probe_key=probe_key,
                endpoint_path=endpoint_path,
                message=f"{ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key)}超过 {timeout_seconds:.1f}s 限制",
                category="content_trust_probe_timeout",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            result = ContentTrustProbeService.probe_error(
                probe_key=probe_key,
                endpoint_path=endpoint_path,
                message=str(exc) or "探针执行异常",
                category="content_trust_probe_exception",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return order_index, result

    @staticmethod
    def probe_error(*, probe_key: str, endpoint_path: str, message: str, category: str, latency_ms: int) -> dict[str, Any]:
        probe_label = ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key)
        return ContentGuardProbeService.probe_failure(
            endpoint_path=endpoint_path,
            endpoint_label=probe_label,
            support_label=f"{probe_label}未通过",
            latency_ms=latency_ms,
            status_code=None,
            guard_result=ContentGuardProbeService.review_result(
                message,
                category=category,
            ),
        ) | {
            "capability_key": f"content_{probe_key}",
            "probe_key": probe_key,
            "probe_label": probe_label,
        }

    @staticmethod
    def skipped_probe(*, probe_key: str, endpoint_path: str, message: str) -> dict[str, Any]:
        is_required = probe_key in ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS
        return ContentGuardProbeService.mark_detection_result({
            "capability_key": f"content_{probe_key}",
            "probe_key": probe_key,
            "probe_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "endpoint_path": endpoint_path,
            "endpoint_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "required_missing" if is_required else "skipped",
            "support_label": "必需能力缺失" if is_required else "已跳过",
            "latency_ms": 0,
            "status_code": None,
            "message": message,
            "trace": [],
            "retryable": False,
            "required_probe": is_required,
            "required_missing": is_required,
            "content_guard": {
                "content_guard_result": ContentGuardRuleService.RESULT_BLOCK if is_required else ContentGuardRuleService.RESULT_REVIEW,
                "content_guard_risk_level": "high" if is_required else "medium",
                "content_guard_categories_json": '["content_trust_required_missing"]' if is_required else '["content_trust_probe_skipped"]',
                "content_guard_reason": f"必需可信探针不可用：{message}" if is_required else message,
                "content_guard_action": "block" if is_required else "record",
            },
        })

    @staticmethod
    def maintenance_probe(*, probe_key: str, endpoint_path: str, provider: Provider | Any) -> dict[str, Any]:
        message = ContentTrustProbeService._provider_maintenance_message(provider)
        return ContentGuardProbeService.mark_detection_result({
            "capability_key": f"content_{probe_key}",
            "probe_key": probe_key,
            "probe_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "endpoint_path": endpoint_path,
            "endpoint_label": ContentTrustProbeService.PROBE_LABELS.get(probe_key, probe_key),
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "provider_maintenance_mode",
            "support_label": "提供商维护中",
            "latency_ms": 0,
            "status_code": 503,
            "message": message,
            "trace": [],
            "retryable": True,
            "required_probe": probe_key in ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS,
            "provider_maintenance_mode": True,
            "error_code": "provider_maintenance_mode",
            "content_guard": {
                "content_guard_result": "maintenance",
                "content_guard_risk_level": "none",
                "content_guard_categories_json": '["provider_maintenance_mode"]',
                "content_guard_reason": message,
                "content_guard_action": "skip",
            },
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
        aggregate_guard = ContentTrustProbeService.aggregate_content_guard_result(results) or {
            "content_guard_result": ContentGuardRuleService.RESULT_PASS,
            "content_guard_reason": "未返回内容防护结果",
        }
        decision = ContentGuardProbeService._content_probe_decision(
            [ContentGuardProbeService.summarize_probe_result(item) for item in results],
            aggregate_guard,
        )
        return ContentGuardRuleService.summarize_probe_guard_results(results, decision=decision)

    @staticmethod
    def aggregate_content_guard_result(results: list[dict[str, Any]]) -> dict[str, Any] | None:
        return ContentGuardRuleService.aggregate_probe_guard_result(results)

from app.utils.timezone import now_beijing