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
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService


VISION_TEST_IMAGE_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
HealthProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class HealthService:
    MANUAL_CHECK_MIN_INTERVAL_SEC = 300
    PROBE_RETRY_MAX_ATTEMPTS = 2
    PROBE_RETRY_DELAY_SEC = 0.35
    MAX_PARALLEL_MODEL_PROBES = 6

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
        progress_callback: HealthProgressCallback | None = None,
    ) -> dict:
        models_to_check = [item for item in provider.provider_models if include_disabled_models or item.enabled]
        model_results = await HealthService._run_provider_model_checks(
            provider,
            models_to_check,
            progress_callback=progress_callback,
        )
        return HealthService._finalize_provider_check(db, provider, models_to_check, model_results)

    @staticmethod
    async def check_selected_providers(
        db: Session,
        *,
        provider_ids: list[int] | None = None,
        include_disabled_models: bool = True,
    ) -> list[dict]:
        providers = ProviderService.list_providers(db)
        if provider_ids:
            provider_map = {provider.id: provider for provider in providers}
            providers = [provider_map[provider_id] for provider_id in provider_ids if provider_id in provider_map]

        results: list[dict] = []
        for provider in providers:
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_enabled": provider.enabled,
                    **(
                        await HealthService.check_provider(
                            db,
                            provider,
                            include_disabled_models=include_disabled_models,
                        )
                    ),
                }
            )
        return results

    @staticmethod
    async def check_provider_model(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        stream_probe: bool = False,
        vision_probe: bool = False,
    ) -> dict:
        model_result = (
            await HealthService._run_provider_model_checks(
                provider,
                [provider_model],
                stream_probe=stream_probe,
                vision_probe=vision_probe,
            )
        )[0]
        HealthService._persist_model_health_result(db, provider, provider_model, model_result)
        return model_result

    @staticmethod
    async def _probe_native_tools(provider: Provider, provider_model: ProviderModel) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_path = "/chat/completions"
        endpoint_label = "tools"
        payload = HealthService._build_chat_tool_probe_payload(provider_model)
        setting = await ProxyService._get_setting_async()
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
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": has_tool_call,
                "native_success": has_tool_call,
                "adapted_success": False,
                "support_mode": "native" if has_tool_call else "unsupported",
                "support_label": "原生支持 tools" if has_tool_call else "不支持 tools",
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
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 tools",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
            }

    @staticmethod
    async def check_all(
        db: Session,
        *,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
        provider_models_map = {
            provider.id: [item for item in provider.provider_models if item.enabled]
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
        phase_count = len(HealthService._build_probe_phase_groups(providers[0])) if providers else 0
        for phase_index in range(phase_count):
            await asyncio.gather(
                *(
                    HealthService._run_provider_phase_group(
                        provider,
                        provider_models_map[provider.id],
                        HealthService._build_probe_phase_groups(provider)[phase_index],
                        phase_index=phase_index + 1,
                        endpoint_results_by_model_id=endpoint_results_by_provider_id[provider.id],
                        progress_callback=progress_callback,
                    )
                    for provider in providers
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
                [item for item in provider.provider_models if item.enabled],
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
        return results

    @staticmethod
    async def _run_provider_model_checks(
        provider: Provider,
        models_to_check: list[ProviderModel],
        *,
        stream_probe: bool = False,
        vision_probe: bool = False,
        progress_callback: HealthProgressCallback | None = None,
    ) -> list[dict]:
        endpoint_results_by_model_id: dict[int, list[dict[str, Any]]] = {
            provider_model.id: []
            for provider_model in models_to_check
        }
        for phase_index, phase_spec in enumerate(
            HealthService._build_probe_phase_groups(
                provider,
                stream_probe=stream_probe,
                vision_probe=vision_probe,
            ),
            start=1,
        ):
            await HealthService._run_provider_phase_group(
                provider,
                models_to_check,
                phase_spec,
                phase_index=phase_index,
                endpoint_results_by_model_id=endpoint_results_by_model_id,
                progress_callback=progress_callback,
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
    ) -> list[dict[str, Any]]:
        return [
            {
                "key": "text",
                "label": "文字调用检查",
                "targets": lambda model: True,
                "probes": [
                    {
                        "key": "chat_completions",
                        "probe": lambda model: HealthService._probe_formal_endpoint(
                            provider,
                            model,
                            endpoint_path="/chat/completions",
                            payload=HealthService._build_chat_probe_payload(
                                model,
                                vision_probe=vision_probe,
                                stream_probe=stream_probe,
                            ),
                        ),
                    },
                    {
                        "key": "responses",
                        "probe": lambda model: HealthService._probe_formal_endpoint(
                            provider,
                            model,
                            endpoint_path="/responses",
                            payload=HealthService._build_responses_probe_payload(
                                model,
                                vision_probe=vision_probe,
                            ),
                        ),
                    },
                ],
            },
            {
                "key": "tools",
                "label": "工具调用检查",
                "targets": lambda model: bool(model.supports_tools),
                "probes": [
                    {
                        "key": "tools",
                        "probe": lambda model: HealthService._probe_native_tools(provider, model),
                    }
                ],
            },
        ]

    @staticmethod
    async def _run_provider_phase_group(
        provider: Provider,
        provider_models: list[ProviderModel],
        phase_spec: dict[str, Any],
        *,
        phase_index: int,
        endpoint_results_by_model_id: dict[int, list[dict[str, Any]]],
        progress_callback: HealthProgressCallback | None = None,
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
        )
        results_by_model_id: dict[int, list[dict[str, Any]]] = {provider_model.id: [] for provider_model in targets}
        for provider_model, endpoint_result in phase_results:
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
    ) -> list[tuple[ProviderModel, dict[str, Any]]]:
        phase_targets = [
            (provider_model, probe_spec["probe"])
            for provider_model in provider_models
            for probe_spec in probe_specs
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
                endpoint_result = await HealthService._probe_with_retry(lambda: probe_factory(provider_model))
                return provider_model, endpoint_result

        return list(await asyncio.gather(*(run_single(provider_model, probe_factory) for provider_model, probe_factory in phase_targets)))

    @staticmethod
    def _finalize_provider_check(
        db: Session,
        provider: Provider,
        models_to_check: list[ProviderModel],
        model_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for provider_model, model_result in zip(models_to_check, model_results, strict=False):
            HealthService._persist_model_health_result(db, provider, provider_model, model_result)

        db.refresh(provider)
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
            provider_id=provider.id,
            provider_name=provider.name,
            request_path="/proxy-test",
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
    ) -> dict[str, Any]:
        last_result: dict[str, Any] | None = None
        for attempt in range(1, HealthService.PROBE_RETRY_MAX_ATTEMPTS + 1):
            current_result = await probe_coro_factory()
            current_result["attempt_count"] = attempt
            current_result["retried"] = attempt > 1
            if current_result.get("success"):
                if attempt > 1:
                    message = str(current_result.get("message") or "ok")
                    current_result["message"] = f"第 {attempt} 次探测成功；{message}"
                return current_result
            last_result = current_result
            if attempt < HealthService.PROBE_RETRY_MAX_ATTEMPTS:
                await asyncio.sleep(HealthService.PROBE_RETRY_DELAY_SEC)
        if last_result is None:
            return {"success": False, "message": "健康检查未返回结果", "attempt_count": 0, "retried": False}
        message = str(last_result.get("message") or "请求失败")
        if last_result.get("attempt_count", 1) > 1:
            last_result["message"] = f"已重试 1 次仍失败；{message}"
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
    ) -> None:
        success = bool(model_result.get("success"))
        message = model_result.get("message")
        HealthService._apply_model_health(
            db,
            provider,
            provider_model,
            health_status=str(model_result.get("health_status") or "unknown"),
            latency_ms=int(model_result.get("latency_ms") or 0),
            error_message=None if success else (str(message) if message is not None else None),
        )
        LogService.create_log(
            db,
            log_type="health_check_model",
            provider_id=provider.id,
            provider_name=provider.name,
            model_name=provider_model.model_name,
            request_path="/proxy-test",
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

    @staticmethod
    def _determine_parallel_probe_limit(provider: Provider, target_count: int) -> int:
        configured_limit = provider.max_active_requests or 0
        if configured_limit <= 0:
            configured_limit = 1
        return max(1, min(target_count, configured_limit, HealthService.MAX_PARALLEL_MODEL_PROBES))

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
    ) -> dict[str, Any]:
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        endpoint_label = "chat/completions" if endpoint_path == "/chat/completions" else "responses"
        native_support_label = f"原生支持 {endpoint_label}"
        adapted_support_label = f"通过适配支持 {endpoint_label}"
        unsupported_label = f"不支持 {endpoint_label}"
        try:
            response, _, fallback_trace = await ProxyService._forward_json_with_endpoint_fallback(
                provider,
                provider_model,
                endpoint_path,
                payload,
                started=started,
                setting=setting,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            output_text = ProxyService._extract_response_text(response, limit_bytes=160)
            adapted = any(item.get("result") == "endpoint_fallback_success" for item in fallback_trace)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": True,
                "native_success": not adapted,
                "adapted_success": adapted,
                "support_mode": "adapted" if adapted else "native",
                "support_label": adapted_support_label if adapted else native_support_label,
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
            }

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
    def _build_chat_probe_payload(provider_model: ProviderModel, *, vision_probe: bool, stream_probe: bool) -> dict[str, Any]:
        content: Any = "ping"
        if vision_probe and provider_model.supports_vision:
            content = [
                {"type": "text", "text": "ping"},
                {"type": "image_url", "image_url": {"url": VISION_TEST_IMAGE_URL}},
            ]
        payload = {
            "model": provider_model.model_name,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 16,
        }
        if stream_probe and provider_model.supports_stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def _build_chat_tool_probe_payload(provider_model: ProviderModel) -> dict[str, Any]:
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
            "max_tokens": 16,
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
    def _build_responses_probe_payload(provider_model: ProviderModel, *, vision_probe: bool) -> dict[str, Any]:
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
            "max_output_tokens": 16,
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
