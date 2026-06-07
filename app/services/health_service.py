import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import time
from typing import Any

import httpx
import requests
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.cache_service import CacheService
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.log_service import LogService
from app.logging.adapters.health_adapter import HealthLogRecorder
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService, StreamTimeoutPolicy
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService


VISION_TEST_IMAGE_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
HealthProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class HealthService:
    MANUAL_CHECK_MIN_INTERVAL_SEC = 300
    PROBE_RETRY_MAX_ATTEMPTS = 2
    PROBE_RETRY_DELAY_SEC = 0.35
    MAX_PARALLEL_MODEL_PROBES = 16
    INTERACTIVE_TEXT_PROBE_PHASE_KEYS = frozenset({"text_stream"})
    INTERACTIVE_TEXT_PROBE_MAX_TOKENS = 1
    INTERACTIVE_CAPABILITY_PROBE_MAX_TOKENS = 1
    INTERACTIVE_PROBE_TIMEOUT_SECONDS = 8.0
    INTERACTIVE_STREAM_CONNECT_TIMEOUT_SECONDS = 4
    INTERACTIVE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS = 4
    SCHEDULED_ACTIVE_MODEL_WINDOW_MINUTES = 30
    SCHEDULED_TEXT_PROBE_MAX_TOKENS = 4
    SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS = 8
    SCHEDULED_CAPABILITY_RESULT_TTL_SECONDS = 60 * 30
    CONTENT_GUARD_PROBE_PHASE_KEYS = ContentGuardProbeService.PROBE_PHASE_KEYS

    @staticmethod
    def cached_provider_status_summary(db: Session) -> dict:
        setting = SettingService.get_or_create(db)
        cache_key = "provider-status-summary"
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached
        providers = ProviderService.list_providers(db)
        payload = {
            "provider_count": len(providers),
            "healthy_provider_count": len([item for item in providers if item.health_status == "healthy"]),
            "degraded_provider_count": len([item for item in providers if item.health_status == "degraded"]),
            "unhealthy_provider_count": len([item for item in providers if item.health_status == "unhealthy"]),
            "open_circuit_provider_count": len([item for item in providers if item.circuit_state == "open"]),
        }
        return CacheService.set(cache_key, payload, ttl_seconds=max(0, int(setting.provider_status_cache_ttl_sec)))

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
        if interactive_mode:
            content_result = (
                await HealthService._run_provider_model_checks(
                    provider,
                    [provider_model],
                    phase_keys=ContentGuardProbeService.PROBE_PHASE_KEYS,
                    text_probe_max_tokens=text_probe_max_tokens,
                    capability_probe_max_tokens=capability_probe_max_tokens,
                    interactive_mode=interactive_mode,
                    parallel_phases=parallel_phases,
                    single_endpoint_mode=False,
                )
            )[0]
            HealthService._persist_model_health_result(
                db,
                provider,
                provider_model,
                content_result,
                request_path="/content-integrity-test",
                update_health_state=False,
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
                    return {
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
                    }
                last_error = {
                    "endpoint_path": endpoint_path,
                    "status_code": 200,
                    "message": "未返回工具调用",
                }
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                detail = getattr(exc, "detail", None)
                message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
                last_error = {
                    "endpoint_path": endpoint_path,
                    "status_code": status_code,
                    "message": message,
                }
                continue
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "endpoint_path": last_error.get("endpoint_path") if last_error else None,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "unsupported",
            "support_label": "不支持 tools",
            "latency_ms": latency_ms,
            "status_code": last_error.get("status_code") if last_error else None,
            "message": last_error.get("message") if last_error else "工具调用探测失败",
            "trace": [],
        }

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
                "message": "提供商或模型未启用该端点原生工具调用探测",
                "trace": [],
            }
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
                    HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            has_tool_call = HealthService._response_has_tool_call(response)
            return {
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
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "capability_key": capability_key,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": f"不支持 {support_label}",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
            }

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
                    HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS
                    if interactive_mode
                    else None
                ),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            has_generated_image = HealthService._response_has_generated_image(response)
            return {
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
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            return {
                "endpoint_path": "/responses",
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 image_generation",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
            }

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
        return {
            "endpoint_path": last_error.get("endpoint_path") if last_error else None,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "unsupported",
            "support_label": "不支持 vision",
            "latency_ms": latency_ms,
            "status_code": last_error.get("status_code") if last_error else None,
            "message": last_error.get("message") if last_error else "图像理解探测失败",
            "trace": last_error.get("trace") if last_error else [],
        }

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
        return {
            **result,
            "endpoint_label": endpoint_label,
            "capability_key": capability_key,
            "support_label": f"原生支持 {support_label}" if result.get("success") else f"不支持 {support_label}",
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
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
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
            await asyncio.gather(
                *(
                    HealthService._run_provider_phase_group(
                        provider,
                        provider_models_map[provider.id],
                        phase_groups_by_provider[provider.id][phase_index],
                        phase_index=phase_index + 1,
                        endpoint_results_by_model_id=endpoint_results_by_provider_id[provider.id],
                        progress_callback=progress_callback,
                        interactive_mode=interactive_mode,
                    )
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
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
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
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
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
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
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
        run = HealthLogRecorder.start_run(
            db,
            trigger_type="manual_batch" if progress_callback is not None else "scheduled_l3",
            scope_type="model",
            phase_keys=list(HealthService.CONTENT_GUARD_PROBE_PHASE_KEYS),
        )
        providers = [
            provider
            for provider in ProviderService.list_providers(db)
            if provider.enabled and bool(getattr(provider, "content_guard_enabled", True))
        ]
        results = await HealthService._check_scheduled_provider_models(
            db,
            providers,
            selector=lambda provider, provider_model: HealthService._should_run_scheduled_content_probe(
                provider,
                provider_model,
                setting=setting,
            ),
            phase_keys=set(HealthService.CONTENT_GUARD_PROBE_PHASE_KEYS),
            level="l3_content_integrity",
            text_probe_max_tokens=32,
            capability_probe_max_tokens=32,
            update_health_state=True,
            progress_callback=progress_callback,
        )
        HealthService._record_run_results(db, run_id=run.run_id, provider_results=results)
        HealthLogRecorder.finish_run(db, run_id=run.run_id, results=results)
        return results

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
        for provider_result in provider_results:
            provider_id = provider_result.get("provider_id")
            for model_result in provider_result.get("model_results") or []:
                endpoint_results = model_result.get("endpoint_results") or []
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
                        error_code=None if model_result.get("success") else "probe_failed",
                        capability_result=model_result,
                        auto_commit=False,
                    )
                    continue
                for endpoint_result in endpoint_results:
                    HealthLogRecorder.record_probe(
                        db,
                        run_id=run_id,
                        provider_id=provider_id,
                        provider_model_id=endpoint_result.get("provider_model_id"),
                        model_name=model_result.get("model_name"),
                        probe_type=str(endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label") or "probe"),
                        endpoint_path=endpoint_result.get("endpoint_path"),
                        protocol_type="chat_completions" if endpoint_result.get("endpoint_path") == "/chat/completions" else "responses",
                        success=bool(endpoint_result.get("success")),
                        status_code=endpoint_result.get("status_code"),
                        latency_ms=endpoint_result.get("latency_ms"),
                        error_code=None if endpoint_result.get("success") else str(endpoint_result.get("support_mode") or "probe_failed"),
                        capability_result=endpoint_result,
                        content_guard_result=endpoint_result.get("content_guard_result"),
                        auto_commit=False,
                    )
        db.commit()

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
        endpoint_results_by_model_id: dict[int, list[dict[str, Any]]] = {
            provider_model.id: []
            for provider_model in models_to_check
        }
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
        if interactive_mode and parallel_phases:
            phase_results = await asyncio.gather(
                *(
                    HealthService._run_provider_phase_group(
                        provider,
                        models_to_check,
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
        else:
            for phase_index, phase_spec in enumerate(phase_specs, start=1):
                await HealthService._run_provider_phase_group(
                    provider,
                    models_to_check,
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
            selected_endpoint = HealthService._interactive_endpoint_path

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
                    "targets": lambda model: selected_endpoint(model) is not None,
                    "probes": [
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
                    "targets": lambda model: bool(model.supports_stream and selected_endpoint(model) is not None),
                    "probes": [
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
            if bool(getattr(model, "supports_responses", False)) and bool(getattr(model, "supports_chat_completions", False)):
                return "both"
            if bool(getattr(model, "supports_chat_completions", False)):
                return "chat_completions"
            return "responses"

        def _should_test_endpoint(model: ProviderModel, endpoint: str) -> bool:
            """判断是否应该测试指定端点"""
            protocol = _get_model_protocol(model)
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
                    _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses')
                ),
                "probes": [
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
                    and (_should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                ),
                "probes": [
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
            {
                "key": "content_fixed_answer",
                "label": "固定答案完整性探针",
                "targets": lambda model: bool(
                    _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses')
                ),
                "probes": [
                    {
                        "key": "content_fixed_answer",
                        "probe": lambda model: ContentGuardProbeService.probe_fixed_answer(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_json",
                "label": "严格 JSON 完整性探针",
                "targets": lambda model: bool(
                    _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses')
                ),
                "probes": [
                    {
                        "key": "content_json",
                        "probe": lambda model: ContentGuardProbeService.probe_json(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_sse",
                "label": "SSE 完整性探针",
                "targets": lambda model: bool(
                    model.supports_stream
                    and (_should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                ),
                "probes": [
                    {
                        "key": "content_sse",
                        "probe": lambda model: ContentGuardProbeService.probe_sse(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_refusal",
                "label": "拒答完整性探针",
                "targets": lambda model: bool(
                    _should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses')
                ),
                "probes": [
                    {
                        "key": "content_refusal",
                        "probe": lambda model: ContentGuardProbeService.probe_refusal(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_tools",
                "label": "工具调用完整性探针",
                "targets": lambda model: (
                    ProviderService.provider_model_supports_tools(model)
                    and (_should_test_endpoint(model, 'chat') or _should_test_endpoint(model, 'responses'))
                ),
                "probes": [
                    {
                        "key": "content_tools",
                        "probe": lambda model: ContentGuardProbeService.probe_tools(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
        ]
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
        phase_results = await HealthService._run_phase_probe_specs_in_parallel(
            provider,
            targets,
            phase_spec["probes"],
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
    async def _run_phase_probe_specs_in_parallel(
        provider: Provider,
        provider_models: list[ProviderModel],
        probe_specs: list[dict[str, Any]],
        *,
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

        async def run_single(
            provider_model: ProviderModel,
            probe_factory: Callable[[ProviderModel], Awaitable[dict[str, Any]]],
        ) -> tuple[ProviderModel, dict[str, Any]]:
            async with semaphore:
                endpoint_result = await HealthService._run_probe_with_interactive_timeout(
                    provider_model,
                    probe_factory,
                    interactive_mode=interactive_mode,
                )
                return provider_model, endpoint_result

        return list(await asyncio.gather(*(run_single(provider_model, probe_factory) for provider_model, probe_factory in phase_targets)))

    @staticmethod
    async def _run_probe_with_interactive_timeout(
        provider_model: ProviderModel,
        probe_factory: Callable[[ProviderModel], Awaitable[dict[str, Any]]],
        *,
        interactive_mode: bool,
    ) -> dict[str, Any]:
        if not interactive_mode:
            return await HealthService._probe_with_retry(lambda: probe_factory(provider_model), interactive_mode=False)
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                HealthService._probe_with_retry(
                    lambda: probe_factory(provider_model),
                    interactive_mode=True,
                    max_attempts=1,
                ),
                timeout=HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS,
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
                "message": f"单项探针超过 {int(HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS)} 秒未返回，已停止等待",
                "trace": [],
                "retryable": True,
            }

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
            return {"success": False, "message": "健康检查未返回结果", "attempt_count": 0, "retried": False}
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
        if update_health_state:
            HealthService._apply_model_health(
                db,
                provider,
                provider_model,
                health_status=str(model_result.get("health_status") or "unknown"),
                latency_ms=int(model_result.get("latency_ms") or 0),
                error_message=None if success else (str(message) if message is not None else None),
            )
        if content_guard_result is not None:
            ContentGuardProbeService.apply_content_probe_health(
                db,
                provider,
                provider_model,
                content_guard_result=content_guard_result,
                endpoint_results=endpoint_results,
            )
        LogService.create_log(
            db,
            log_type="health_check_model",
            provider_id=provider.id,
            provider_name=provider.name,
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
        if update_health_state:
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
        if provider_model.content_probe_last_passed_at is None and provider_model.content_probe_last_failed_at is None:
            return True
        if provider_model.content_integrity_status in {"unknown", "degraded", "blocked"}:
            return True
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
        return (datetime.utcnow() - last_probe_at).total_seconds() >= interval_seconds

    @staticmethod
    def _capability_probe_cache_key(provider_id: int, provider_model_id: int, capability: str) -> str:
        return f"health-capability-probe:{provider_id}:{provider_model_id}:{capability}"

    @staticmethod
    def _interactive_endpoint_path(provider_model: ProviderModel) -> str | None:
        if bool(getattr(provider_model, "supports_responses", False)):
            return "/responses"
        if bool(getattr(provider_model, "supports_chat_completions", False)):
            return "/chat/completions"
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
        checked_at = datetime.utcnow().isoformat()
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
            "chat_completions",
            "responses",
            "chat_completions_stream",
            "responses_stream",
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
                elapsed_seconds = (datetime.utcnow() - started_at).total_seconds()
                if elapsed_seconds < HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC:
                    remaining = max(1, int(HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC - elapsed_seconds))
                    raise ValueError(f"{scope_label} 距离上次健康检查不足 5 分钟，请在 {remaining} 秒后重试")
        CacheService.set(
            cache_key,
            datetime.utcnow().isoformat(),
            ttl_seconds=HealthService.MANUAL_CHECK_MIN_INTERVAL_SEC,
        )

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
                    HealthService.INTERACTIVE_PROBE_TIMEOUT_SECONDS
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
                return ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label=native_support_label,
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                )
            output_text = ProxyService._extract_response_display_text(response, limit_bytes=160)
            return {
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
            }
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=exc.response.status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }
        except requests.HTTPError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            response = exc.response
            message = response.text[:500] if response is not None else str(exc)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": response.status_code if response is not None else None,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=response.status_code if response is not None else None,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }

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
                stream_connect_timeout_seconds=(
                    HealthService.INTERACTIVE_STREAM_CONNECT_TIMEOUT_SECONDS
                    if interactive_mode
                    else setting.stream_connect_timeout_seconds
                ),
            )
            timeout_policy = (
                StreamTimeoutPolicy(
                    first_token_timeout_seconds=HealthService.INTERACTIVE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS,
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
                return ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label=native_support_label if chunk else unsupported_label,
                    latency_ms=latency_ms,
                    status_code=200,
                    guard_result=guard_result,
                )
            return {
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
            }
        except StopAsyncIteration as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
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
            }
        except httpx.HTTPStatusError as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=exc.response.status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }
        except Exception as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": unsupported_label,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": HealthService._is_probe_failure_retryable(
                    status_code=status_code,
                    message=message,
                    interactive_mode=interactive_mode,
                ),
            }
        finally:
            if stream_context is not None:
                await stream_context.__aexit__(exc_type, exc_value, exc_traceback)

    @staticmethod
    def _should_retry_probe_result(result: dict[str, Any]) -> bool:
        retryable = result.get("retryable")
        if isinstance(retryable, bool):
            return retryable
        return True

    @staticmethod
    def _is_probe_failure_retryable(
        *,
        status_code: int | None,
        message: str | None,
        interactive_mode: bool,
    ) -> bool:
        if not interactive_mode:
            return True
        if status_code is not None and 400 <= int(status_code) < 500:
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
            provider.last_check_at = datetime.utcnow()
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
            provider.last_check_at = datetime.utcnow()
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
    ) -> None:
        provider_model.health_status = health_status
        provider_model.last_check_at = datetime.utcnow()
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
                provider_model.circuit_opened_at = datetime.utcnow()
            elif provider.auto_circuit_break_enabled:
                threshold = ProviderService.get_effective_circuit_breaker_threshold(db, provider)
                if provider_model.circuit_state == "half_open" or provider_model.failure_count >= threshold:
                    provider_model.health_status = "unhealthy"
                    provider_model.circuit_state = "open"
                    provider_model.circuit_opened_at = datetime.utcnow()
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
        db.commit()

    @staticmethod
    def _mark_provider_unreachable(
        db: Session,
        provider: Provider,
        *,
        latency_ms: int,
        error_message: str | None,
    ) -> None:
        now = datetime.utcnow()
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
            await response.aread()
        except Exception:
            return f"upstream status {response.status_code}"
        return response.text[:500]
