from datetime import datetime
import time
from typing import Any

import httpx
import requests
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService


VISION_TEST_IMAGE_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


class HealthService:
    @staticmethod
    async def check_provider(
        db: Session,
        provider: Provider,
        *,
        include_disabled_models: bool = False,
    ) -> dict:
        models_to_check = [item for item in provider.provider_models if include_disabled_models or item.enabled]
        model_results: list[dict] = []
        for provider_model in models_to_check:
            model_results.append(await HealthService.check_provider_model(db, provider, provider_model))

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
        effective_vision_probe = vision_probe
        endpoint_results = []
        endpoint_results.append(
            await HealthService._probe_formal_endpoint(
                provider,
                provider_model,
                endpoint_path="/chat/completions",
                payload=HealthService._build_chat_probe_payload(provider_model, vision_probe=effective_vision_probe, stream_probe=stream_probe),
            )
        )
        endpoint_results.append(
            await HealthService._probe_formal_endpoint(
                provider,
                provider_model,
                endpoint_path="/responses",
                payload=HealthService._build_responses_probe_payload(provider_model, vision_probe=effective_vision_probe),
            )
        )
        success = all(item["success"] for item in endpoint_results)
        provider_success = any(item["success"] for item in endpoint_results)
        health_status = "healthy" if success else ("degraded" if provider_success else "unhealthy")
        latency_ms = max((item["latency_ms"] for item in endpoint_results), default=0)
        status_code = next((item.get("status_code") for item in endpoint_results if not item["success"]), 200 if success else None)
        message = "；".join(
            f"{item['endpoint_label']} {'成功' if item['success'] else '失败'}"
            + (f"（{item['message']}）" if item.get("message") else "")
            for item in endpoint_results
        )
        HealthService._apply_model_health(
            db,
            provider,
            provider_model,
            health_status=health_status,
            latency_ms=latency_ms,
            error_message=None if success else message,
        )
        LogService.create_log(
            db,
            log_type="health_check_model",
            provider_id=provider.id,
            provider_name=provider.name,
            model_name=provider_model.model_name,
            request_path="/proxy-test",
            success=success,
            status_code=status_code,
            latency_ms=latency_ms,
            message=message,
            schedule_token_fill=False,
        )
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
    async def check_all(db: Session) -> list[dict]:
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
        results: list[dict] = []
        for provider in providers:
            provider_result = await HealthService.check_provider(db, provider, include_disabled_models=False)
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "scope": "provider",
                    **provider_result,
                }
            )
            for provider_model in provider.provider_models:
                if not provider_model.enabled:
                    continue
                model_result = await HealthService.check_provider_model(db, provider, provider_model)
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
        return results

    @staticmethod
    async def _probe_formal_endpoint(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        endpoint_label = "chat/completions" if endpoint_path == "/chat/completions" else "responses"
        try:
            response, _ = await ProxyService._forward_json(provider, endpoint_path, payload)
            latency_ms = int((time.perf_counter() - started) * 1000)
            output_text = ProxyService._extract_response_text(response, limit_bytes=160)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": True,
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": output_text or "ok",
            }
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
            }
        except requests.HTTPError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            response = exc.response
            message = response.text[:500] if response is not None else str(exc)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "latency_ms": latency_ms,
                "status_code": response.status_code if response is not None else None,
                "message": message,
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "latency_ms": latency_ms,
                "status_code": None,
                "message": str(exc),
            }

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
            payload["stream"] = False
        return payload

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
            threshold = SettingService.get_or_create(db).circuit_breaker_threshold
            provider_model.failure_count += 1
            if provider_model.circuit_state == "half_open" or provider_model.failure_count >= threshold:
                provider_model.health_status = "unhealthy"
                provider_model.circuit_state = "open"
                provider_model.circuit_opened_at = datetime.utcnow()
            else:
                provider_model.health_status = "degraded"
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
