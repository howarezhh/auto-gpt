from datetime import datetime
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
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
        connectivity_result = await HealthService.check_provider_connectivity(db, provider, log_result=True)
        models_to_check = [item for item in provider.provider_models if include_disabled_models or item.enabled]
        model_results: list[dict] = []

        if not connectivity_result["success"]:
            HealthService._mark_provider_unreachable(
                db,
                provider,
                latency_ms=connectivity_result["latency_ms"],
                error_message=connectivity_result.get("message"),
            )
            for provider_model in models_to_check:
                LogService.create_log(
                    db,
                    log_type="health_check_model",
                    provider_id=provider.id,
                    provider_name=provider.name,
                    model_name=provider_model.model_name,
                    request_path="/chat/completions",
                    success=False,
                    status_code=connectivity_result.get("status_code"),
                    latency_ms=connectivity_result["latency_ms"],
                    message=connectivity_result.get("message"),
                    schedule_token_fill=False,
                )
                model_results.append(
                    {
                        "provider_model_id": provider_model.id,
                        "model_name": provider_model.model_name,
                        "success": False,
                        "health_status": "unhealthy",
                        "latency_ms": connectivity_result["latency_ms"],
                        "status_code": connectivity_result.get("status_code"),
                        "message": connectivity_result.get("message"),
                    }
                )
        else:
            for provider_model in models_to_check:
                model_results.append(await HealthService.check_provider_model(db, provider, provider_model))

        db.refresh(provider)
        models_total = len(models_to_check)
        models_success = sum(1 for item in model_results if item.get("success"))
        models_failed = max(0, models_total - models_success)
        overall_success = connectivity_result["success"] and models_failed == 0
        if not connectivity_result["success"]:
            message = connectivity_result.get("message") or "provider connectivity failed"
        elif not models_to_check:
            message = "provider connectivity success, no models configured"
        elif models_failed:
            message = f"provider connectivity success, models {models_success}/{models_total} healthy"
        else:
            message = f"provider connectivity success, models {models_total}/{models_total} healthy"
        return {
            "success": overall_success,
            "provider_success": connectivity_result["success"],
            "health_status": provider.health_status,
            "latency_ms": connectivity_result["latency_ms"],
            "status_code": connectivity_result.get("status_code"),
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
        started = time.perf_counter()
        try:
            client = UpstreamClientService.get_client()
            basic_response = await client.post(
                f"{provider.base_url}/chat/completions",
                headers=HealthService._auth_headers(provider),
                json=HealthService._build_model_probe_payload(provider_model, vision_probe=False, stream=False),
                timeout=provider.timeout_ms / 1000,
            )
            basic_response.raise_for_status()

            health_status = "healthy"
            message = "model probe success"

            if vision_probe and provider_model.supports_vision:
                vision_response = await client.post(
                    f"{provider.base_url}/chat/completions",
                    headers=HealthService._auth_headers(provider),
                    json=HealthService._build_model_probe_payload(provider_model, vision_probe=True, stream=False),
                    timeout=provider.timeout_ms / 1000,
                )
                vision_response.raise_for_status()

            if stream_probe and provider_model.supports_stream:
                async with client.stream(
                    "POST",
                    f"{provider.base_url}/chat/completions",
                    headers=HealthService._auth_headers(provider),
                    json=HealthService._build_model_probe_payload(provider_model, vision_probe=False, stream=True),
                    timeout=provider.timeout_ms / 1000,
                ) as stream_response:
                    stream_response.raise_for_status()
                    first_chunk = None
                    async for chunk in stream_response.aiter_bytes():
                        if chunk:
                            first_chunk = chunk
                            break
                    if not first_chunk:
                        health_status = "degraded"
                        message = "stream probe opened but no chunk received"

            latency_ms = int((time.perf_counter() - started) * 1000)
            HealthService._apply_model_health(
                db,
                provider,
                provider_model,
                health_status=health_status,
                latency_ms=latency_ms,
                error_message=None if health_status == "healthy" else message,
            )
            LogService.create_log(
                db,
                log_type="health_check_model",
                provider_id=provider.id,
                provider_name=provider.name,
                model_name=provider_model.model_name,
                request_path="/chat/completions",
                success=health_status == "healthy",
                status_code=200,
                latency_ms=latency_ms,
                message=message,
            )
            return {
                "success": health_status == "healthy",
                "health_status": health_status,
                "latency_ms": latency_ms,
                "status_code": 200,
                "message": message,
            }
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await HealthService._safe_error_text(exc.response)
            HealthService._apply_model_health(
                db,
                provider,
                provider_model,
                health_status="unhealthy",
                latency_ms=latency_ms,
                error_message=message,
            )
            LogService.create_log(
                db,
                log_type="health_check_model",
                provider_id=provider.id,
                provider_name=provider.name,
                model_name=provider_model.model_name,
                request_path="/chat/completions",
                success=False,
                status_code=exc.response.status_code,
                latency_ms=latency_ms,
                message=message,
            )
            return {
                "success": False,
                "health_status": "unhealthy",
                "latency_ms": latency_ms,
                "status_code": exc.response.status_code,
                "message": message,
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = str(exc)
            HealthService._apply_model_health(
                db,
                provider,
                provider_model,
                health_status="unhealthy",
                latency_ms=latency_ms,
                error_message=message,
            )
            LogService.create_log(
                db,
                log_type="health_check_model",
                provider_id=provider.id,
                provider_name=provider.name,
                model_name=provider_model.model_name,
                request_path="/chat/completions",
                success=False,
                latency_ms=latency_ms,
                message=message,
            )
            return {
                "success": False,
                "health_status": "unhealthy",
                "latency_ms": latency_ms,
                "message": message,
            }

    @staticmethod
    async def check_all(db: Session) -> list[dict]:
        providers = [provider for provider in ProviderService.list_providers(db) if provider.enabled]
        results: list[dict] = []
        for provider in providers:
            provider_result = await HealthService.check_provider_connectivity(db, provider, log_result=True)
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "scope": "provider",
                    **provider_result,
                }
            )
            if not provider_result["success"]:
                HealthService._mark_provider_unreachable(
                    db,
                    provider,
                    latency_ms=provider_result["latency_ms"],
                    error_message=provider_result.get("message"),
                )
                for provider_model in provider.provider_models:
                    if not provider_model.enabled:
                        continue
                    LogService.create_log(
                        db,
                        log_type="health_check_model",
                        provider_id=provider.id,
                        provider_name=provider.name,
                        model_name=provider_model.model_name,
                        request_path="/chat/completions",
                        success=False,
                        status_code=provider_result.get("status_code"),
                        latency_ms=provider_result["latency_ms"],
                        message=provider_result.get("message"),
                    )
                    results.append(
                        {
                            "provider_id": provider.id,
                            "provider_name": provider.name,
                            "provider_model_id": provider_model.id,
                            "model_name": provider_model.model_name,
                            "scope": "model",
                            "success": False,
                            "health_status": "unhealthy",
                            "latency_ms": provider_result["latency_ms"],
                            "status_code": provider_result.get("status_code"),
                            "message": provider_result.get("message"),
                        }
                    )
                continue

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
