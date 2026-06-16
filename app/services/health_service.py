from app.utils.timezone import now_beijing
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import time
from types import SimpleNamespace
from typing import Any

import httpx
import requests
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.database import SessionLocal
from app.schemas.content_guard import ContentGuardRunRequest
from app.services.cache_service import CacheService
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.log_service import LogService
from app.logging.adapters.health_adapter import HealthLogRecorder
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.probe_error_policy_service import ProbeErrorPolicyService
from app.services.probe_rate_limit_service import ProbeRateLimitService
from app.services.provider_service import ProviderService
from app.services.native_protocol_adapter import NativeProtocolAdapter
from app.services.proxy_service import PreparedUpstreamRequest, ProxyService, StreamTimeoutPolicy
from app.services.redis_service import RedisService
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService


VISION_TEST_IMAGE_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
HealthProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class HealthService:
    MANUAL_CHECK_MIN_INTERVAL_SEC = 300
    PROBE_RETRY_MAX_ATTEMPTS = 2
    PROBE_RETRY_DELAY_SEC = 0.35
    MAX_PARALLEL_MODEL_PROBES = 16
    MAX_GLOBAL_PHASE_PROVIDERS = 4
    INTERACTIVE_TEXT_PROBE_PHASE_KEYS = frozenset({"text_stream"})
    INTERACTIVE_TEXT_PROBE_MAX_TOKENS = 1
    INTERACTIVE_CAPABILITY_PROBE_MAX_TOKENS = 1
    INTERACTIVE_PROBE_TIMEOUT_SECONDS = 8.0
    INTERACTIVE_PROBE_TIMEOUT_CAP_SECONDS = 175.0
    INTERACTIVE_PROBE_TIMEOUT_BUFFER_SECONDS = 2.0
    INTERACTIVE_STREAM_CONNECT_TIMEOUT_SECONDS = 4
    INTERACTIVE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS = 4
    HEALTH_STREAM_PROGRESS_QUEUE_SIZE = 200
    MANUAL_PROVIDER_SCAN_LIMIT = 200
    SCHEDULED_ACTIVE_MODEL_WINDOW_MINUTES = 30
    SCHEDULED_TEXT_PROBE_MAX_TOKENS = 4
    SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS = 8
    SCHEDULED_CONTENT_INTEGRITY_MODEL_LIMIT = 20
    SCHEDULED_CONTENT_INTEGRITY_CONCURRENCY = 3
    SCHEDULED_PROVIDER_SCAN_LIMIT = 200
    SCHEDULED_CAPABILITY_RESULT_TTL_SECONDS = 60 * 30
    AUTO_MODEL_PROBE_LIMIT_PER_MINUTE = 2
    PROVIDER_MODEL_PROBE_STAGGER_SECONDS = 10.0
    CONTENT_GUARD_PROBE_PHASE_AUDIT_KEYS = (
        "content_fixed_answer",
        "content_pollution_rules",
        "content_json",
        "content_sse",
    )

    @staticmethod
    async def _gather_staggered_by_previous_completion(
        items: list[Any],
        runner: Callable[[Any], Awaitable[Any]],
        *,
        stagger_seconds: float | None = None,
    ) -> list[Any]:
        """Start ordered probe items with a max wait for the previous item."""
        if not items:
            return []
        wait_seconds = (
            HealthService.PROVIDER_MODEL_PROBE_STAGGER_SECONDS
            if stagger_seconds is None
            else max(0.0, float(stagger_seconds))
        )
        tasks: list[asyncio.Task[Any]] = []
        previous_task: asyncio.Task[Any] | None = None
        for item in items:
            if previous_task is not None and wait_seconds > 0:
                await asyncio.wait({previous_task}, timeout=wait_seconds)
            task = asyncio.create_task(runner(item))
            tasks.append(task)
            previous_task = task
        return list(await asyncio.gather(*tasks))
    CONTENT_GUARD_PROBE_PHASE_KEYS = frozenset(
        f"content_{probe_key}" for probe_key in ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS
    )

    @staticmethod
    def cached_provider_status_summary(db: Session) -> dict:
        setting = SettingService.get_or_create(db)
        cache_key = "provider-status-summary"
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached
        row = db.execute(
            select(
                func.count(Provider.id).label("provider_count"),
                func.coalesce(func.sum(case((Provider.health_status == "healthy", 1), else_=0)), 0).label("healthy_provider_count"),
                func.coalesce(func.sum(case((Provider.health_status == "degraded", 1), else_=0)), 0).label("degraded_provider_count"),
                func.coalesce(func.sum(case((Provider.health_status == "unhealthy", 1), else_=0)), 0).label("unhealthy_provider_count"),
                func.coalesce(func.sum(case((Provider.circuit_state == "open", 1), else_=0)), 0).label("open_circuit_provider_count"),
            )
        ).one()
        payload = {
            "provider_count": int(row.provider_count or 0),
            "healthy_provider_count": int(row.healthy_provider_count or 0),
            "degraded_provider_count": int(row.degraded_provider_count or 0),
            "unhealthy_provider_count": int(row.unhealthy_provider_count or 0),
            "open_circuit_provider_count": int(row.open_circuit_provider_count or 0),
        }
        return CacheService.set(cache_key, payload, ttl_seconds=max(0, int(setting.provider_status_cache_ttl_sec)))

    @staticmethod
    def _scheduled_enabled_providers(
        db: Session,
        *,
        content_guard_only: bool = False,
        apply_scan_limit: bool = True,
        include_models: bool = True,
    ) -> list[Provider]:
        options = [selectinload(Provider.provider_models)] if include_models else []
        stmt = (
            select(Provider)
            .options(*options)
            .where(Provider.enabled.is_(True))
            .order_by(Provider.priority.asc(), Provider.id.asc())
        )
        if content_guard_only:
            stmt = stmt.where(Provider.content_guard_enabled.is_(True))
        if apply_scan_limit:
            stmt = stmt.limit(HealthService.SCHEDULED_PROVIDER_SCAN_LIMIT)
        return list(db.scalars(stmt))

    @staticmethod
    async def check_provider(
        db: Session,
        provider: Provider,
        *,
        include_disabled_models: bool = False,
        phase_keys: set[str] | frozenset[str] | None = None,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        progress_callback: HealthProgressCallback | None = None,
        interactive_mode: bool = False,
        single_endpoint_mode: bool = False,
    ) -> dict:
        run = (
            HealthLogRecorder.start_run(
                db,
                trigger_type="manual_single",
                scope_type="provider",
                scope_id=provider.id,
                phase_keys=phase_keys or HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
            )
            if interactive_mode
            else None
        )
        models_to_check = [item for item in provider.provider_models if include_disabled_models or item.enabled]
        model_results = await HealthService._run_provider_model_checks(
            provider,
            models_to_check,
            phase_keys=phase_keys,
            text_probe_max_tokens=text_probe_max_tokens,
            capability_probe_max_tokens=capability_probe_max_tokens,
            progress_callback=progress_callback,
            interactive_mode=interactive_mode,
            single_endpoint_mode=single_endpoint_mode,
        )
        provider_result = HealthService._finalize_provider_check(db, provider, models_to_check, model_results)
        if run is not None:
            run_payload = [{
                "provider_id": provider.id,
                "provider_name": provider.name,
                **provider_result,
            }]
            HealthService._record_run_results(db, run_id=run.run_id, provider_results=run_payload)
            HealthLogRecorder.finish_run(db, run_id=run.run_id, results=run_payload)
        return provider_result

    @staticmethod
    async def check_selected_providers(
        db: Session,
        *,
        provider_ids: list[int] | None = None,
        include_disabled_models: bool = True,
        phase_keys: set[str] | frozenset[str] | None = None,
        text_probe_max_tokens: int | None = None,
        interactive_mode: bool = False,
        single_endpoint_mode: bool = False,
    ) -> list[dict]:
        run = (
            HealthLogRecorder.start_run(
                db,
                trigger_type="manual_batch",
                scope_type="provider" if provider_ids else "all",
                scope_id=",".join(str(item) for item in provider_ids) if provider_ids else None,
                phase_keys=phase_keys or HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
            )
            if interactive_mode
            else None
        )
        if provider_ids:
            providers = list(
                db.scalars(
                    select(Provider)
                    .options(selectinload(Provider.provider_models))
                    .where(Provider.id.in_(provider_ids))
                    .order_by(Provider.priority.asc(), Provider.id.asc())
                )
            )
        else:
            providers = ProviderService.list_providers(db)
        if provider_ids:
            provider_map = {provider.id: provider for provider in providers}
            providers = [provider_map[provider_id] for provider_id in provider_ids if provider_id in provider_map]
        endpoint_results_by_provider_id = {
            provider.id: {provider_model.id: [] for provider_model in provider.provider_models}
            for provider in providers
        }

        phase_groups_by_provider = {
            provider.id: HealthService._build_probe_phase_groups(
                provider,
                phase_keys=phase_keys,
                text_probe_max_tokens=text_probe_max_tokens,
                single_endpoint_mode=single_endpoint_mode,
            )
            for provider in providers
        }
        phase_count = max((len(groups) for groups in phase_groups_by_provider.values()), default=0)
        for phase_index in range(phase_count):
            await asyncio.gather(
                *(
                    HealthService._run_provider_phase_group(
                        provider,
                        [item for item in provider.provider_models if include_disabled_models or item.enabled],
                        phase_groups_by_provider[provider.id][phase_index],
                        phase_index=phase_index + 1,
                        endpoint_results_by_model_id=endpoint_results_by_provider_id[provider.id],
                        interactive_mode=interactive_mode,
                    )
                    for provider in providers
                    if phase_index < len(phase_groups_by_provider[provider.id])
                )
            )

        results: list[dict] = []
        for provider in providers:
            models_to_check = [item for item in provider.provider_models if include_disabled_models or item.enabled]
            model_results = [
                HealthService._build_model_result(
                    provider,
                    provider_model,
                    endpoint_results_by_provider_id[provider.id].get(provider_model.id, []),
                )
                for provider_model in models_to_check
            ]
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_enabled": provider.enabled,
                    **HealthService._finalize_provider_check(db, provider, models_to_check, model_results),
                }
            )
        if run is not None:
            HealthService._record_run_results(db, run_id=run.run_id, provider_results=results)
            HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def check_provider_model(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        stream_probe: bool = False,
        vision_probe: bool = False,
        phase_keys: set[str] | frozenset[str] | None = None,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        interactive_mode: bool = False,
        parallel_phases: bool = False,
        single_endpoint_mode: bool = False,
    ) -> dict:
        if not interactive_mode and not HealthService._claim_auto_model_probe_slot(provider.id, provider_model.id):
            now = now_beijing()
            provider_model.health_status = "unhealthy"
            provider_model.circuit_state = "open"
            provider_model.circuit_opened_at = now
            provider_model.last_check_at = now
            provider_model.last_error = "自动可用性探针频率超过每分钟 2 次限制，未继续触发上游探针，按异常状态处理"
            ProviderService.refresh_provider_state(provider)
            db.commit()
            ProviderHealthStateService.record_model_probe(
                provider,
                provider_model,
                success=False,
                health_status="unhealthy",
                circuit_state="open",
                latency_ms=0,
            )
            return {
                "model_name": provider_model.model_name,
                "success": False,
                "provider_success": False,
                "health_status": "unhealthy",
                "latency_ms": 0,
                "status_code": 429,
                "message": provider_model.last_error,
                "probe_rate_limited": True,
                "endpoint_results": [
                    {
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                        "endpoint_label": "自动可用性探针频率限制",
                        "capability_key": "auto_probe_rate_limit",
                        "success": False,
                        "status_code": 429,
                        "latency_ms": 0,
                        "message": provider_model.last_error,
                    }
                ],
            }
        run = (
            HealthLogRecorder.start_run(
                db,
                trigger_type="manual_single",
                scope_type="model",
                scope_id=provider_model.id,
                phase_keys=phase_keys or HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
            )
            if interactive_mode
            else None
        )
        model_result = (
            await HealthService._run_provider_model_checks(
                provider,
                [provider_model],
                stream_probe=stream_probe,
                vision_probe=vision_probe,
                phase_keys=phase_keys,
                text_probe_max_tokens=text_probe_max_tokens,
                capability_probe_max_tokens=capability_probe_max_tokens,
                interactive_mode=interactive_mode,
                parallel_phases=parallel_phases,
                single_endpoint_mode=single_endpoint_mode,
            )
        )[0]
        HealthService._persist_model_health_result(db, provider, provider_model, model_result)
        requested_phase_keys = set(phase_keys or ())
        if interactive_mode and "content_trust_probe" in requested_phase_keys:
            try:
                trust_probe_result = await ContentTrustProbeService.run_trust_probe(
                    db,
                    ContentGuardRunRequest(
                        target_type="internal",
                        provider_id=provider.id,
                        provider_model_id=provider_model.id,
                        probe_keys=list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS),
                        persist_internal_result=True,
                    ),
                    detection_source="manual_health_trust_probe",
                )
                content_result = HealthService._content_trust_probe_model_result(provider_model, trust_probe_result)
            except Exception as exc:
                content_result = HealthService._content_trust_probe_error_result(provider_model, exc)
                ContentTrustProbeService.update_provider_model_trust_status(
                    db,
                    provider,
                    provider_model,
                    content_guard_result=content_result["endpoint_results"][0]["content_guard"],
                    endpoint_results=content_result["endpoint_results"],
                    detection_source="manual_health_trust_probe",
                )
            LogService.create_log(
                db,
                log_type="health_check_model",
                provider_id=provider.id,
                provider_name=provider.name,
                model_name=provider_model.model_name,
                request_path="/content-integrity-test",
                success=bool(content_result.get("success")),
                status_code=content_result.get("status_code"),
                latency_ms=int(content_result.get("latency_ms") or 0),
                message=content_result.get("message"),
                capability_result={
                    "endpoint_results": content_result.get("endpoint_results") or [],
                    "content_probe_summary": content_result.get("content_probe_summary"),
                },
            )
            model_result["trust_status"] = ProviderService.provider_model_trust_status(provider_model)
            model_result["trust_status_label"] = ProviderService.provider_model_to_dict(provider_model).get("trust_status_label")
            model_result["trust_status_reason"] = ProviderService.provider_model_to_dict(provider_model).get("trust_status_reason")
            model_result["content_probe_results"] = content_result.get("endpoint_results") or []
        if run is not None:
            provider_result = {
                "provider_id": provider.id,
                "provider_name": provider.name,
                "success": bool(model_result.get("success")),
                "model_results": [model_result],
            }
            HealthService._record_run_results(db, run_id=run.run_id, provider_results=[provider_result])
            HealthLogRecorder.finish_run(db, run_id=run.run_id, results=[model_result])
        return model_result

    @staticmethod
    def _claim_auto_model_probe_slot(provider_id: int, provider_model_id: int) -> bool:
        try:
            minute_bucket = int(now_beijing().timestamp() // 60)
            key = f"health:auto-model-probe-rate:{provider_id}:{provider_model_id}:{minute_bucket}"
            client = RedisService.get_sync_client()
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 90)
            count, _ = pipe.execute()
            return int(count or 0) <= HealthService.AUTO_MODEL_PROBE_LIMIT_PER_MINUTE
        except Exception:
            return False

    @staticmethod
    async def _probe_native_tools(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        max_tokens: int = 16,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = "tools"
        probe_specs: list[tuple[str, dict[str, Any]]] = []
        if ProviderService.provider_supports_responses(provider):
            probe_specs.append(
                (
                    "/responses",
                    HealthService._build_responses_tool_probe_payload(
                        provider_model,
                        max_output_tokens=max_tokens,
                    ),
                )
            )
        if ProviderService.provider_supports_chat_completions(provider):
            probe_specs.append(
                (
                    "/chat/completions",
                    HealthService._build_chat_tool_probe_payload(provider_model, max_tokens=max_tokens),
                )
            )
        if not probe_specs:
            return {
                "endpoint_path": None,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 tools",
                "latency_ms": 0,
                "status_code": None,
                "message": "模型未启用原生 chat/completions 或 responses 工具调用探测",
                "trace": [],
            }
        setting = await ProxyService._get_setting_async()
        last_error: dict[str, Any] | None = None
        for endpoint_path, payload in probe_specs:
            try:
                prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
                response, _ = await ProxyService._send_prepared_json(
                    provider,
                    prepared=prepared,
                    headers={"Authorization": f"Bearer {provider.api_key}"},
                    requested_payload=payload,
                    setting=setting,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                has_tool_call = HealthService._response_has_tool_call(response)
                if has_tool_call:
                    return HealthService._attach_probe_raw_provider_response({
                        "endpoint_path": endpoint_path,
                        "endpoint_label": endpoint_label,
                        "success": True,
                        "native_success": True,
                        "adapted_success": False,
                        "support_mode": "native",
                        "support_label": "原生支持 tools",
                        "latency_ms": latency_ms,
                        "status_code": 200,
                        "message": "ok",
                        "trace": [],
                    }, response=response)
                last_error = {
                    "endpoint_path": endpoint_path,
                    "status_code": 200,
                    "message": "未返回工具调用",
                }
            except Exception as exc:
                status_code = HealthService._exception_status_code(exc)
                message = HealthService._exception_message(exc)
                support_mode, support_label = HealthService._probe_failure_support_state(
                    endpoint_label=endpoint_label,
                    unsupported_label="不支持 tools",
                    status_code=status_code,
                    message=message,
                )
                last_error = {
                    "endpoint_path": endpoint_path,
                    "status_code": status_code,
                    "message": message,
                    "support_mode": support_mode,
                    "support_label": support_label,
                }
                continue
        latency_ms = int((time.perf_counter() - started) * 1000)
        support_mode = str(last_error.get("support_mode") or "") if last_error else ""
        support_label = (
            str(last_error.get("support_label") or "")
            if support_mode == "unknown"
            else "不支持 tools"
        )
        return HealthService._attach_probe_raw_provider_response({
            "endpoint_path": last_error.get("endpoint_path") if last_error else None,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": support_mode or "unsupported",
            "support_label": support_label or "不支持 tools",
            "latency_ms": latency_ms,
            "status_code": last_error.get("status_code") if last_error else None,
            "message": last_error.get("message") if last_error else "工具调用探测失败",
            "trace": [],
        }, response=last_error.get("message") if last_error else None)

    @staticmethod
    async def _probe_native_tools_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        max_tokens: int = 16,
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = "tools"
        capability_key = HealthService._endpoint_capability_key("tools", endpoint_path)
        if endpoint_path == "/responses":
            endpoint_supported = (
                bool(provider_model.supports_responses)
                if interactive_mode
                else ProviderService.provider_supports_responses(provider)
            )
            payload = HealthService._build_responses_tool_probe_payload(provider_model, max_output_tokens=max_tokens)
            support_label = "Responses 工具调用"
        else:
            endpoint_supported = (
                bool(provider_model.supports_chat_completions)
                if interactive_mode
                else ProviderService.provider_supports_chat_completions(provider)
            )
            payload = HealthService._build_chat_tool_probe_payload(provider_model, max_tokens=max_tokens)
            support_label = "Chat 工具调用"
        if not endpoint_supported:
            result = {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": f"不支持 {support_label}",
                "latency_ms": 0,
                "status_code": None,
                "message": "提供商或模型未启用该端点原生工具调用探测",
                "trace": [],
            }
            return result
        setting = await ProxyService._get_setting_async()
        try:
            prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
            response, _ = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                requested_payload=payload,
                setting=setting,
                request_timeout_seconds=(
                    HealthService._interactive_probe_timeout_seconds(provider)
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            has_tool_call = HealthService._response_has_tool_call(response)
            result = {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": has_tool_call,
                "native_success": has_tool_call,
                "adapted_success": False,
                "support_mode": "native" if has_tool_call else "unsupported",
                "support_label": f"原生支持 {support_label}" if has_tool_call else f"不支持 {support_label}",
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": "ok" if has_tool_call else "未返回工具调用",
                "trace": [],
            }
            return HealthService._attach_probe_raw_provider_response(result, response=response)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            support_mode, failure_label = HealthService._probe_failure_support_state(
                endpoint_label=support_label,
                unsupported_label=f"不支持 {support_label}",
                status_code=status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": failure_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
            }, response=message)

    @staticmethod
    async def _probe_native_image_generation(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = "image_generation"
        if (
            not provider_model.supports_responses
            or not ProviderService.provider_model_supports_image_generation(provider_model)
        ):
            return {
                "endpoint_path": "/responses",
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 image_generation",
                "latency_ms": 0,
                "status_code": None,
                "message": "提供商或模型未满足 Responses + Tools + 图片生成探测条件",
                "trace": [],
            }
        setting = await ProxyService._get_setting_async()
        payload = HealthService._build_responses_image_generation_probe_payload(provider_model)
        try:
            prepared = ProxyService._prepare_upstream_request(provider, endpoint_path="/responses", payload=payload)
            response, _ = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers={"Authorization": f"Bearer {provider.api_key}"},
                requested_payload=payload,
                setting=setting,
                request_timeout_seconds=(
                    HealthService._interactive_probe_timeout_seconds(provider)
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            has_generated_image = HealthService._response_has_generated_image(response)
            result = {
                "endpoint_path": "/responses",
                "endpoint_label": endpoint_label,
                "success": has_generated_image,
                "native_success": has_generated_image,
                "adapted_success": False,
                "support_mode": "native" if has_generated_image else "unsupported",
                "support_label": "原生支持 image_generation" if has_generated_image else "不支持 image_generation",
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": (
                    ProxyService._extract_response_display_text(response, limit_bytes=160)
                    or ("ok" if has_generated_image else "未返回图片生成结果")
                ),
                "trace": [],
            }
            return HealthService._attach_probe_raw_provider_response(result, response=response)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label="image_generation",
                unsupported_label="不支持 image_generation",
                status_code=status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": "/responses",
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": support_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
            }, response=message)

    @staticmethod
    async def _probe_native_vision(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        max_tokens: int = 4,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = "vision"
        if not provider_model.supports_vision:
            return {
                "endpoint_path": None,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 vision",
                "latency_ms": 0,
                "status_code": None,
                "message": "模型未声明支持图像理解",
                "trace": [],
            }
        probe_specs: list[tuple[str, dict[str, Any]]] = []
        if ProviderService.provider_supports_responses(provider):
            probe_specs.append(
                (
                    "/responses",
                    HealthService._build_responses_probe_payload(
                        provider_model,
                        vision_probe=True,
                        max_output_tokens=max_tokens,
                    ),
                )
            )
        if ProviderService.provider_supports_chat_completions(provider):
            probe_specs.append(
                (
                    "/chat/completions",
                    HealthService._build_chat_probe_payload(
                        provider_model,
                        vision_probe=True,
                        stream_probe=False,
                        max_tokens=max_tokens,
                    ),
                )
            )
        last_error: dict[str, Any] | None = None
        for endpoint_path, payload in probe_specs:
            result = await HealthService._probe_formal_endpoint(
                provider,
                provider_model,
                endpoint_path=endpoint_path,
                payload=payload,
            )
            if result.get("success"):
                return {
                    **result,
                    "endpoint_label": endpoint_label,
                    "support_label": "原生支持 vision",
                }
            last_error = result
        latency_ms = int((time.perf_counter() - started) * 1000)
        support_mode = str(last_error.get("support_mode") or "") if last_error else ""
        support_label = (
            "vision 上游暂不可用，支持状态待确认"
            if support_mode == "unknown"
            else "不支持 vision"
        )
        return HealthService._attach_probe_raw_provider_response({
            "endpoint_path": last_error.get("endpoint_path") if last_error else None,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": support_mode or "unsupported",
            "support_label": support_label or "不支持 vision",
            "latency_ms": latency_ms,
            "status_code": last_error.get("status_code") if last_error else None,
            "message": last_error.get("message") if last_error else "图像理解探测失败",
            "trace": last_error.get("trace") if last_error else [],
        }, response=last_error.get("message") if last_error else None)

    @staticmethod
    async def _probe_native_vision_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        max_tokens: int = 4,
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        endpoint_label = "vision"
        capability_key = HealthService._endpoint_capability_key("vision", endpoint_path)
        if not provider_model.supports_vision:
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 vision",
                "latency_ms": 0,
                "status_code": None,
                "message": "模型未声明支持图像理解",
                "trace": [],
            }
        if endpoint_path == "/responses":
            endpoint_supported = (
                bool(provider_model.supports_responses)
                if interactive_mode
                else ProviderService.provider_supports_responses(provider)
            )
            payload = HealthService._build_responses_probe_payload(
                provider_model,
                vision_probe=True,
                max_output_tokens=max_tokens,
            )
            support_label = "Responses 图像理解"
        else:
            endpoint_supported = (
                bool(provider_model.supports_chat_completions)
                if interactive_mode
                else ProviderService.provider_supports_chat_completions(provider)
            )
            payload = HealthService._build_chat_probe_payload(
                provider_model,
                vision_probe=True,
                stream_probe=False,
                max_tokens=max_tokens,
            )
            support_label = "Chat 图像理解"
        if not endpoint_supported:
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": f"不支持 {support_label}",
                "latency_ms": 0,
                "status_code": None,
                "message": "提供商或模型未启用该端点原生图像理解探测",
                "trace": [],
            }
        result = await HealthService._probe_formal_endpoint(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            payload=payload,
            interactive_mode=interactive_mode,
        )
        if result.get("success"):
            next_support_label = f"原生支持 {support_label}"
        elif result.get("support_mode") == "unknown":
            next_support_label = result.get("support_label") or f"{support_label} 上游暂不可用，支持状态待确认"
        else:
            next_support_label = f"不支持 {support_label}"
        return {
            **result,
            "endpoint_label": endpoint_label,
            "capability_key": capability_key,
            "support_label": next_support_label,
        }

    @staticmethod
    async def check_all(
        db: Session,
        *,
        selective: bool = True,
        phase_keys: set[str] | frozenset[str] | None = None,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        progress_callback: HealthProgressCallback | None = None,
        interactive_mode: bool = False,
        parallel_phases: bool = False,
        single_endpoint_mode: bool = False,
    ) -> list[dict]:
        run = (
            HealthLogRecorder.start_run(
                db,
                trigger_type="manual_batch",
                scope_type="all",
                phase_keys=phase_keys or HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
            )
            if interactive_mode
            else None
        )
        providers = HealthService._scheduled_enabled_providers(
            db,
            apply_scan_limit=True,
            include_models=True,
        )
        if progress_callback is not None:
            providers = providers[:HealthService.MANUAL_PROVIDER_SCAN_LIMIT]
        route_metrics = LogService.route_metric_summary(
            db,
            window_minutes=HealthService.SCHEDULED_ACTIVE_MODEL_WINDOW_MINUTES,
        ) if selective else {}
        active_model_keys = set(route_metrics.keys())
        provider_models_map = {
            provider.id: [
                item
                for item in provider.provider_models
                if item.enabled
                and (
                    not selective
                    or HealthService._should_run_scheduled_text_probe(provider, item, active_model_keys)
                    or HealthService._should_run_scheduled_capability_probe(
                        provider,
                        item,
                        route_metrics=route_metrics,
                    )
                )
            ]
            for provider in providers
        }
        endpoint_results_by_provider_id = {
            provider.id: {
                provider_model.id: []
                for provider_model in provider_models_map[provider.id]
            }
            for provider in providers
        }
        results: list[dict] = []
        total_providers = len(providers)
        for provider_index, provider in enumerate(providers, start=1):
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_started",
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                },
            )
        phase_groups_by_provider = {
            provider.id: HealthService._build_probe_phase_groups(
                provider,
                phase_keys=phase_keys,
                selective_capability_probes=selective,
                route_metrics=route_metrics,
                text_probe_max_tokens=text_probe_max_tokens or HealthService.SCHEDULED_TEXT_PROBE_MAX_TOKENS,
                capability_probe_max_tokens=capability_probe_max_tokens or HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
                single_endpoint_mode=single_endpoint_mode,
            )
            for provider in providers
        }
        phase_count = max((len(groups) for groups in phase_groups_by_provider.values()), default=0)
        for phase_index in range(phase_count):
            provider_semaphore = asyncio.Semaphore(HealthService.MAX_GLOBAL_PHASE_PROVIDERS)

            async def run_provider_phase(provider: Provider) -> None:
                async with provider_semaphore:
                    await HealthService._run_provider_phase_group(
                        provider,
                        provider_models_map[provider.id],
                        phase_groups_by_provider[provider.id][phase_index],
                        phase_index=phase_index + 1,
                        endpoint_results_by_model_id=endpoint_results_by_provider_id[provider.id],
                        progress_callback=progress_callback,
                        interactive_mode=interactive_mode,
                    )

            await asyncio.gather(
                *(
                    run_provider_phase(provider)
                    for provider in providers
                    if phase_index < len(phase_groups_by_provider[provider.id])
                )
            )

        for provider_index, provider in enumerate(providers, start=1):
            models_to_check = provider_models_map[provider.id]
            model_results = [
                HealthService._build_model_result(
                    provider,
                    provider_model,
                    endpoint_results_by_provider_id[provider.id].get(provider_model.id, []),
                )
                for provider_model in models_to_check
            ]
            provider_result = HealthService._finalize_provider_check(db, provider, models_to_check, model_results)
            model_results = list(provider_result.get("model_results") or [])
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "scope": "provider",
                    **provider_result,
                }
            )
            for provider_model, model_result in zip(
                models_to_check,
                model_results,
                strict=False,
            ):
                results.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                        "scope": "model",
                        **model_result,
                    }
                )
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_completed",
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "success": provider_result.get("success"),
                    "models_total": provider_result.get("models_total"),
                    "models_success": provider_result.get("models_success"),
                    "models_failed": provider_result.get("models_failed"),
                },
            )
        if run is not None:
            provider_results = [item for item in results if item.get("scope") == "provider"]
            HealthService._record_run_results(db, run_id=run.run_id, provider_results=provider_results)
            HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def check_provider_connectivity_all(
        db: Session,
        *,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        run = HealthLogRecorder.start_run(
            db,
            trigger_type="manual_batch" if progress_callback is not None else "scheduled_l0",
            scope_type="all",
            phase_keys=["connectivity"],
        )
        providers = HealthService._scheduled_enabled_providers(db, apply_scan_limit=False)
        results: list[dict] = []
        total_providers = len(providers)
        for provider_index, provider in enumerate(providers, start=1):
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_started",
                    "level": "l0",
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                },
            )
            provider_result = await HealthService.check_provider_connectivity(db, provider, log_result=True)
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "scope": "provider",
                    "level": "l0",
                    **provider_result,
                }
            )
            HealthLogRecorder.record_probe(
                db,
                run_id=run.run_id,
                provider_id=provider.id,
                model_name=provider.name,
                probe_type="connectivity",
                endpoint_path="/models",
                success=bool(provider_result.get("success")),
                status_code=provider_result.get("status_code"),
                latency_ms=provider_result.get("latency_ms"),
                error_code=None if provider_result.get("success") else "provider_connectivity_failed",
                auto_commit=False,
            )
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_completed",
                    "level": "l0",
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "success": provider_result.get("success"),
                },
            )
        db.commit()
        HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def check_scheduled_text_models(
        db: Session,
        *,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        run = HealthLogRecorder.start_run(
            db,
            trigger_type="manual_batch" if progress_callback is not None else "scheduled_l1",
            scope_type="model",
            phase_keys=["text_stream"],
        )
        route_metrics = LogService.route_metric_summary(
            db,
            window_minutes=HealthService.SCHEDULED_ACTIVE_MODEL_WINDOW_MINUTES,
        )
        active_model_keys = set(route_metrics.keys())
        providers = HealthService._scheduled_enabled_providers(db, apply_scan_limit=False)
        results = await HealthService._check_scheduled_provider_models(
            db,
            providers,
            selector=lambda provider, provider_model: HealthService._should_run_scheduled_text_probe(
                provider,
                provider_model,
                active_model_keys,
            ),
            phase_keys={"text"},
            level="l1_text",
            text_probe_max_tokens=HealthService.SCHEDULED_TEXT_PROBE_MAX_TOKENS,
            update_health_state=True,
            progress_callback=progress_callback,
        )
        HealthService._record_run_results(db, run_id=run.run_id, provider_results=results)
        HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def check_scheduled_capability_models(
        db: Session,
        *,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        run = HealthLogRecorder.start_run(
            db,
            trigger_type="manual_batch" if progress_callback is not None else "scheduled_l2",
            scope_type="model",
            phase_keys=["tools", "vision"],
        )
        providers = HealthService._scheduled_enabled_providers(db, apply_scan_limit=False)
        results = await HealthService._check_scheduled_provider_models(
            db,
            providers,
            selector=lambda provider, provider_model: HealthService._should_run_scheduled_capability_probe(
                provider,
                provider_model,
            ),
            phase_keys={"tools", "vision"},
            level="l2_capability",
            capability_probe_max_tokens=HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
            update_health_state=False,
            progress_callback=progress_callback,
        )
        HealthService._record_run_results(db, run_id=run.run_id, provider_results=results)
        HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def check_scheduled_content_integrity_models(
        db: Session,
        *,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        setting = SettingService.get_or_create(db)
        if not bool(getattr(setting, "content_guard_enabled", True)):
            return []
        if progress_callback is None and not bool(getattr(setting, "content_guard_precheck_auto_enabled", False)):
            return []
        run = HealthLogRecorder.start_run(
            db,
            trigger_type="manual_batch" if progress_callback is not None else "scheduled_l3",
            scope_type="model",
            phase_keys=list(HealthService.CONTENT_GUARD_PROBE_PHASE_KEYS),
        )
        providers = HealthService._scheduled_enabled_providers(
            db,
            content_guard_only=True,
            apply_scan_limit=progress_callback is None,
        )
        results = await HealthService._run_scheduled_content_trust_probes(
            db,
            providers,
            setting=setting,
            progress_callback=progress_callback,
        )
        HealthService._record_run_results(db, run_id=run.run_id, provider_results=results)
        HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

    @staticmethod
    async def _run_scheduled_content_trust_probes(
        db: Session,
        providers: list[Provider],
        *,
        setting: Any,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        total_providers = len(providers)
        remaining_model_budget = HealthService.SCHEDULED_CONTENT_INTEGRITY_MODEL_LIMIT
        provider_targets: list[tuple[int, Provider, list[ProviderModel]]] = []
        for provider_index, provider in enumerate(providers, start=1):
            if remaining_model_budget <= 0:
                break
            models_to_check = [
                provider_model
                for provider_model in provider.provider_models
                if provider_model.enabled
                and HealthService._should_run_scheduled_content_probe(
                    provider,
                    provider_model,
                    setting=setting,
                )
            ]
            if len(models_to_check) > remaining_model_budget:
                models_to_check = models_to_check[:remaining_model_budget]
            remaining_model_budget -= len(models_to_check)

            provider_targets.append((provider_index, provider, models_to_check))

        async def run_provider_content_trust(
            provider_index: int,
            provider: Provider,
            models_to_check: list[ProviderModel],
        ) -> dict[str, Any]:
            semaphore = asyncio.Semaphore(
                max(1, min(HealthService.SCHEDULED_CONTENT_INTEGRITY_CONCURRENCY, len(models_to_check)))
            )

            async def run_model_probe(provider_model: ProviderModel) -> dict[str, Any]:
                await HealthService._emit_progress(
                    progress_callback,
                    {
                        "event": "model_started",
                        "level": "l3_content_integrity",
                        "provider_index": provider_index,
                        "provider_total": total_providers,
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                    },
                )
                async with semaphore:
                    model_result = await HealthService._run_scheduled_content_trust_probe_for_model(
                        provider.id,
                        provider_model.id,
                    )
                await HealthService._emit_progress(
                    progress_callback,
                    {
                        "event": "model_completed",
                        "level": "l3_content_integrity",
                        "provider_index": provider_index,
                        "provider_total": total_providers,
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                        "success": model_result.get("success"),
                    },
                )
                return model_result

            model_results = await HealthService._gather_staggered_by_previous_completion(
                models_to_check,
                run_model_probe,
            )
            for provider_model, model_result in zip(models_to_check, model_results, strict=False):
                if not model_result.get("model_name"):
                    model_result["model_name"] = provider_model.model_name
                if model_result.get("provider_model_id") is None:
                    model_result["provider_model_id"] = provider_model.id
                for endpoint_result in model_result.get("endpoint_results") or []:
                    endpoint_result.setdefault("provider_model_id", provider_model.id)
            models_total = len(models_to_check)
            models_success = sum(1 for item in model_results if item.get("success"))
            models_failed = max(0, models_total - models_success)
            skipped_no_due_models = models_total == 0
            provider_success = True if skipped_no_due_models else models_success > 0
            latency_ms = max((int(item.get("latency_ms") or 0) for item in model_results), default=0)
            status_code = next((item.get("status_code") for item in model_results if not item.get("success")), 204 if skipped_no_due_models else (200 if provider_success else None))
            message = (
                f"内容可信预检完成，模型 {models_success}/{models_total} 可信"
                if models_total
                else "当前没有达到自动预检条件的模型"
            )
            provider_result = {
                "provider_id": provider.id,
                "provider_name": provider.name,
                "success": provider_success and models_failed == 0,
                "provider_success": provider_success,
                "skipped": skipped_no_due_models,
                "health_status": provider.health_status,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "models_total": models_total,
                "models_success": models_success,
                "models_failed": models_failed,
                "model_results": model_results,
            }
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_completed",
                    "level": "l3_content_integrity",
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "success": provider_result.get("success"),
                    "models_total": models_total,
                    "models_success": models_success,
                    "models_failed": models_failed,
                },
            )
            return provider_result

        return list(
            await asyncio.gather(
                *(
                    run_provider_content_trust(provider_index, provider, models_to_check)
                    for provider_index, provider, models_to_check in provider_targets
                )
            )
        )

    @staticmethod
    async def _run_scheduled_content_trust_probe_for_model(
        provider_id: int,
        provider_model_id: int,
    ) -> dict[str, Any]:
        probe_db = SessionLocal()
        try:
            provider = probe_db.get(Provider, provider_id)
            provider_model = probe_db.get(ProviderModel, provider_model_id)
            if provider is None or provider_model is None:
                model_name = f"provider_model:{provider_model_id}"
                return {
                    "provider_model_id": provider_model_id,
                    "model_name": model_name,
                    "success": False,
                    "provider_success": False,
                    "health_status": "unknown",
                    "content_integrity_status": "unknown",
                    "latency_ms": 0,
                    "status_code": None,
                    "message": "内容可信预检目标不存在",
                    "endpoint_results": [
                        {
                            "provider_model_id": provider_model_id,
                            "capability_key": "content_trust_probe",
                            "endpoint_label": "内容可信预检",
                            "endpoint_path": None,
                            "success": False,
                            "support_mode": "target_not_found",
                            "message": "内容可信预检目标不存在",
                            "retryable": False,
                        }
                    ],
                }
            try:
                probe_result = await ContentTrustProbeService.run_trust_probe(
                    probe_db,
                    ContentGuardRunRequest(
                        target_type="internal",
                        provider_id=provider.id,
                        provider_model_id=provider_model.id,
                        probe_keys=list(ContentTrustProbeService.REQUIRED_TRUST_PROBE_KEYS),
                        persist_internal_result=True,
                    ),
                    detection_source="automatic_trust_probe",
                )
                return HealthService._content_trust_probe_model_result(provider_model, probe_result)
            except Exception as exc:
                model_result = HealthService._content_trust_probe_error_result(provider_model, exc)
                ContentTrustProbeService.update_provider_model_trust_status(
                    probe_db,
                    provider,
                    provider_model,
                    content_guard_result=model_result["endpoint_results"][0]["content_guard"],
                    endpoint_results=model_result["endpoint_results"],
                    detection_source="automatic_trust_probe",
                )
                return model_result
        finally:
            probe_db.close()

    @staticmethod
    def _content_trust_probe_model_result(provider_model: ProviderModel, probe_result: dict[str, Any]) -> dict[str, Any]:
        summary = probe_result.get("summary") or {}
        endpoint_results = list(probe_result.get("probe_results") or [])
        for endpoint_result in endpoint_results:
            endpoint_result.setdefault("provider_model_id", provider_model.id)
        success = str(summary.get("status") or "") == "passed" and str(summary.get("content_guard_result") or "") == "pass"
        latency_ms = max((int(item.get("latency_ms") or 0) for item in endpoint_results), default=0)
        status_code = next((item.get("status_code") for item in endpoint_results if not item.get("success")), 200 if success else None)
        reason = str(summary.get("content_guard_reason") or "")
        return {
            "model_name": provider_model.model_name,
            "success": success,
            "provider_success": success,
            "health_status": provider_model.health_status,
            "content_integrity_status": provider_model.content_integrity_status,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": reason or ("内容可信预检通过" if success else "内容可信预检未通过"),
            "endpoint_results": endpoint_results,
            "content_probe_summary": summary,
        }

    @staticmethod
    def _content_trust_probe_error_result(provider_model: ProviderModel, exc: Exception) -> dict[str, Any]:
        reason = str(exc)[:500] or "内容可信预检异常"
        retryable = HealthService._content_probe_exception_retryable(exc)
        content_guard = {
            "content_guard_result": ContentGuardRuleService.RESULT_REVIEW,
            "content_guard_risk_level": "medium",
            "content_guard_reason": reason,
            "content_guard_action": "record",
        }
        endpoint_result = {
            "provider_model_id": provider_model.id,
            "capability_key": "content_trust_probe",
            "probe_key": "trust_probe",
            "probe_label": "内容可信预检",
            "endpoint_label": "内容可信预检",
            "endpoint_path": None,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "error",
            "support_label": "预检异常",
            "latency_ms": 0,
            "status_code": None,
            "message": reason,
            "trace": [],
            "retryable": retryable,
            "content_guard": content_guard,
        }
        return {
            "model_name": provider_model.model_name,
            "success": False,
            "provider_success": False,
            "health_status": provider_model.health_status,
            "content_integrity_status": provider_model.content_integrity_status,
            "latency_ms": 0,
            "status_code": None,
            "message": reason,
            "endpoint_results": [endpoint_result],
            "content_probe_summary": {
                "status": "review",
                "content_guard_result": ContentGuardRuleService.RESULT_REVIEW,
                "content_guard_reason": reason,
                "total": 1,
                "passed": 0,
                "failed": 1,
            },
        }

    @staticmethod
    async def _check_scheduled_provider_models(
        db: Session,
        providers: list[Provider],
        *,
        selector: Callable[[Provider, ProviderModel], bool],
        phase_keys: set[str],
        level: str,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        update_health_state: bool,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        results: list[dict] = []
        total_providers = len(providers)
        for provider_index, provider in enumerate(providers, start=1):
            models_to_check = [
                provider_model
                for provider_model in provider.provider_models
                if provider_model.enabled and selector(provider, provider_model)
            ]
            if not models_to_check:
                continue
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_started",
                    "level": level,
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "models_total": len(models_to_check),
                },
            )
            model_results = await HealthService._run_provider_model_checks(
                provider,
                models_to_check,
                phase_keys=phase_keys,
                text_probe_max_tokens=text_probe_max_tokens,
                capability_probe_max_tokens=capability_probe_max_tokens,
                progress_callback=progress_callback,
            )
            if level == "l2_capability":
                HealthService._cache_capability_probe_results(provider, models_to_check, model_results)
            provider_result = HealthService._finalize_provider_check(
                db,
                provider,
                models_to_check,
                model_results,
                request_path=f"/scheduled-{level}",
                update_health_state=update_health_state,
            )
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "scope": "provider",
                    "level": level,
                    **provider_result,
                }
            )
            for provider_model, model_result in zip(models_to_check, provider_result.get("model_results") or [], strict=False):
                results.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                        "scope": "model",
                        "level": level,
                        **model_result,
                    }
                )
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "provider_completed",
                    "level": level,
                    "provider_index": provider_index,
                    "provider_total": total_providers,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "success": provider_result.get("success"),
                    "models_total": provider_result.get("models_total"),
                    "models_success": provider_result.get("models_success"),
                    "models_failed": provider_result.get("models_failed"),
                },
            )
        return results

    @staticmethod
    def _record_run_results(db: Session, *, run_id: str, provider_results: list[dict]) -> None:
        def _probe_protocol_type(endpoint_result: dict[str, Any]) -> str | None:
            protocol_type = endpoint_result.get("protocol_type") or endpoint_result.get("endpoint_type")
            if protocol_type:
                return str(protocol_type)
            endpoint_path = str(endpoint_result.get("endpoint_path") or "")
            if endpoint_path == "/chat/completions":
                return "chat_completions"
            if endpoint_path == "/responses":
                return "responses"
            if endpoint_path.startswith("/native/"):
                native_protocol = endpoint_path.removeprefix("/native/")
                return native_protocol if native_protocol in {"gemini", "claude_messages"} else None
            return None

        for provider_result in provider_results:
            provider_id = provider_result.get("provider_id")
            for model_result in provider_result.get("model_results") or []:
                endpoint_results = list(model_result.get("endpoint_results") or [])
                content_probe_results = list(model_result.get("content_probe_results") or [])
                endpoint_results.extend(content_probe_results)
                if not endpoint_results:
                    HealthLogRecorder.record_probe(
                        db,
                        run_id=run_id,
                        provider_id=provider_id,
                        model_name=model_result.get("model_name"),
                        probe_type="model_probe",
                        success=bool(model_result.get("success")),
                        status_code=model_result.get("status_code"),
                        latency_ms=model_result.get("latency_ms"),
                        error_code=None if model_result.get("success") else str(model_result.get("error_code") or "probe_failed"),
                        capability_result=model_result,
                        auto_commit=False,
                    )
                    continue
                for endpoint_result in endpoint_results:
                    endpoint_path = endpoint_result.get("endpoint_path")
                    HealthLogRecorder.record_probe(
                        db,
                        run_id=run_id,
                        provider_id=provider_id,
                        provider_model_id=endpoint_result.get("provider_model_id"),
                        model_name=model_result.get("model_name"),
                        probe_type=str(endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label") or "probe"),
                        endpoint_path=endpoint_path,
                        protocol_type=_probe_protocol_type(endpoint_result),
                        success=bool(endpoint_result.get("success")),
                        status_code=endpoint_result.get("status_code"),
                        latency_ms=endpoint_result.get("latency_ms"),
                        error_code=None if endpoint_result.get("success") else HealthService._probe_result_error_code(endpoint_result),
                        capability_result=endpoint_result,
                        content_guard_result=endpoint_result.get("content_guard") or endpoint_result.get("content_guard_result"),
                        auto_commit=False,
                    )
        db.commit()

    @staticmethod
    def _probe_result_error_code(endpoint_result: dict[str, Any]) -> str:
        if endpoint_result.get("error_code"):
            return str(endpoint_result.get("error_code"))
        content_guard = endpoint_result.get("content_guard")
        if isinstance(content_guard, dict):
            categories = content_guard.get("content_guard_categories_json") or content_guard.get("categories")
            policy = ProbeErrorPolicyService.classify(
                status_code=endpoint_result.get("status_code"),
                message=endpoint_result.get("message") or content_guard.get("content_guard_reason"),
                support_mode=endpoint_result.get("support_mode"),
                category=categories,
                retryable=endpoint_result.get("retryable"),
                probe_kind="health",
            )
            return policy.error_code
        policy = ProbeErrorPolicyService.classify(
            status_code=endpoint_result.get("status_code"),
            message=endpoint_result.get("message") or endpoint_result.get("support_label"),
            detail=endpoint_result.get("error_detail"),
            support_mode=endpoint_result.get("support_mode"),
            retryable=endpoint_result.get("retryable"),
            probe_kind="health",
        )
        return policy.error_code

    @staticmethod
    async def _run_provider_model_checks(
        provider: Provider,
        models_to_check: list[ProviderModel],
        *,
        stream_probe: bool = False,
        vision_probe: bool = False,
        phase_keys: set[str] | None = None,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        progress_callback: HealthProgressCallback | None = None,
        interactive_mode: bool = False,
        parallel_phases: bool = False,
        single_endpoint_mode: bool = False,
    ) -> list[dict]:
        manual_probe = bool(interactive_mode or progress_callback is not None)
        if not manual_probe and HealthService._provider_in_maintenance(provider):
            return [
                HealthService._maintenance_model_result(provider, provider_model)
                for provider_model in models_to_check
            ]
        endpoint_results_by_model_id: dict[int, list[dict[str, Any]]] = {
            provider_model.id: []
            for provider_model in models_to_check
        }
        actual_models_to_check: list[ProviderModel] = []
        for provider_model in models_to_check:
            if manual_probe or HealthService._claim_auto_model_probe_slot(provider.id, provider_model.id):
                actual_models_to_check.append(provider_model)
                continue
            endpoint_results_by_model_id[provider_model.id].append(
                {
                    "provider_model_id": provider_model.id,
                    "model_name": provider_model.model_name,
                    "endpoint_label": "自动可用性探针频率限制",
                    "capability_key": "auto_probe_rate_limit",
                    "success": False,
                    "status_code": 429,
                    "latency_ms": 0,
                    "message": "自动可用性探针频率超过每分钟 2 次限制，未继续触发上游探针，按异常状态处理",
                }
            )
        models_to_probe = actual_models_to_check
        phase_specs = HealthService._build_probe_phase_groups(
            provider,
            stream_probe=stream_probe,
            vision_probe=vision_probe,
            phase_keys=phase_keys,
            text_probe_max_tokens=text_probe_max_tokens,
            capability_probe_max_tokens=capability_probe_max_tokens,
            interactive_mode=interactive_mode,
            single_endpoint_mode=single_endpoint_mode,
        )
        if models_to_probe and interactive_mode and parallel_phases:
            phase_results = await asyncio.gather(
                *(
                    HealthService._run_provider_phase_group(
                        provider,
                        models_to_probe,
                        phase_spec,
                        phase_index=phase_index,
                        endpoint_results_by_model_id=endpoint_results_by_model_id,
                        progress_callback=progress_callback,
                        interactive_mode=interactive_mode,
                    )
                    for phase_index, phase_spec in enumerate(phase_specs, start=1)
                )
            )
            _ = phase_results
        elif models_to_probe:
            for phase_index, phase_spec in enumerate(phase_specs, start=1):
                await HealthService._run_provider_phase_group(
                    provider,
                    models_to_probe,
                    phase_spec,
                    phase_index=phase_index,
                    endpoint_results_by_model_id=endpoint_results_by_model_id,
                    progress_callback=progress_callback,
                    interactive_mode=interactive_mode,
                )
        return [
            HealthService._build_model_result(
                provider,
                provider_model,
                endpoint_results_by_model_id.get(provider_model.id, []),
            )
            for provider_model in models_to_check
        ]

    @staticmethod
    def _provider_in_maintenance(provider: Any) -> bool:
        return bool(getattr(provider, "maintenance_mode_enabled", False))

    @staticmethod
    def _provider_maintenance_message(provider: Any) -> str:
        maintenance_window = str(getattr(provider, "maintenance_window", "") or "").strip()
        if maintenance_window:
            return f"提供商当前处于维护模式（{maintenance_window}），自动探针不执行；请在维护结束后重试，或由用户手动检测。"
        return "提供商当前处于维护模式，自动探针不执行；请在维护结束后重试，或由用户手动检测。"

    @staticmethod
    def _maintenance_model_result(provider: Any, provider_model: Any) -> dict[str, Any]:
        message = HealthService._provider_maintenance_message(provider)
        return {
            "provider_model_id": getattr(provider_model, "id", None),
            "model_name": getattr(provider_model, "model_name", None),
            "success": False,
            "provider_success": False,
            "status": "skipped",
            "error_code": "provider_maintenance_mode",
            "provider_maintenance_mode": True,
            "health_status": getattr(provider_model, "health_status", "unknown"),
            "latency_ms": 0,
            "status_code": 503,
            "message": message,
            "endpoint_results": [
                {
                    "provider_model_id": getattr(provider_model, "id", None),
                    "model_name": getattr(provider_model, "model_name", None),
                    "endpoint_label": "维护模式",
                    "capability_key": "provider_maintenance_mode",
                    "success": False,
                    "status_code": 503,
                    "latency_ms": 0,
                    "support_mode": "provider_maintenance_mode",
                    "provider_maintenance_mode": True,
                    "error_code": "provider_maintenance_mode",
                    "message": message,
                }
            ],
        }

    @staticmethod
    async def _claim_probe_rate_limit_result(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        probe_type: str,
        limit_per_minute: int | None = None,
        window_seconds: int | None = None,
    ) -> Any:
        return await ProbeRateLimitService.claim(
            provider,
            provider_model,
            probe_type=probe_type,
            limit_per_minute=limit_per_minute,
            window_seconds=window_seconds,
        )

    @staticmethod
    def _endpoint_protocol_provider(target: dict[str, Any]) -> Any:
        provider_payload = dict(target.get("provider") or {})
        return SimpleNamespace(
            id=provider_payload.get("id", target.get("provider_id")),
            name=provider_payload.get("name", target.get("provider_name")),
            base_url=provider_payload.get("base_url"),
            api_key=provider_payload.get("api_key"),
            provider_type=provider_payload.get("provider_type", "openai_compatible"),
            protocol_type=provider_payload.get("protocol_type", target.get("previous_protocol_type") or "both"),
            timeout_ms=provider_payload.get("timeout_ms", 30000),
            max_retries=provider_payload.get("max_retries", 0),
            first_token_timeout_sec=provider_payload.get("first_token_timeout_sec", 60),
            maintenance_mode_enabled=provider_payload.get("maintenance_mode_enabled", False),
            maintenance_window=provider_payload.get("maintenance_window"),
        )

    @staticmethod
    def _endpoint_protocol_model(target: dict[str, Any]) -> Any:
        return SimpleNamespace(
            id=target.get("provider_model_id"),
            provider_id=target.get("provider_id"),
            model_name=target.get("model_name"),
            supports_chat_completions=bool(target.get("previous_supports_chat_completions")),
            supports_responses=bool(target.get("previous_supports_responses")),
            protocol_type=target.get("previous_protocol_type") or "responses",
        )

    @staticmethod
    def _endpoint_protocol_response_is_valid(endpoint_type: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if endpoint_type == "chat_completions":
            choices = payload.get("choices")
            return payload.get("object") == "chat.completion" and isinstance(choices, list) and bool(choices)
        if endpoint_type == "responses":
            if payload.get("object") != "response":
                return False
            if str(payload.get("status") or "").lower() in {"failed", "cancelled", "incomplete"}:
                return False
            output = payload.get("output")
            return isinstance(output, list) and bool(output)
        return False

    @staticmethod
    def _endpoint_protocol_payload(endpoint_path: str, model_name: str) -> dict[str, Any]:
        if endpoint_path == "/chat/completions":
            return {
                "model": model_name,
                "messages": [{"role": "user", "content": "只回复 pong"}],
                "max_tokens": 16,
                "stream": False,
            }
        return {
            "model": model_name,
            "input": "只回复 pong",
            "max_output_tokens": 16,
            "stream": False,
        }

    @staticmethod
    async def _probe_endpoint_protocol(
        provider: Any,
        *,
        model_name: str,
        endpoint_path: str,
        setting: Any,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_type = "chat_completions" if endpoint_path == "/chat/completions" else "responses"
        try:
            payload = HealthService._endpoint_protocol_payload(endpoint_path, model_name)
            prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
            response_payload, _ = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers={"Accept-Encoding": "identity"},
                requested_payload=payload,
                setting=setting,
                request_timeout_seconds=timeout_seconds,
            )
            success = HealthService._endpoint_protocol_response_is_valid(endpoint_type, response_payload)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_type": endpoint_type,
                "success": success,
                "support_state": "supported" if success else "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "message": "端点协议检测通过" if success else "端点返回结构不符合标准，保留原协议配置",
            }
        except httpx.HTTPStatusError as exc:
            message = await HealthService._safe_error_text(exc.response)
            status_code = exc.response.status_code
            explicit_unsupported = HealthService._is_explicit_endpoint_unsupported(status_code, message)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_type": endpoint_type,
                "success": False,
                "support_state": "unsupported" if explicit_unsupported else "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": message,
            }
        except Exception as exc:
            return {
                "endpoint_path": endpoint_path,
                "endpoint_type": endpoint_type,
                "success": False,
                "support_state": "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "message": str(exc),
            }

    @staticmethod
    def _is_explicit_endpoint_unsupported(status_code: int | None, message: str) -> bool:
        normalized = str(message or "").lower()
        hints = ("cannot post", "not found", "unknown url", "unknown endpoint", "unsupported", "no route")
        return status_code in {400, 404, 405} and any(hint in normalized for hint in hints)

    @staticmethod
    def _exception_status_code(exc: Exception) -> int | None:
        for value in (
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _exception_message(exc: Exception) -> str:
        detail = getattr(exc, "detail", None)
        if detail is not None:
            return ProxyService._error_message_for_log(detail)
        response = getattr(exc, "response", None)
        response_text = getattr(response, "text", None)
        if response_text:
            text = str(response_text)
            if len(text) > 1200:
                text = text[:1200] + "...[truncated]"
            return f"{exc}\n{text}"
        return str(exc)

    @staticmethod
    def _attach_probe_raw_provider_response(
        result: dict[str, Any],
        *,
        response: Any = None,
        output_text: str | None = None,
        stream_chunk: bytes | str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        stream_chunks: list[str] | None = None
        if stream_chunk is not None:
            if isinstance(stream_chunk, bytes):
                stream_chunks = [stream_chunk.decode("utf-8", errors="replace")]
            else:
                stream_chunks = [str(stream_chunk)]
        return ContentGuardProbeService.attach_raw_provider_response(
            result,
            response=response,
            output_text=output_text,
            stream_chunks=stream_chunks,
            note=note,
        )

    @staticmethod
    def _protocol_type_from_supports(supports_chat: bool, supports_responses: bool, fallback: str) -> str:
        if supports_chat and supports_responses:
            return "both"
        if supports_chat:
            return "chat_completions"
        if supports_responses:
            return "responses"
        return fallback or "responses"

    @staticmethod
    def _native_protocol_endpoint_label(protocol_type: str) -> str:
        if protocol_type == "gemini":
            return "Gemini generateContent"
        if protocol_type == "claude_messages":
            return "Claude Messages"
        return protocol_type

    @staticmethod
    async def _probe_native_endpoint_protocol(
        provider: Any,
        provider_model: Any,
        *,
        model_name: str,
        protocol_type: str,
        setting: Any,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = HealthService._native_protocol_endpoint_label(protocol_type)
        endpoint_path = "/chat/completions"
        try:
            payload = HealthService._endpoint_protocol_payload(endpoint_path, model_name)
            prepared = ProxyService._prepare_upstream_request(
                provider,
                provider_model=provider_model,
                endpoint_path=endpoint_path,
                payload=payload,
            )
            response_payload, _ = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers={"Accept-Encoding": "identity"},
                requested_payload=payload,
                setting=setting,
                request_timeout_seconds=timeout_seconds,
            )
            success = HealthService._endpoint_protocol_response_is_valid("chat_completions", response_payload)
            return {
                "endpoint_path": prepared.request_path,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": success,
                "support_state": "supported" if success else "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "message": "原生协议检测通过" if success else "原生端点返回结构不符合适配预期，保留原协议配置",
            }
        except httpx.HTTPStatusError as exc:
            message = await HealthService._safe_error_text(exc.response)
            status_code = exc.response.status_code
            explicit_unsupported = HealthService._is_explicit_endpoint_unsupported(status_code, message)
            return {
                "endpoint_path": None,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": False,
                "support_state": "unsupported" if explicit_unsupported else "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": message,
            }
        except Exception as exc:
            return {
                "endpoint_path": None,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": False,
                "support_state": "unknown",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "message": str(exc),
            }

    @staticmethod
    async def _detect_endpoint_protocol_for_target(target: dict[str, Any]) -> dict[str, Any]:
        provider = HealthService._endpoint_protocol_provider(target)
        provider_model = HealthService._endpoint_protocol_model(target)
        previous_chat = bool(target.get("previous_supports_chat_completions"))
        previous_responses = bool(target.get("previous_supports_responses"))
        previous_protocol = str(target.get("previous_protocol_type") or provider_model.protocol_type or "responses")
        native_protocol = ProviderService.provider_or_model_native_protocol(provider, provider_model)
        if native_protocol:
            return {
                "provider_id": target.get("provider_id"),
                "provider_name": target.get("provider_name"),
                "provider_model_id": target.get("provider_model_id"),
                "model_name": target.get("model_name"),
                "status": "skipped",
                "message": "Gemini/Claude 原生协议模型无需端点协议检测，请使用对应原生可用性检测和可信检测",
                "skipped_reason": "native_protocol_without_endpoint_protocol_detection",
                "updated": False,
                "update_allowed": False,
                "supports_chat_completions": previous_chat,
                "supports_responses": previous_responses,
                "protocol_type": native_protocol,
                "protocol_label": ProviderService.provider_protocol_label(native_protocol),
                "endpoint_results": [],
                "latency_ms": 0,
            }
        limit_result = await HealthService._claim_probe_rate_limit_result(
            provider,
            provider_model,
            probe_type="endpoint_protocol",
        )
        if limit_result is not None and not getattr(limit_result, "allowed", False):
            return {
                "provider_id": target.get("provider_id"),
                "provider_name": target.get("provider_name"),
                "provider_model_id": target.get("provider_model_id"),
                "model_name": target.get("model_name"),
                "status": "rate_limited",
                "error_code": ProbeRateLimitService.ERROR_CODE,
                "message": getattr(limit_result, "reason", "探针频率限制"),
                "updated": False,
                "update_allowed": False,
                "supports_chat_completions": previous_chat,
                "supports_responses": previous_responses,
                "protocol_type": previous_protocol,
                "protocol_label": ProviderService.provider_protocol_label(previous_protocol),
                "endpoint_results": [],
                "latency_ms": 0,
            }
        setting = await ProxyService._get_setting_async()
        timeout_seconds = None
        try:
            timeout_ms = int(getattr(provider, "timeout_ms", 0) or 0)
            timeout_seconds = timeout_ms / 1000 if timeout_ms > 0 else None
        except Exception:
            timeout_seconds = None
        endpoint_results = [
            await HealthService._probe_endpoint_protocol(
                provider,
                model_name=str(target.get("model_name") or ""),
                endpoint_path="/chat/completions",
                setting=setting,
                timeout_seconds=timeout_seconds,
            ),
            await HealthService._probe_endpoint_protocol(
                provider,
                model_name=str(target.get("model_name") or ""),
                endpoint_path="/responses",
                setting=setting,
                timeout_seconds=timeout_seconds,
            ),
        ]
        chat_state = endpoint_results[0]["support_state"]
        responses_state = endpoint_results[1]["support_state"]
        supports_chat = True if chat_state == "supported" else (False if chat_state == "unsupported" else previous_chat)
        supports_responses = True if responses_state == "supported" else (False if responses_state == "unsupported" else previous_responses)
        has_confident_result = any(item["support_state"] in {"supported", "unsupported"} for item in endpoint_results)
        protocol_type = HealthService._protocol_type_from_supports(supports_chat, supports_responses, previous_protocol)
        update_allowed = bool(has_confident_result and (supports_chat or supports_responses))
        status_text = "passed" if update_allowed else "failed"
        message = "端点协议检测完成" if update_allowed else "端点协议检测未获得可沉淀结论，保留原协议配置"
        return {
            "provider_id": target.get("provider_id"),
            "provider_name": target.get("provider_name"),
            "provider_model_id": target.get("provider_model_id"),
            "model_name": target.get("model_name"),
            "status": status_text,
            "message": message,
            "updated": False,
            "update_allowed": update_allowed,
            "supports_chat_completions": supports_chat if update_allowed else previous_chat,
            "supports_responses": supports_responses if update_allowed else previous_responses,
            "protocol_type": protocol_type if update_allowed else previous_protocol,
            "protocol_label": ProviderService.provider_protocol_label(protocol_type if update_allowed else previous_protocol),
            "endpoint_results": endpoint_results,
            "latency_ms": sum(int(item.get("latency_ms") or 0) for item in endpoint_results),
        }

    @staticmethod
    async def _run_endpoint_protocol_detection_targets(
        db: Session,
        targets: list[dict[str, Any]],
        *,
        trigger_type: str,
    ) -> dict[str, Any]:
        manual_trigger = str(trigger_type or "").startswith("manual")
        async def run_one(target: dict[str, Any]) -> dict[str, Any]:
            provider = HealthService._endpoint_protocol_provider(target)
            if not manual_trigger and HealthService._provider_in_maintenance(provider):
                previous_protocol = str(target.get("previous_protocol_type") or "responses")
                return {
                    "provider_id": target.get("provider_id"),
                    "provider_name": target.get("provider_name"),
                    "provider_model_id": target.get("provider_model_id"),
                    "model_name": target.get("model_name"),
                    "status": "skipped",
                    "error_code": "provider_maintenance_mode",
                    "provider_maintenance_mode": True,
                    "message": HealthService._provider_maintenance_message(provider),
                    "updated": False,
                    "update_allowed": False,
                    "supports_chat_completions": bool(target.get("previous_supports_chat_completions")),
                    "supports_responses": bool(target.get("previous_supports_responses")),
                    "protocol_type": previous_protocol,
                    "protocol_label": ProviderService.provider_protocol_label(previous_protocol),
                    "endpoint_results": [],
                    "latency_ms": 0,
                }
            return await HealthService._detect_endpoint_protocol_for_target(target)

        provider_order = list(dict.fromkeys(target.get("provider_id") for target in targets))
        targets_by_provider = {
            provider_id: [target for target in targets if target.get("provider_id") == provider_id]
            for provider_id in provider_order
        }

        async def run_provider_targets(provider_id: Any) -> list[dict[str, Any]]:
            return await HealthService._gather_staggered_by_previous_completion(
                targets_by_provider.get(provider_id) or [],
                run_one,
            )

        grouped_model_results = await asyncio.gather(*(run_provider_targets(provider_id) for provider_id in provider_order))
        model_results = [item for group in grouped_model_results for item in group]
        updated_count = 0
        for result in model_results:
            if result.get("update_allowed"):
                provider_model = db.get(ProviderModel, result.get("provider_model_id"))
                if provider_model is not None:
                    provider_model.supports_chat_completions = bool(result.get("supports_chat_completions"))
                    provider_model.supports_responses = bool(result.get("supports_responses"))
                    provider_model.protocol_type = str(result.get("protocol_type") or provider_model.protocol_type)
                    result["updated"] = True
                    updated_count += 1
        if updated_count:
            db.commit()
            ProviderService.invalidate_provider_runtime_cache()
        maintenance_blocked_count = sum(1 for item in model_results if item.get("error_code") == "provider_maintenance_mode")
        provider_results: list[dict[str, Any]] = []
        for provider_id in dict.fromkeys(item.get("provider_id") for item in model_results):
            items = [item for item in model_results if item.get("provider_id") == provider_id]
            provider_results.append(
                {
                    "provider_id": provider_id,
                    "provider_name": items[0].get("provider_name") if items else None,
                    "total": len(items),
                    "updated_count": sum(1 for item in items if item.get("updated")),
                    "success": any(item.get("update_allowed") for item in items),
                    "model_results": items,
                }
            )
        return {
            "success": updated_count > 0 and maintenance_blocked_count < len(model_results),
            "trigger_type": trigger_type,
            "total": len(model_results),
            "updated_count": updated_count,
            "maintenance_blocked_count": maintenance_blocked_count,
            "provider_results": provider_results,
            "model_results": model_results,
        }

    @staticmethod
    def _endpoint_protocol_target_from_model(provider: Provider, provider_model: ProviderModel) -> dict[str, Any]:
        return {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "api_key": provider.api_key,
                "provider_type": provider.provider_type,
                "protocol_type": provider.protocol_type,
                "timeout_ms": provider.timeout_ms,
                "max_retries": provider.max_retries,
                "first_token_timeout_sec": provider.first_token_timeout_sec,
                "maintenance_mode_enabled": provider.maintenance_mode_enabled,
                "maintenance_window": provider.maintenance_window,
            },
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
            "previous_supports_chat_completions": bool(provider_model.supports_chat_completions),
            "previous_supports_responses": bool(provider_model.supports_responses),
            "previous_protocol_type": provider_model.protocol_type,
        }

    @staticmethod
    async def detect_endpoint_protocols_for_provider_ids(
        db: Session,
        *,
        provider_ids: list[int],
        trigger_type: str,
    ) -> dict[str, Any]:
        ids = [int(item) for item in provider_ids if int(item) > 0]
        if not ids:
            return {
                "success": False,
                "trigger_type": trigger_type,
                "total": 0,
                "updated_count": 0,
                "maintenance_blocked_count": 0,
                "provider_results": [],
                "model_results": [],
            }
        providers = list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .where(Provider.id.in_(ids))
            )
        )
        targets = [
            HealthService._endpoint_protocol_target_from_model(provider, provider_model)
            for provider in providers
            for provider_model in provider.provider_models
            if provider_model.enabled
        ]
        return await HealthService._run_endpoint_protocol_detection_targets(
            db,
            targets,
            trigger_type=trigger_type,
        )

    @staticmethod
    async def detect_endpoint_protocols_for_provider_model_ids(
        db: Session,
        *,
        targets: list[dict[str, Any]],
        trigger_type: str,
    ) -> dict[str, Any]:
        resolved_targets: list[dict[str, Any]] = []
        for target in targets:
            provider_model_id = target.get("provider_model_id")
            provider_id = target.get("provider_id")
            provider_model = db.get(ProviderModel, provider_model_id) if provider_model_id is not None else None
            provider = db.get(Provider, provider_id) if provider_id is not None else None
            if provider_model is None or provider is None:
                continue
            resolved_targets.append(HealthService._endpoint_protocol_target_from_model(provider, provider_model))
        return await HealthService._run_endpoint_protocol_detection_targets(
            db,
            resolved_targets,
            trigger_type=trigger_type,
        )

    @staticmethod
    def _build_probe_phase_groups(
        provider: Provider,
        *,
        stream_probe: bool = False,
        vision_probe: bool = False,
        phase_keys: set[str] | None = None,
        text_probe_max_tokens: int | None = None,
        capability_probe_max_tokens: int | None = None,
        selective_capability_probes: bool = False,
        route_metrics: dict[tuple[int | None, str | None], dict] | None = None,
        interactive_mode: bool = False,
        single_endpoint_mode: bool = False,
    ) -> list[dict[str, Any]]:
        text_max_tokens = text_probe_max_tokens or HealthService.SCHEDULED_TEXT_PROBE_MAX_TOKENS
        capability_max_tokens = capability_probe_max_tokens or (
            HealthService.INTERACTIVE_CAPABILITY_PROBE_MAX_TOKENS
            if interactive_mode
            else HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS
        )
        provider_supports_chat = ProviderService.provider_supports_chat_completions(provider)
        provider_supports_responses = ProviderService.provider_supports_responses(provider)

        if single_endpoint_mode:
            def selected_native_protocol(model: ProviderModel) -> str | None:
                return ProviderService.provider_or_model_native_protocol(provider, model)

            def selected_endpoint(model: ProviderModel) -> str | None:
                if selected_native_protocol(model):
                    return None
                return HealthService._interactive_endpoint_path(provider, model)

            def build_selected_text_payload(model: ProviderModel, *, stream: bool) -> dict[str, Any]:
                endpoint_path = selected_endpoint(model)
                if endpoint_path == "/chat/completions":
                    return HealthService._build_chat_probe_payload(
                        model,
                        vision_probe=vision_probe,
                        stream_probe=stream,
                        max_tokens=text_max_tokens,
                    )
                return HealthService._build_responses_probe_payload(
                    model,
                    vision_probe=vision_probe,
                    max_output_tokens=text_max_tokens,
                )

            phases = [
                {
                    "key": "text",
                    "label": "文字调用检查",
                    "targets": lambda model: bool(selected_native_protocol(model) or selected_endpoint(model) is not None),
                    "probes": [
                        {
                            "key": "selected_native_text_endpoint",
                            "targets": lambda model: selected_native_protocol(model) is not None,
                            "probe": lambda model: HealthService._probe_native_health_endpoint(
                                provider,
                                model,
                                max_tokens=text_max_tokens,
                                interactive_mode=interactive_mode,
                            ),
                        },
                        {
                            "key": "selected_text_endpoint",
                            "targets": lambda model: selected_endpoint(model) is not None,
                            "probe": lambda model: HealthService._probe_formal_endpoint(
                                provider,
                                model,
                                endpoint_path=selected_endpoint(model) or "/responses",
                                payload=build_selected_text_payload(model, stream=False),
                                interactive_mode=interactive_mode,
                            ),
                        }
                    ],
                },
                {
                    "key": "text_stream",
                    "label": "文字流式检查",
                    "targets": lambda model: bool(
                        model.supports_stream
                        and (selected_native_protocol(model) or selected_endpoint(model) is not None)
                    ),
                    "probes": [
                        {
                            "key": "selected_native_text_stream_endpoint",
                            "targets": lambda model: bool(model.supports_stream and selected_native_protocol(model)),
                            "probe": lambda model: HealthService._probe_native_health_stream_endpoint(
                                provider,
                                model,
                                max_tokens=text_max_tokens,
                                interactive_mode=interactive_mode,
                            ),
                        },
                        {
                            "key": "selected_text_stream_endpoint",
                            "targets": lambda model: bool(model.supports_stream and selected_endpoint(model) is not None),
                            "probe": lambda model: HealthService._probe_formal_stream_endpoint(
                                provider,
                                model,
                                endpoint_path=selected_endpoint(model) or "/responses",
                                payload=build_selected_text_payload(model, stream=True),
                                interactive_mode=interactive_mode,
                            ),
                        }
                    ],
                },
                {
                    "key": "tools",
                    "label": "工具调用检查",
                    "targets": lambda model: (
                        ProviderService.provider_model_supports_tools(model)
                        and selected_endpoint(model) is not None
                        and (
                            not selective_capability_probes
                            or HealthService._should_run_scheduled_capability_probe(
                                provider,
                                model,
                                capability="tools",
                                route_metrics=route_metrics,
                            )
                        )
                    ),
                    "probes": [
                        {
                            "key": "selected_tools_endpoint",
                            "targets": lambda model: selected_endpoint(model) is not None,
                            "probe": lambda model: HealthService._probe_native_tools_endpoint(
                                provider,
                                model,
                                endpoint_path=selected_endpoint(model) or "/responses",
                                max_tokens=capability_max_tokens,
                                interactive_mode=interactive_mode,
                            ),
                        }
                    ],
                },
                {
                    "key": "vision",
                    "label": "图像理解检查",
                    "targets": lambda model: (
                        bool(model.supports_vision)
                        and selected_endpoint(model) is not None
                        and (
                            not selective_capability_probes
                            or HealthService._should_run_scheduled_capability_probe(
                                provider,
                                model,
                                capability="vision",
                                route_metrics=route_metrics,
                            )
                        )
                    ),
                    "probes": [
                        {
                            "key": "selected_vision_endpoint",
                            "targets": lambda model: selected_endpoint(model) is not None,
                            "probe": lambda model: HealthService._probe_native_vision_endpoint(
                                provider,
                                model,
                                endpoint_path=selected_endpoint(model) or "/responses",
                                max_tokens=capability_max_tokens,
                                interactive_mode=interactive_mode,
                            ),
                        }
                    ],
                },
                {
                    "key": "image_generation",
                    "label": "图片生成检查",
                    "targets": lambda model: (
                        selected_endpoint(model) == "/responses"
                        and ProviderService.provider_model_supports_image_generation(model)
                        and (
                            not selective_capability_probes
                            or HealthService._should_run_scheduled_capability_probe(
                                provider,
                                model,
                                capability="image_generation",
                                route_metrics=route_metrics,
                            )
                        )
                    ),
                    "probes": [
                        {
                            "key": "selected_image_generation_endpoint",
                            "probe": lambda model: HealthService._probe_native_image_generation(
                                provider,
                                model,
                                interactive_mode=interactive_mode,
                            ),
                        }
                    ],
                },
            ]
            if phase_keys is None:
                return phases
            return [phase for phase in phases if phase["key"] in phase_keys]

        def _get_model_protocol(model: ProviderModel) -> str:
            """获取模型挂载级协议类型"""
            protocol_type = ProviderService.provider_model_protocol_type(model)
            if protocol_type in {"gemini", "claude_messages"}:
                return protocol_type
            if bool(getattr(model, "supports_responses", False)) and bool(getattr(model, "supports_chat_completions", False)):
                return "both"
            if bool(getattr(model, "supports_chat_completions", False)):
                return "chat_completions"
            return "responses"

        def _should_test_endpoint(model: ProviderModel, endpoint: str) -> bool:
            """判断是否应该测试指定端点"""
            protocol = _get_model_protocol(model)
            if protocol in {"gemini", "claude_messages"}:
                return endpoint == 'native'
            if protocol == 'both':
                if endpoint == 'responses':
                    return provider_supports_responses
                elif endpoint == 'chat':
                    return provider_supports_chat
            elif protocol == 'responses':
                return endpoint == 'responses' and provider_supports_responses
            elif protocol == 'chat_completions':
                return endpoint == 'chat' and provider_supports_chat
            return False

        phases = [
            {
                "key": "text",
                "label": "文字调用检查",
                "targets": lambda model: bool(
                    _should_test_endpoint(model, 'native') or _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses')
                ),
                "probes": [
                    {
                        "key": "native_text",
                        "targets": lambda model: _should_test_endpoint(model, 'native'),
                        "probe": lambda model: HealthService._probe_native_health_endpoint(
                            provider,
                            model,
                            max_tokens=text_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "chat_completions",
                        "targets": lambda model: _should_test_endpoint(model, 'chat'),
                        "probe": lambda model: HealthService._probe_formal_endpoint(
                            provider,
                            model,
                            endpoint_path="/chat/completions",
                            payload=HealthService._build_chat_probe_payload(
                                model,
                                vision_probe=vision_probe,
                                stream_probe=stream_probe,
                                max_tokens=text_max_tokens,
                            ),
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "responses",
                        "targets": lambda model: _should_test_endpoint(model, 'responses'),
                        "probe": lambda model: HealthService._probe_formal_endpoint(
                            provider,
                            model,
                            endpoint_path="/responses",
                            payload=HealthService._build_responses_probe_payload(
                                model,
                                vision_probe=vision_probe,
                                max_output_tokens=text_max_tokens,
                            ),
                            interactive_mode=interactive_mode,
                        ),
                    },
                ],
            },
            {
                "key": "text_stream",
                "label": "文字流式检查",
                "targets": lambda model: bool(
                    model.supports_stream
                    and (_should_test_endpoint(model, 'native') or _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                ),
                "probes": [
                    {
                        "key": "native_text_stream",
                        "targets": lambda model: _should_test_endpoint(model, 'native') and model.supports_stream,
                        "probe": lambda model: HealthService._probe_native_health_stream_endpoint(
                            provider,
                            model,
                            max_tokens=text_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "chat_completions_stream",
                        "targets": lambda model: _should_test_endpoint(model, 'chat') and model.supports_stream,
                        "probe": lambda model: HealthService._probe_formal_stream_endpoint(
                            provider,
                            model,
                            endpoint_path="/chat/completions",
                            payload=HealthService._build_chat_probe_payload(
                                model,
                                vision_probe=vision_probe,
                                stream_probe=True,
                                max_tokens=text_max_tokens,
                            ),
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "responses_stream",
                        "targets": lambda model: _should_test_endpoint(model, 'responses') and model.supports_stream,
                        "probe": lambda model: HealthService._probe_formal_stream_endpoint(
                            provider,
                            model,
                            endpoint_path="/responses",
                            payload=HealthService._build_responses_probe_payload(
                                model,
                                vision_probe=vision_probe,
                                max_output_tokens=text_max_tokens,
                            ),
                            interactive_mode=interactive_mode,
                        ),
                    },
                ],
            },
            {
                "key": "tools",
                "label": "工具调用检查",
                "targets": lambda model: (
                    ProviderService.provider_model_supports_tools(model)
                    and (_should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                    and (
                        not selective_capability_probes
                        or HealthService._should_run_scheduled_capability_probe(
                            provider,
                            model,
                            capability="tools",
                            route_metrics=route_metrics,
                        )
                    )
                ),
                "probes": [
                    {
                        "key": "tools_chat_completions",
                        "targets": lambda model: _should_test_endpoint(model, 'chat'),
                        "probe": lambda model: HealthService._probe_native_tools_endpoint(
                            provider,
                            model,
                            endpoint_path="/chat/completions",
                            max_tokens=capability_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "tools_responses",
                        "targets": lambda model: _should_test_endpoint(model, 'responses'),
                        "probe": lambda model: HealthService._probe_native_tools_endpoint(
                            provider,
                            model,
                            endpoint_path="/responses",
                            max_tokens=capability_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    }
                ],
            },
            {
                "key": "vision",
                "label": "图像理解检查",
                "targets": lambda model: (
                    bool(model.supports_vision)
                    and (_should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                    and (
                        not selective_capability_probes
                        or HealthService._should_run_scheduled_capability_probe(
                            provider,
                            model,
                            capability="vision",
                            route_metrics=route_metrics,
                        )
                    )
                ),
                "probes": [
                    {
                        "key": "vision_chat_completions",
                        "targets": lambda model: _should_test_endpoint(model, 'chat'),
                        "probe": lambda model: HealthService._probe_native_vision_endpoint(
                            provider,
                            model,
                            endpoint_path="/chat/completions",
                            max_tokens=capability_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    },
                    {
                        "key": "vision_responses",
                        "targets": lambda model: _should_test_endpoint(model, 'responses'),
                        "probe": lambda model: HealthService._probe_native_vision_endpoint(
                            provider,
                            model,
                            endpoint_path="/responses",
                            max_tokens=capability_max_tokens,
                            interactive_mode=interactive_mode,
                        ),
                    }
                ],
            },
            {
                "key": "image_generation",
                "label": "图片生成检查",
                "targets": lambda model: (
                    provider_supports_responses
                    and
                    ProviderService.provider_model_supports_image_generation(model)
                    and (
                        not selective_capability_probes
                        or HealthService._should_run_scheduled_capability_probe(
                            provider,
                            model,
                            capability="image_generation",
                            route_metrics=route_metrics,
                        )
                    )
                ),
                "probes": [
                    {
                        "key": "image_generation",
                        "probe": lambda model: HealthService._probe_native_image_generation(
                            provider,
                            model,
                            interactive_mode=interactive_mode,
                        ),
                    }
                ],
            },
        ]
        phases.extend(
            ContentGuardProbeService.build_health_phase_specs(
                provider,
                should_test_endpoint=_should_test_endpoint,
                include_json_probe=ContentTrustProbeService.json_probe_enabled(),
            )
        )
        if phase_keys is None:
            return phases
        return [phase for phase in phases if phase["key"] in phase_keys]

    @staticmethod
    async def _run_provider_phase_group(
        provider: Provider,
        provider_models: list[ProviderModel],
        phase_spec: dict[str, Any],
        *,
        phase_index: int,
        endpoint_results_by_model_id: dict[int, list[dict[str, Any]]],
        progress_callback: HealthProgressCallback | None = None,
        interactive_mode: bool = False,
    ) -> None:
        targets = [provider_model for provider_model in provider_models if phase_spec["targets"](provider_model)]
        if not targets:
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "stage_completed",
                    "phase_index": phase_index,
                    "phase_key": phase_spec["key"],
                    "phase_label": phase_spec["label"],
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "model_total": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "skipped": True,
                },
            )
            return
        await HealthService._emit_progress(
            progress_callback,
            {
                "event": "stage_started",
                "phase_index": phase_index,
                "phase_key": phase_spec["key"],
                "phase_label": phase_spec["label"],
                "provider_id": provider.id,
                "provider_name": provider.name,
                "model_total": len(targets),
            },
        )
        phase_results = await HealthService._run_phase_probe_specs_with_model_stagger(
            provider,
            targets,
            phase_spec["probes"],
            phase_spec=phase_spec,
            progress_callback=progress_callback,
            interactive_mode=interactive_mode,
        )
        results_by_model_id: dict[int, list[dict[str, Any]]] = {provider_model.id: [] for provider_model in targets}
        for provider_model, endpoint_result in phase_results:
            endpoint_result["capability_key"] = endpoint_result.get("capability_key") or phase_spec["key"]
            endpoint_results_by_model_id.setdefault(provider_model.id, []).append(endpoint_result)
            results_by_model_id.setdefault(provider_model.id, []).append(endpoint_result)
        success_count = sum(
            1
            for provider_model in targets
            if results_by_model_id.get(provider_model.id)
            and all(item.get("success") for item in results_by_model_id[provider_model.id])
        )
        await HealthService._emit_progress(
            progress_callback,
            {
                "event": "stage_completed",
                "phase_index": phase_index,
                "phase_key": phase_spec["key"],
                "phase_label": phase_spec["label"],
                "provider_id": provider.id,
                "provider_name": provider.name,
                "model_total": len(targets),
                "success_count": success_count,
                "failure_count": max(0, len(targets) - success_count),
                "skipped": False,
            },
        )

    @staticmethod
    async def _run_phase_probe_specs_with_model_stagger(
        provider: Provider,
        provider_models: list[ProviderModel],
        probe_specs: list[dict[str, Any]],
        *,
        phase_spec: dict[str, Any] | None = None,
        progress_callback: HealthProgressCallback | None = None,
        interactive_mode: bool = False,
    ) -> list[tuple[ProviderModel, dict[str, Any]]]:
        phase_targets = [
            (provider_model, probe_spec["probe"])
            for provider_model in provider_models
            for probe_spec in probe_specs
            if probe_spec.get("targets", lambda _model: True)(provider_model)
        ]
        if not phase_targets:
            return []
        parallelism = HealthService._determine_parallel_probe_limit(provider, len(phase_targets))
        semaphore = asyncio.Semaphore(parallelism)

        async def run_one_probe(
            provider_model: ProviderModel,
            probe_factory: Callable[[ProviderModel], Awaitable[dict[str, Any]]],
        ) -> tuple[ProviderModel, dict[str, Any]]:
            async with semaphore:
                endpoint_result = await HealthService._run_probe_with_interactive_timeout(
                    provider,
                    provider_model,
                    probe_factory,
                    interactive_mode=interactive_mode,
                )
                endpoint_result.setdefault("provider_model_id", provider_model.id)
                HealthService._apply_probe_error_policy(endpoint_result)
                return provider_model, endpoint_result

        probes_by_model_id: dict[int, list[Callable[[ProviderModel], Awaitable[dict[str, Any]]]]] = {}
        models_by_id: dict[int, ProviderModel] = {}
        for provider_model, probe_factory in phase_targets:
            models_by_id[provider_model.id] = provider_model
            probes_by_model_id.setdefault(provider_model.id, []).append(probe_factory)
        ordered_models = [
            provider_model
            for provider_model in provider_models
            if provider_model.id in probes_by_model_id
        ]

        async def run_model(provider_model: ProviderModel) -> list[tuple[ProviderModel, dict[str, Any]]]:
            probe_factories = probes_by_model_id.get(provider_model.id) or []
            phase_key = str((phase_spec or {}).get("key") or "")
            phase_label = str((phase_spec or {}).get("label") or "检查阶段")
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "model_started",
                    "phase_key": phase_key,
                    "phase_label": phase_label,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_model_id": provider_model.id,
                    "model_name": provider_model.model_name,
                },
            )
            results = list(
                await asyncio.gather(
                    *(run_one_probe(provider_model, probe_factory) for probe_factory in probe_factories)
                )
            )
            endpoint_results = [endpoint_result for _model, endpoint_result in results]
            model_result = HealthService._build_model_result(provider, provider_model, endpoint_results)
            await HealthService._emit_progress(
                progress_callback,
                {
                    "event": "model_completed",
                    "phase_key": phase_key,
                    "phase_label": phase_label,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_model_id": provider_model.id,
                    "model_name": provider_model.model_name,
                    "success": model_result.get("success"),
                    "provider_success": model_result.get("provider_success"),
                    "latency_ms": model_result.get("latency_ms"),
                    "message": model_result.get("message"),
                    "result": model_result,
                },
            )
            return results

        grouped_results = await HealthService._gather_staggered_by_previous_completion(ordered_models, run_model)
        return [item for group in grouped_results for item in group]

    @staticmethod
    async def _run_probe_with_interactive_timeout(
        provider: Provider,
        provider_model: ProviderModel,
        probe_factory: Callable[[ProviderModel], Awaitable[dict[str, Any]]],
        *,
        interactive_mode: bool,
    ) -> dict[str, Any]:
        if not interactive_mode:
            return await HealthService._probe_with_retry(lambda: probe_factory(provider_model), interactive_mode=False)
        started = time.perf_counter()
        timeout_seconds = HealthService._interactive_probe_timeout_seconds(provider)
        try:
            return await asyncio.wait_for(
                HealthService._probe_with_retry(
                    lambda: probe_factory(provider_model),
                    interactive_mode=True,
                    max_attempts=1,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {
                "endpoint_path": None,
                "endpoint_label": "即时探针",
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "timeout",
                "support_label": "即时测试超时",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": 504,
                "message": f"单项探针超过 {int(timeout_seconds)} 秒未返回，已停止等待",
                "trace": [],
                "retryable": True,
            }

    @staticmethod
    def _apply_probe_error_policy(result: dict[str, Any]) -> dict[str, Any]:
        return ProbeErrorPolicyService.annotate_result(result, probe_kind="health")

    @staticmethod
    def _finalize_provider_check(
        db: Session,
        provider: Provider,
        models_to_check: list[ProviderModel],
        model_results: list[dict[str, Any]],
        *,
        request_path: str = "/proxy-test",
        update_health_state: bool = True,
    ) -> dict[str, Any]:
        provider_id = provider.id
        provider_name = provider.name
        for provider_model, model_result in zip(models_to_check, model_results, strict=False):
            HealthService._persist_model_health_result(
                db,
                provider,
                provider_model,
                model_result,
                request_path=request_path,
                update_health_state=update_health_state,
            )
        provider = db.get(Provider, provider_id) or provider
        models_total = len(models_to_check)
        models_success = sum(1 for item in model_results if item.get("success"))
        models_failed = max(0, models_total - models_success)
        provider_success = any(item.get("provider_success", item.get("success")) for item in model_results) if model_results else False
        overall_success = provider_success and models_failed == 0
        latency_ms = max((int(item.get("latency_ms") or 0) for item in model_results), default=0)
        status_code = next((item.get("status_code") for item in model_results if not item.get("success")), None)
        if not models_to_check:
            message = "provider connectivity success, no models configured"
        elif not provider_success:
            message = "formal proxy probe failed for all models"
        elif models_failed:
            message = f"formal proxy probe success, models {models_success}/{models_total} healthy"
        else:
            message = f"formal proxy probe success, models {models_total}/{models_total} healthy"
        LogService.create_log(
            db,
            log_type="health_check_provider",
            provider_id=provider_id,
            provider_name=provider_name,
            request_path=request_path,
            success=provider_success,
            status_code=status_code,
            latency_ms=latency_ms,
            message=message,
            trace=HealthService._flatten_model_endpoint_traces(model_results),
            schedule_token_fill=False,
        )
        return {
            "success": overall_success,
            "provider_success": provider_success,
            "health_status": provider.health_status,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "models_total": models_total,
            "models_success": models_success,
            "models_failed": models_failed,
            "model_results": model_results,
        }

    @staticmethod
    async def _probe_with_retry(
        probe_coro_factory: Callable[[], Awaitable[dict[str, Any]]],
        *,
        interactive_mode: bool = False,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """执行探针，所有能力测试只执行一次，不重试"""
        last_result: dict[str, Any] | None = None
        # 所有测试只执行一次，不重试
        attempts = 1
        for attempt in range(1, attempts + 1):
            current_result = await probe_coro_factory()
            current_result["attempt_count"] = attempt
            current_result["retried"] = False
            return current_result
        if last_result is None:
            return {"success": False, "message": "可用性检测未返回结果", "attempt_count": 0, "retried": False}
        return last_result

    @staticmethod
    def _build_model_result(
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        success = bool(endpoint_results) and all(item["success"] for item in endpoint_results)
        provider_success = any(item["success"] for item in endpoint_results)
        adapted_success = any(item.get("support_mode") == "adapted" for item in endpoint_results)
        health_status = "healthy" if success and not adapted_success else ("degraded" if provider_success else "unhealthy")
        latency_ms = max((int(item.get("latency_ms") or 0) for item in endpoint_results), default=0)
        status_code = next((item.get("status_code") for item in endpoint_results if not item["success"]), 200 if success else None)
        message = "；".join(
            f"{item.get('support_label') or item['endpoint_label'] + ('成功' if item['success'] else '失败')}"
            + (f"（{item['message']}）" if item.get("message") else "")
            for item in endpoint_results
        ) or "当前未执行任何探针"
        return {
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
            "success": success,
            "provider_success": provider_success,
            "health_status": health_status,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "endpoint_results": endpoint_results,
        }

    @staticmethod
    def _persist_model_health_result(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        model_result: dict[str, Any],
        *,
        request_path: str = "/proxy-test",
        update_health_state: bool = True,
    ) -> None:
        success = bool(model_result.get("success"))
        message = model_result.get("message")
        endpoint_results = model_result.get("endpoint_results") or []
        content_guard_result = ContentGuardProbeService.first_content_guard_result(endpoint_results)
        availability_state_updated = False
        if update_health_state:
            availability_state_updated = HealthService._apply_model_health(
                db,
                provider,
                provider_model,
                health_status=str(model_result.get("health_status") or "unknown"),
                latency_ms=int(model_result.get("latency_ms") or 0),
                error_message=None if success else (str(message) if message is not None else None),
                endpoint_results=endpoint_results,
            )
        if content_guard_result is not None:
            ContentGuardProbeService.apply_content_probe_health(
                db,
                provider,
                provider_model,
                content_guard_result=content_guard_result,
                endpoint_results=endpoint_results,
                detection_source="automatic_health_probe",
            )
        LogService.create_log(
            db,
            log_type="health_check_model",
            provider_id=provider.id,
            provider_name=provider.name,
            resolved_provider_model_id=provider_model.id,
            model_name=provider_model.model_name,
            request_path=request_path,
            success=success,
            status_code=model_result.get("status_code"),
            latency_ms=int(model_result.get("latency_ms") or 0),
            message=str(message or ""),
            trace=HealthService._endpoint_results_to_trace(
                model_result.get("endpoint_results") or [],
                provider=provider,
                provider_model=provider_model,
            ),
            schedule_token_fill=False,
        )
        if update_health_state and availability_state_updated:
            ProviderHealthStateService.record_model_probe(
                provider,
                provider_model,
                success=success,
                health_status=str(model_result.get("health_status") or provider_model.health_status),
                circuit_state=str(provider_model.circuit_state or "closed"),
                latency_ms=int(model_result.get("latency_ms") or 0),
            )
        for endpoint_result in model_result.get("endpoint_results") or []:
            capability = endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label")
            if not HealthService._is_capability_probe_key(str(capability or "")):
                continue
            ProviderHealthStateService.record_capability_probe(
                provider,
                provider_model,
                capability=str(capability),
                success=bool(endpoint_result.get("success")),
                latency_ms=int(endpoint_result.get("latency_ms") or 0),
                status_code=endpoint_result.get("status_code"),
                message=str(endpoint_result.get("message") or ""),
                support_mode=str(endpoint_result.get("support_mode") or ""),
            )

    @staticmethod
    def _should_run_scheduled_text_probe(
        provider: Provider,
        provider_model: ProviderModel,
        active_model_keys: set[tuple[int | None, str | None]],
    ) -> bool:
        if provider_model.last_check_at is None:
            return True
        if provider_model.health_status in {"unknown", "degraded", "unhealthy"}:
            return True
        if provider_model.circuit_state in {"open", "half_open"}:
            return True
        return (provider.id, provider_model.model_name) in active_model_keys

    @staticmethod
    def _should_run_scheduled_capability_probe(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        capability: str | None = None,
        route_metrics: dict[tuple[int | None, str | None], dict] | None = None,
    ) -> bool:
        capabilities = [capability] if capability else HealthService._provider_model_capability_keys(provider_model)
        capabilities = [
            item
            for item in capabilities
            if HealthService._provider_model_supports_capability(provider_model, item)
        ]
        if not capabilities:
            return False
        if provider_model.last_check_at is None:
            return True
        if provider_model.health_status in {"unknown", "degraded", "unhealthy"}:
            return True
        if provider_model.circuit_state in {"open", "half_open"}:
            return True
        metric = (route_metrics or {}).get((provider.id, provider_model.model_name), {})
        if float(metric.get("failure_rate") or 0.0) >= 0.2:
            return True
        return any(
            not HealthService._has_recent_capability_probe_cache(provider, provider_model, capability_key)
            for capability_key in capabilities
        )

    @staticmethod
    def _should_run_scheduled_content_probe(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        setting: Any,
    ) -> bool:
        interval_seconds = max(300, int(getattr(setting, "content_guard_probe_interval_sec", 3600) or 3600))
        if provider_model.content_integrity_status == "blocked":
            return False
        last_probe_at = max(
            (
                item
                for item in (
                    provider_model.content_probe_last_passed_at,
                    provider_model.content_probe_last_failed_at,
                )
                if item is not None
            ),
            default=None,
        )
        if last_probe_at is None:
            return True
        return (now_beijing() - last_probe_at).total_seconds() >= interval_seconds

    @staticmethod
    def _capability_probe_cache_key(provider_id: int, provider_model_id: int, capability: str) -> str:
        return f"health-capability-probe:{provider_id}:{provider_model_id}:{capability}"

    @staticmethod
    def _interactive_endpoint_path(provider: Provider, provider_model: ProviderModel) -> str | None:
        if (
            ProviderService.provider_supports_chat_completions(provider)
            and bool(getattr(provider_model, "supports_chat_completions", False))
        ):
            return "/chat/completions"
        if (
            ProviderService.provider_supports_responses(provider)
            and bool(getattr(provider_model, "supports_responses", False))
        ):
            return "/responses"
        return None

    @staticmethod
    def _endpoint_capability_key(capability: str, endpoint_path: str | None) -> str:
        if capability not in {"tools", "vision"}:
            return capability
        if endpoint_path == "/chat/completions":
            return f"{capability}_chat_completions"
        if endpoint_path == "/responses":
            return f"{capability}_responses"
        return capability

    @staticmethod
    def _has_recent_capability_probe_cache(provider: Provider, provider_model: ProviderModel, capability: str) -> bool:
        payload = CacheService.get(HealthService._capability_probe_cache_key(provider.id, provider_model.id, capability))
        if not isinstance(payload, dict):
            return False
        checked_at = payload.get("checked_at")
        if isinstance(checked_at, str) and provider_model.updated_at is not None:
            try:
                if datetime.fromisoformat(checked_at) < provider_model.updated_at:
                    return False
            except ValueError:
                return False
        return True

    @staticmethod
    def _provider_model_capability_keys(provider_model: ProviderModel) -> list[str]:
        capabilities: list[str] = []
        if ProviderService.provider_model_supports_tools(provider_model):
            capabilities.append("tools")
        if provider_model.supports_vision:
            capabilities.append("vision")
        return capabilities

    @staticmethod
    def _provider_model_supports_capability(provider_model: ProviderModel, capability: str) -> bool:
        if capability == "tools":
            return ProviderService.provider_model_supports_tools(provider_model)
        if capability == "vision":
            return bool(provider_model.supports_vision)
        if capability == "image_generation":
            return ProviderService.provider_model_supports_image_generation(provider_model)
        return False

    @staticmethod
    def _cache_capability_probe_results(
        provider: Provider,
        models_to_check: list[ProviderModel],
        model_results: list[dict[str, Any]],
    ) -> None:
        checked_at = now_beijing().isoformat()
        for provider_model, model_result in zip(models_to_check, model_results, strict=False):
            for endpoint_result in model_result.get("endpoint_results") or []:
                capability = endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label")
                if not HealthService._is_capability_probe_key(str(capability or "")):
                    continue
                payload = {
                    "success": bool(endpoint_result.get("success")),
                    "checked_at": checked_at,
                    "latency_ms": int(endpoint_result.get("latency_ms") or 0),
                    "status_code": endpoint_result.get("status_code"),
                    "message": str(endpoint_result.get("message") or "")[:500],
                    "support_mode": endpoint_result.get("support_mode"),
                }
                CacheService.set(
                    HealthService._capability_probe_cache_key(provider.id, provider_model.id, capability),
                    payload,
                    ttl_seconds=HealthService.SCHEDULED_CAPABILITY_RESULT_TTL_SECONDS,
                )
                ProviderHealthStateService.record_capability_probe(
                    provider,
                    provider_model,
                    capability=str(capability),
                    success=bool(endpoint_result.get("success")),
                    latency_ms=int(endpoint_result.get("latency_ms") or 0),
                    status_code=endpoint_result.get("status_code"),
                    message=str(endpoint_result.get("message") or ""),
                    support_mode=str(endpoint_result.get("support_mode") or ""),
                )

    @staticmethod
    def _is_capability_probe_key(value: str) -> bool:
        return value in {
            "tools",
            "tools_chat_completions",
            "tools_responses",
            "vision",
            "vision_chat_completions",
            "vision_responses",
            "image_generation",
        }

    @staticmethod
    def _determine_parallel_probe_limit(provider: Provider, target_count: int) -> int:
        configured_limit = provider.max_active_requests or 0
        if configured_limit <= 0:
            return max(1, min(target_count, 8))
        capacity_limit = max(1, int(configured_limit * 0.2))
        return max(1, min(target_count, capacity_limit, HealthService.MAX_PARALLEL_MODEL_PROBES))

    @staticmethod
    async def _emit_progress(
        progress_callback: HealthProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if progress_callback is None:
            return
        maybe_awaitable = progress_callback(payload)
        if isinstance(maybe_awaitable, Awaitable):
            await maybe_awaitable

    @staticmethod
    def claim_manual_check_slot(scope_key: str, scope_label: str) -> None:
        cache_key = f"health-check-slot:{scope_key}"
        last_started = CacheService.get(cache_key)
        if isinstance(last_started, str):
            try:
                started_at = datetime.fromisoformat(last_started)
            except ValueError:
                started_at = None
            if started_at is not None:
                elapsed_seconds = (now_beijing() - started_at).total_seconds()
                if elapsed_seconds < HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC:
                    remaining = max(1, int(HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC - elapsed_seconds))
                    raise ValueError(f"{scope_label} 已有可用性检测任务运行中，请在 {remaining} 秒后重试")
        CacheService.set(
            cache_key,
            now_beijing().isoformat(),
            ttl_seconds=HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC,
        )

    @staticmethod
    def release_manual_check_slot(scope_key: str) -> None:
        CacheService.invalidate(f"health-check-slot:{scope_key}")

    @staticmethod
    def _native_probe_label(protocol_type: str, *, stream: bool = False) -> str:
        if protocol_type == "gemini":
            return "Gemini streamGenerateContent" if stream else "Gemini generateContent"
        if protocol_type == "claude_messages":
            return "Claude Messages stream" if stream else "Claude Messages"
        return protocol_type

    @staticmethod
    def _native_health_prepared_request(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        protocol_type: str,
        prompt: str,
        max_tokens: int,
        stream: bool = False,
    ) -> PreparedUpstreamRequest:
        model_name = ProviderService.provider_model_upstream_model_name(
            provider_model,
            include_provider_model_id=True,
        )
        return PreparedUpstreamRequest(
            request_path=NativeProtocolAdapter.request_path(
                protocol_type,
                model_name,
                stream=stream,
                endpoint_path_template=(
                    getattr(provider_model, "native_endpoint_path", None)
                    or getattr(provider, "native_endpoint_path", None)
                ),
            ),
            request_payload=NativeProtocolAdapter.native_text_payload(
                protocol_type,
                model_name=model_name,
                prompt=prompt,
                max_tokens=max_tokens,
                stream=stream,
            ),
            public_endpoint_path=f"/native/{protocol_type}",
            upstream_protocol_type=protocol_type,
            response_model_override=model_name,
        )

    @staticmethod
    async def _probe_native_health_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        max_tokens: int = 8,
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        protocol_type = ProviderService.provider_or_model_native_protocol(provider, provider_model)
        if not protocol_type:
            return {
                "endpoint_path": None,
                "endpoint_label": "原生可用性探针",
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "非原生协议模型",
                "latency_ms": 0,
                "status_code": None,
                "message": "该模型未配置 Gemini 或 Claude 原生协议",
                "trace": [],
            }
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        endpoint_label = HealthService._native_probe_label(protocol_type)
        prepared = HealthService._native_health_prepared_request(
            provider,
            provider_model,
            protocol_type=protocol_type,
            prompt="只输出 pong，不要解释。",
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            response, _ = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers={"Accept-Encoding": "identity"},
                requested_payload={"model": prepared.response_model_override or provider_model.model_name},
                setting=setting,
                request_timeout_seconds=(
                    HealthService._interactive_probe_timeout_seconds(provider)
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            guard_result = ContentGuardProbeService.inspect_probe_json_response(
                response,
                provider=provider,
                provider_model=provider_model,
                endpoint_path=prepared.request_path,
                request_payload=prepared.request_payload,
            )
            if guard_result.result != ContentGuardProbeService.RESULT_PASS:
                return HealthService._attach_probe_raw_provider_response(ContentGuardProbeService.probe_failure(
                    endpoint_path=prepared.request_path,
                    endpoint_label=endpoint_label,
                    support_label="原生可用性探针未通过",
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                ), response=response)
            output_text = ProxyService._extract_response_display_text(response, limit_bytes=160)
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": prepared.request_path,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": True,
                "native_success": True,
                "adapted_success": False,
                "support_mode": "native",
                "support_label": "原生可用性探针通过",
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": output_text or "ok",
                "trace": [{"result": "native_health_probe", "protocol_type": protocol_type, "endpoint": prepared.request_path}],
            }, response=response, output_text=output_text)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": prepared.request_path,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "probe_failed",
                "support_label": "原生可用性探针失败",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [{"result": "native_health_probe_failed", "protocol_type": protocol_type, "endpoint": prepared.request_path}],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)

    @staticmethod
    async def _probe_native_health_stream_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        max_tokens: int = 8,
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        protocol_type = ProviderService.provider_or_model_native_protocol(provider, provider_model)
        if not protocol_type:
            return await HealthService._probe_native_health_endpoint(
                provider,
                provider_model,
                max_tokens=max_tokens,
                interactive_mode=interactive_mode,
            )
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        endpoint_label = HealthService._native_probe_label(protocol_type, stream=True)
        prepared = HealthService._native_health_prepared_request(
            provider,
            provider_model,
            protocol_type=protocol_type,
            prompt="只输出 pong，不要解释。",
            max_tokens=max_tokens,
            stream=True,
        )
        limit_result = await HealthService._claim_probe_rate_limit_result(
            provider,
            provider_model,
            probe_type="health_stream",
        )
        if limit_result is not None and not getattr(limit_result, "allowed", False):
            return ProbeRateLimitService.rate_limited_probe_result(
                limit_result,
                endpoint_path=prepared.request_path,
                endpoint_label=endpoint_label,
                support_label="原生流式可用性探针已限频",
            )
        stream_context = None
        exc_type = exc_value = exc_traceback = None
        try:
            headers = ProxyService._build_upstream_headers(
                provider,
                prepared=prepared,
                extra_headers={"Accept-Encoding": "identity"},
            )
            stream_context = ProxyService._stream_prepared_request(
                provider,
                prepared=prepared,
                headers=headers,
                stream_connect_timeout_seconds=(
                    HealthService._interactive_stream_connect_timeout_seconds(provider)
                    if interactive_mode
                    else setting.stream_connect_timeout_seconds
                ),
            )
            response, _ = await stream_context.__aenter__()
            await ProxyService._raise_stream_response_for_status(response)
            timeout_policy = (
                StreamTimeoutPolicy(
                    first_token_timeout_seconds=HealthService._interactive_stream_first_token_timeout_seconds(provider),
                    idle_timeout_seconds=max(0, int(getattr(setting, "stream_idle_timeout_seconds", 0) or 0)),
                    max_duration_seconds=max(0, int(getattr(setting, "stream_max_duration_seconds", 0) or 0)),
                )
                if interactive_mode
                else ProxyService._build_stream_timeout_policy(provider=provider, setting=setting)
            )
            chunk = await ProxyService._read_next_stream_chunk(
                response.aiter_bytes().__aiter__(),
                first_chunk_latency_ms=None,
                stream_started=time.perf_counter(),
                timeout_policy=timeout_policy,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            guard_result = ContentGuardProbeService.inspect_probe_stream_chunk(chunk, endpoint_path=prepared.request_path)
            if guard_result.result != ContentGuardProbeService.RESULT_PASS:
                return HealthService._attach_probe_raw_provider_response(ContentGuardProbeService.probe_failure(
                    endpoint_path=prepared.request_path,
                    endpoint_label=endpoint_label,
                    support_label="原生流式可用性探针未通过",
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                ), stream_chunk=chunk, note="原生流式可用性探针首个数据块")
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": prepared.request_path,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": bool(chunk),
                "native_success": bool(chunk),
                "adapted_success": False,
                "support_mode": "native" if chunk else "unsupported",
                "support_label": "原生流式可用性探针通过" if chunk else "原生流式响应为空",
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": "已收到原生流式首个数据块" if chunk else "原生流式响应为空",
                "trace": [{"result": "native_stream_health_probe", "protocol_type": protocol_type, "endpoint": prepared.request_path}],
            }, stream_chunk=chunk, note="原生流式可用性探针首个数据块")
        except Exception as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": prepared.request_path,
                "endpoint_type": protocol_type,
                "protocol_type": protocol_type,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "probe_failed",
                "support_label": "原生流式可用性探针失败",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [{"result": "native_stream_health_probe_failed", "protocol_type": protocol_type, "endpoint": prepared.request_path}],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)
        finally:
            if stream_context is not None:
                await stream_context.__aexit__(exc_type, exc_value, exc_traceback)

    @staticmethod
    async def _probe_formal_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        endpoint_label = "chat/completions" if endpoint_path == "/chat/completions" else "responses"
        native_support_label = f"原生支持 {endpoint_label}"
        unsupported_label = f"不支持 {endpoint_label}"
        try:
            response, _, fallback_trace = await ProxyService._forward_json_with_endpoint_fallback(
                provider,
                provider_model,
                endpoint_path,
                payload,
                started=started,
                setting=setting,
                request_timeout_seconds=(
                    HealthService._interactive_probe_timeout_seconds(provider)
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            guard_result = ContentGuardProbeService.inspect_probe_json_response(
                response,
                provider=provider,
                provider_model=provider_model,
                endpoint_path=endpoint_path,
                request_payload=payload,
            )
            if guard_result.result != ContentGuardProbeService.RESULT_PASS:
                return HealthService._attach_probe_raw_provider_response(ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label=native_support_label,
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                ), response=response)
            output_text = ProxyService._extract_response_display_text(response, limit_bytes=160)
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": True,
                "native_success": True,
                "adapted_success": False,
                "support_mode": "native",
                "support_label": native_support_label,
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": output_text or "ok",
                "trace": fallback_trace,
            }, response=response, output_text=output_text)
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label=endpoint_label,
                unsupported_label=unsupported_label,
                status_code=exc.response.status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": support_label,
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=exc.response.status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)
        except requests.HTTPError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            response = exc.response
            message = response.text[:500] if response is not None else str(exc)
            status_code = response.status_code if response is not None else None
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label=endpoint_label,
                unsupported_label=unsupported_label,
                status_code=status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": support_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label=endpoint_label,
                unsupported_label=unsupported_label,
                status_code=status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": support_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)

    @staticmethod
    async def _probe_formal_stream_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        interactive_mode: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        endpoint_label = "chat/completions stream" if endpoint_path == "/chat/completions" else "responses stream"
        native_support_label = f"原生支持 {endpoint_label}"
        unsupported_label = f"不支持 {endpoint_label}"
        limit_result = await HealthService._claim_probe_rate_limit_result(
            provider,
            provider_model,
            probe_type="health_stream",
        )
        if limit_result is not None and not getattr(limit_result, "allowed", False):
            return ProbeRateLimitService.rate_limited_probe_result(
                limit_result,
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="流式可用性探针已限频",
            )
        stream_context = None
        exc_type = exc_value = exc_traceback = None
        try:
            stream_payload = dict(payload)
            stream_payload["stream"] = True
            response, _prepared, stream_context, fallback_trace = await ProxyService._open_stream_with_endpoint_fallback(
                provider,
                provider_model,
                endpoint_path,
                stream_payload,
                started=started,
                extra_headers={"Accept-Encoding": "identity"},
                stream_connect_timeout_seconds=(
                    HealthService._interactive_stream_connect_timeout_seconds(provider)
                    if interactive_mode
                    else setting.stream_connect_timeout_seconds
                ),
            )
            timeout_policy = (
                StreamTimeoutPolicy(
                    first_token_timeout_seconds=HealthService._interactive_stream_first_token_timeout_seconds(provider),
                    idle_timeout_seconds=max(0, int(getattr(setting, "stream_idle_timeout_seconds", 0) or 0)),
                    max_duration_seconds=max(0, int(getattr(setting, "stream_max_duration_seconds", 0) or 0)),
                )
                if interactive_mode
                else ProxyService._build_stream_timeout_policy(provider=provider, setting=setting)
            )
            chunk = await ProxyService._read_next_stream_chunk(
                response.aiter_bytes().__aiter__(),
                first_chunk_latency_ms=None,
                stream_started=time.perf_counter(),
                timeout_policy=timeout_policy,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            guard_result = ContentGuardProbeService.inspect_probe_stream_chunk(
                chunk,
                endpoint_path=endpoint_path,
            )
            if guard_result.result != ContentGuardProbeService.RESULT_PASS:
                return HealthService._attach_probe_raw_provider_response(ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label=native_support_label if chunk else unsupported_label,
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                ), stream_chunk=chunk, note="流式可用性探针首个数据块")
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": bool(chunk),
                "native_success": bool(chunk),
                "adapted_success": False,
                "support_mode": "native" if chunk else "unsupported",
                "support_label": native_support_label if chunk else unsupported_label,
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": "已收到流式首个数据块" if chunk else "流式响应为空",
                "trace": fallback_trace,
            }, stream_chunk=chunk, note="流式可用性探针首个数据块")
        except StopAsyncIteration as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": "上游流式响应未返回任何数据",
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=200,
                    message="上游流式响应未返回任何数据",
                    interactive_mode=interactive_mode,
                ),
            }, note="上游流式响应未返回任何数据")
        except httpx.HTTPStatusError as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label=endpoint_label,
                unsupported_label=unsupported_label,
                status_code=exc.response.status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": support_mode,
                "support_label": support_label,
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=exc.response.status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)
        except Exception as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = HealthService._exception_status_code(exc)
            message = HealthService._exception_message(exc)
            decompression_error = any(
                hint in message.lower()
                for hint in ("decompress", "incorrect header check", "content-encoding", "压缩")
            )
            support_mode, support_label = HealthService._probe_failure_support_state(
                endpoint_label=endpoint_label,
                unsupported_label=unsupported_label,
                status_code=status_code,
                message=message,
            )
            return HealthService._attach_probe_raw_provider_response({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unknown" if decompression_error else support_mode,
                "support_label": "压缩响应异常，端点支持状态待确认" if decompression_error else support_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }, response=message)
        finally:
            if stream_context is not None:
                await stream_context.__aexit__(exc_type, exc_value, exc_traceback)

    @staticmethod
    def _interactive_probe_timeout_seconds(provider: Provider) -> float:
        provider_timeout_seconds = max(0.0, float(getattr(provider, "timeout_ms", 0) or 0) / 1000)
        stream_connect_timeout = float(HealthService._interactive_stream_connect_timeout_seconds(provider))
        first_token_timeout = float(HealthService._interactive_stream_first_token_timeout_seconds(provider))
        timeout_seconds = max(
            float(HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS),
            provider_timeout_seconds,
            stream_connect_timeout + first_token_timeout,
        )
        timeout_seconds += float(HealthService.INTERACTIVE_PROBE_TIMEOUT_BUFFER_SECONDS)
        return min(timeout_seconds, float(HealthService.INTERACTIVE_PROBE_TIMEOUT_CAP_SECONDS))

    @staticmethod
    def _interactive_stream_connect_timeout_seconds(provider: Provider) -> int:
        return HealthService.INTERACTIVE_STREAM_CONNECT_TIMEOUT_SECONDS

    @staticmethod
    def _interactive_stream_first_token_timeout_seconds(provider: Provider) -> int:
        provider_first_token_timeout = int(getattr(provider, "first_token_timeout_sec", 0) or 0)
        if provider_first_token_timeout > 0:
            return provider_first_token_timeout
        return HealthService.INTERACTIVE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS

    @staticmethod
    def _probe_failure_support_state(
        *,
        endpoint_label: str,
        unsupported_label: str,
        status_code: int | None,
        message: str | None,
    ) -> tuple[str, str]:
        transient = False
        if status_code is not None:
            status_value = int(status_code)
            transient = status_value in {408, 429} or status_value >= 500
        normalized_message = (message or "").strip().lower()
        auth_or_quota_hints = (
            "invalid api key",
            "unauthorized",
            "forbidden",
            "permission",
            "insufficient balance",
            "insufficient_quota",
            "quota",
            "billing",
            "paid_model_required",
            "no credit",
            "余额",
            "额度",
            "鉴权",
            "无权限",
        )
        if (status_code is not None and int(status_code) in {401, 402, 403}) or any(
            hint in normalized_message for hint in auth_or_quota_hints
        ):
            return "unknown", f"{endpoint_label} 上游鉴权或额度异常，支持状态待确认"
        transient_hints = (
            "timeout",
            "timed out",
            "connection",
            "temporarily",
            "temporary",
            "service_unavailable",
            "rate limit",
            "rate_limit",
            "429",
            "503",
            "上游",
            "暂不可用",
            "稍后重试",
            "超时",
        )
        if any(hint in normalized_message for hint in transient_hints):
            transient = True
        if transient:
            return "unknown", f"{endpoint_label} 上游暂不可用，支持状态待确认"
        unsupported_hints = (
            "cannot post",
            "unknown url",
            "unknown endpoint",
            "unsupported",
            "does not support",
            "not support",
            "not implemented",
            "no route",
            "not found",
            "不存在该接口",
            "不支持",
        )
        if status_code is not None and int(status_code) in {400, 404, 405} and any(
            hint in normalized_message for hint in unsupported_hints
        ):
            return "unsupported", unsupported_label
        if status_code is not None and 400 <= int(status_code) < 500:
            return "unknown", f"{endpoint_label} 上游请求被拒绝，支持状态待确认"
        return "unsupported", unsupported_label

    @staticmethod
    def _should_retry_probe_result(result: dict[str, Any]) -> bool:
        retryable = result.get("retryable")
        if isinstance(retryable, bool):
            return retryable
        return True

    @staticmethod
    def _content_probe_exception_retryable(exc: Exception) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, requests.RequestException)):
            return True
        if isinstance(exc, (ValueError, TypeError, AttributeError, KeyError)):
            return False
        normalized_message = str(exc or "").strip().lower()
        non_retryable_hints = (
            "不存在",
            "停用",
            "不属于",
            "没有可检测",
            "未启用",
            "必须",
            "invalid",
            "unsupported",
            "does not support",
            "not implemented",
            "model_not_found",
            "bad_response_status_code",
        )
        if any(hint in normalized_message for hint in non_retryable_hints):
            return False
        transient_hints = ("timeout", "temporarily", "connection", "network", "rate_limit", "429", "503")
        return any(hint in normalized_message for hint in transient_hints)

    @staticmethod
    def _is_probe_failure_retryable(
        *,
        status_code: int | None,
        message: str | None,
        interactive_mode: bool,
    ) -> bool:
        if not interactive_mode:
            return True
        if status_code is not None:
            status_value = int(status_code)
            if status_value in {408, 429}:
                return True
            if 400 <= status_value < 500:
                return False
        normalized_message = (message or "").strip().lower()
        if not normalized_message:
            return True
        non_retryable_hints = (
            "not implemented",
            "bad_response_status_code",
            "invalid api key",
            "paid_model_required",
            "rate_limit_exceeded",
            "insufficient balance",
            "model_not_found",
            "does not support",
            "unsupported",
        )
        return not any(hint in normalized_message for hint in non_retryable_hints)

    @staticmethod
    def _endpoint_results_to_trace(endpoint_results: list[dict], *, provider: Provider, provider_model: ProviderModel) -> list[dict]:
        trace: list[dict] = []
        for item in endpoint_results:
            item_trace = item.get("trace")
            if isinstance(item_trace, list) and item_trace:
                trace.extend(item_trace)
                continue
            result = "success" if item.get("success") else "request_rejected"
            trace.append(
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    result,
                    int(item.get("latency_ms") or 0),
                    status_code=item.get("status_code"),
                    error=None if item.get("success") else item.get("message"),
                    extra={
                        "endpoint": item.get("endpoint_path"),
                        "support_mode": item.get("support_mode"),
                        "support_label": item.get("support_label"),
                    },
                )
            )
        return trace

    @staticmethod
    def _flatten_model_endpoint_traces(model_results: list[dict]) -> list[dict]:
        trace: list[dict] = []
        for model_result in model_results:
            for endpoint_result in model_result.get("endpoint_results") or []:
                endpoint_trace = endpoint_result.get("trace")
                if isinstance(endpoint_trace, list):
                    trace.extend(endpoint_trace)
        return trace

    @staticmethod
    def _build_chat_probe_payload(
        provider_model: ProviderModel,
        *,
        vision_probe: bool,
        stream_probe: bool,
        max_tokens: int = 16,
    ) -> dict[str, Any]:
        content: Any = "ping"
        if vision_probe and provider_model.supports_vision:
            content = [
                {"type": "text", "text": "ping"},
                {"type": "image_url", "image_url": {"url": VISION_TEST_IMAGE_URL}},
            ]
        payload = {
            "model": provider_model.model_name,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max(1, int(max_tokens)),
        }
        if stream_probe and provider_model.supports_stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def _build_chat_tool_probe_payload(provider_model: ProviderModel, *, max_tokens: int = 16) -> dict[str, Any]:
        return {
            "model": provider_model.model_name,
            "messages": [{"role": "user", "content": "调用 get_time 工具。"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_time",
                        "description": "返回当前时间。",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "get_time"}},
            "max_tokens": max(1, int(max_tokens)),
        }

    @staticmethod
    def _build_responses_tool_probe_payload(
        provider_model: ProviderModel,
        *,
        max_output_tokens: int = 16,
    ) -> dict[str, Any]:
        return {
            "model": provider_model.model_name,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "调用 get_time 工具。"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "get_time",
                    "description": "返回当前时间。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "required",
            "max_output_tokens": max(1, int(max_output_tokens)),
        }

    @staticmethod
    def _response_has_tool_call(response: dict[str, Any]) -> bool:
        choices = response.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") == "tool_calls":
                    return True
                message = choice.get("message")
                if isinstance(message, dict) and message.get("tool_calls"):
                    return True
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                item_type = item.get("type") if isinstance(item, dict) else None
                if isinstance(item_type, str) and item_type in {"function_call", "tool_call"}:
                    return True
        return False

    @staticmethod
    def _response_has_generated_image(response: dict[str, Any]) -> bool:
        return bool(ProxyService._extract_generated_images(response, limit_images=1))

    @staticmethod
    def _build_responses_probe_payload(
        provider_model: ProviderModel,
        *,
        vision_probe: bool,
        max_output_tokens: int = 16,
    ) -> dict[str, Any]:
        input_value: Any = "ping"
        if vision_probe and provider_model.supports_vision:
            input_value = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "ping"},
                        {"type": "input_image", "image_url": VISION_TEST_IMAGE_URL},
                    ],
                }
            ]
        return {
            "model": provider_model.model_name,
            "input": input_value,
            "max_output_tokens": max(1, int(max_output_tokens)),
        }

    @staticmethod
    def _build_responses_image_generation_probe_payload(provider_model: ProviderModel) -> dict[str, Any]:
        return {
            "model": provider_model.model_name,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "请生成一张最简单的链路检测图片，不要输出额外文字。",
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "type": "image_generation",
                    "model": ProxyService.LEGACY_IMAGE_DEFAULT_TOOL_MODEL,
                    "action": "generate",
                }
            ],
            "tool_choice": {"type": "image_generation"},
        }

    @staticmethod
    async def check_provider_connectivity(db: Session, provider: Provider, *, log_result: bool) -> dict:
        started = time.perf_counter()
        try:
            response = await UpstreamClientService.get_client().get(
                f"{provider.base_url}/models",
                headers=HealthService._auth_headers(provider),
                timeout=provider.timeout_ms / 1000,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            provider.last_check_at = now_beijing()
            provider.last_latency_ms = latency_ms
            success = response.status_code == 200
            if success:
                provider.failure_count = 0
                provider.success_count += 1
            else:
                provider.failure_count += 1
            db.commit()
            if log_result:
                LogService.create_log(
                    db,
                    log_type="health_check_provider",
                    provider_id=provider.id,
                    provider_name=provider.name,
                    request_path="/models",
                    success=success,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    message="provider connectivity success" if success else response.text[:500],
                    schedule_token_fill=False,
                )
            provider.circuit_state = "closed" if success else "open"
            if success:
                ProviderService.refresh_provider_state(provider)
                db.commit()
            else:
                provider.health_status = "unhealthy"
                db.commit()
            ProviderHealthStateService.record_provider_probe(
                provider,
                success=success,
                latency_ms=latency_ms,
                health_status=provider.health_status,
                circuit_state=provider.circuit_state,
            )
            return {
                "success": success,
                "latency_ms": latency_ms,
                "status_code": response.status_code,
                "message": None if success else response.text[:500],
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            provider.last_check_at = now_beijing()
            provider.last_latency_ms = latency_ms
            provider.failure_count += 1
            provider.health_status = "unhealthy"
            provider.circuit_state = "open"
            db.commit()
            if log_result:
                LogService.create_log(
                    db,
                    log_type="health_check_provider",
                    provider_id=provider.id,
                    provider_name=provider.name,
                    request_path="/models",
                    success=False,
                    latency_ms=latency_ms,
                    message=str(exc),
                    schedule_token_fill=False,
                )
            ProviderHealthStateService.record_provider_probe(
                provider,
                success=False,
                latency_ms=latency_ms,
                health_status=provider.health_status,
                circuit_state=provider.circuit_state,
            )
            return {"success": False, "latency_ms": latency_ms, "message": str(exc)}

    @staticmethod
    def _apply_model_health(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        health_status: str,
        latency_ms: int,
        error_message: str | None,
        endpoint_results: list[dict[str, Any]] | None = None,
    ) -> bool:
        if (
            provider_model.last_error == ProviderHealthStateService.FIXED_SUCCESS_RESPONSE_ERROR_CODE
            and health_status in {"healthy", "degraded"}
            and not HealthService._fixed_success_recovery_probe_passed(endpoint_results or [])
        ):
            ProviderHealthStateService.record_runtime_metrics(
                provider,
                provider_model,
                {
                    "total_requests": 0,
                    "success_requests": 0,
                    "failed_requests": 0,
                    "upstream_failure_requests": 0,
                    "window_seconds": ProviderHealthStateService.ROUTE_METRIC_WINDOW_MINUTES * 60,
                    "decision": "fixed_success_recovery_probe_required",
                    "confidence": 1.0,
                },
                health_status="unhealthy",
                circuit_state="open",
                status_update_reason="fixed_success_recovery_probe_required",
            )
            return False
        provider_model.health_status = health_status
        provider_model.last_check_at = now_beijing()
        provider_model.last_latency_ms = latency_ms
        provider_model.last_error = error_message
        if health_status == "healthy":
            provider_model.failure_count = 0
            provider_model.success_count += 1
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
        elif health_status == "degraded":
            provider_model.failure_count = 0
            provider_model.success_count += 1
            provider_model.health_status = "degraded"
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
        else:
            provider_model.failure_count += 1
            if health_status == "unhealthy":
                provider_model.health_status = "unhealthy"
                provider_model.circuit_state = "open"
                provider_model.circuit_opened_at = now_beijing()
            elif provider.auto_circuit_break_enabled:
                threshold = ProviderService.get_effective_circuit_breaker_threshold(db, provider)
                if provider_model.circuit_state == "half_open" or provider_model.failure_count >= threshold:
                    provider_model.health_status = "unhealthy"
                    provider_model.circuit_state = "open"
                    provider_model.circuit_opened_at = now_beijing()
                else:
                    provider_model.health_status = "degraded"
                    provider_model.circuit_state = "closed"
                    provider_model.circuit_opened_at = None
            else:
                provider_model.health_status = "degraded"
                provider_model.circuit_state = "closed"
                provider_model.circuit_opened_at = None
        provider.last_check_at = provider_model.last_check_at
        provider.last_latency_ms = latency_ms
        ProviderService.refresh_provider_state(provider)
        return True

    @staticmethod
    def _fixed_success_recovery_probe_passed(endpoint_results: list[dict[str, Any]]) -> bool:
        for item in endpoint_results:
            if not isinstance(item, dict):
                continue
            if ProbeRateLimitService.is_rate_limited_result(item):
                return False
            probe_key = str(item.get("probe_key") or item.get("key") or "")
            capability_key = str(item.get("capability_key") or "")
            if probe_key == "fixed_answer" or capability_key == "content_fixed_answer":
                return bool(item.get("success"))
        return False

    @staticmethod
    def _mark_provider_unreachable(
        db: Session,
        provider: Provider,
        *,
        latency_ms: int,
        error_message: str | None,
    ) -> None:
        now = now_beijing()
        provider.last_check_at = now
        provider.last_latency_ms = latency_ms
        provider.health_status = "unhealthy"
        provider.circuit_state = "open"
        for provider_model in provider.provider_models:
            if not provider_model.enabled:
                continue
            provider_model.health_status = "unhealthy"
            provider_model.last_check_at = now
            provider_model.last_latency_ms = latency_ms
            provider_model.last_error = error_message
            provider_model.failure_count += 1
            provider_model.circuit_state = "open"
            provider_model.circuit_opened_at = now
        db.commit()

    @staticmethod
    def _auth_headers(provider: Provider) -> dict[str, str]:
        return {"Authorization": f"Bearer {provider.api_key}"}

    @staticmethod
    def _build_model_probe_payload(provider_model: ProviderModel, *, vision_probe: bool, stream: bool) -> dict[str, Any]:
        content: Any = "ping"
        if vision_probe and provider_model.supports_vision:
            content = [
                {"type": "text", "text": "ping"},
                {"type": "image_url", "image_url": {"url": VISION_TEST_IMAGE_URL}},
            ]
        return {
            "model": provider_model.model_name,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1,
            "stream": stream,
        }

    @staticmethod
    async def _safe_error_text(response: httpx.Response) -> str:
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                if len(body) + len(chunk) > 65536:
                    body.extend(chunk[: max(0, 65536 - len(body))])
                    break
                body.extend(chunk)
        except Exception:
            return f"upstream status {response.status_code}"
        return bytes(body).decode("utf-8", errors="ignore")[:500]
