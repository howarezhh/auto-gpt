from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas.api_key import ApiKeyCreate, ApiKeyUpdate, RouteMode
from app.schemas.conversation import ConversationReplay, ConversationSummaryList
from app.schemas.log import (
    LogFilterOptionsResponse,
    LogListResponse,
    LogSummaryOut,
    MetricItem,
    MetricListResponse,
    MetricTimeSeriesItem,
    MetricTimeSeriesResponse,
    RequestLogOut,
)
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.api_key_service import ApiClientAuthError, ApiKeyService
from app.services.asset_service import AssetService
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService
from app.services.user_portal_service import UserPortalService
from app.utils.json_utils import safeJsonParse


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

SELF_TEST_IMAGE_MODES = {"none", "url", "upload", "generate"}
SELF_TEST_TEXT_PROMPT = "请只回复 pong"
SELF_TEST_IMAGE_PROMPT = "请确认你已看到这张测试图片，并用一句中文概括图片主体。"
SELF_TEST_IMAGE_GENERATION_PROMPT = "请生成一张用于链路检测的简单图片，不要输出文字说明。"


def require_user_html(request: Request, db: Session):
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        return RedirectResponse(UserAuthService.build_login_redirect_path(request), status_code=303)
    if user.role == USER_ROLE_ADMIN:
        return RedirectResponse("/", status_code=303)
    return user


def _pagination(total: int, page: int, page_size: int) -> dict:
    total_pages = max(1, ceil(total / page_size)) if page_size else 1
    normalized_page = min(max(1, page), total_pages)
    return {
        "page": normalized_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": normalized_page > 1,
        "has_next": normalized_page < total_pages,
        "prev_page": normalized_page - 1,
        "next_page": normalized_page + 1,
    }


def _redirect_user_profile(**updates) -> RedirectResponse:
    params = {key: value for key, value in updates.items() if value not in (None, "", [])}
    query = urlencode(params)
    return RedirectResponse(f"/user/profile?{query}" if query else "/user/profile", status_code=303)


def _redirect_user_api_keys(**updates) -> RedirectResponse:
    params = {key: value for key, value in updates.items() if value not in (None, "", [])}
    query = urlencode(params)
    return RedirectResponse(f"/user/api-keys?{query}" if query else "/user/api-keys", status_code=303)


def _parse_optional_int(value: str | None) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _parse_optional_int_form(value: str | None, *, field_label: str) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_label}必须为数字") from exc


def _build_user_api_key_payload(
    *,
    current_user_id: int,
    selectable_provider_ids: set[int],
    name: str,
    raw_api_key: str | None,
    remark: str | None,
    enabled: bool,
    route_mode: RouteMode,
    default_provider_id: str | None,
    manual_allow_fallback: bool,
    allowed_provider_ids: list[int],
) -> dict:
    parsed_default_provider_id = _parse_optional_int_form(default_provider_id, field_label="默认中转站")
    normalized_allowed_provider_ids: list[int] = []
    seen_provider_ids: set[int] = set()
    for provider_id in allowed_provider_ids:
        if provider_id in seen_provider_ids:
            continue
        seen_provider_ids.add(provider_id)
        normalized_allowed_provider_ids.append(provider_id)
    invalid_provider_ids = [provider_id for provider_id in normalized_allowed_provider_ids if provider_id not in selectable_provider_ids]
    if invalid_provider_ids:
        raise ValueError("仅允许选择管理员已启用的中转站")
    if parsed_default_provider_id is not None:
        if parsed_default_provider_id not in selectable_provider_ids:
            raise ValueError("默认中转站未启用，当前不可选择")
        if parsed_default_provider_id not in seen_provider_ids:
            normalized_allowed_provider_ids.append(parsed_default_provider_id)
    return {
        "name": name,
        "raw_api_key": raw_api_key,
        "remark": remark,
        "enabled": enabled,
        "route_mode": route_mode,
        "default_provider_id": parsed_default_provider_id,
        "owner_user_id": current_user_id,
        "manual_allow_fallback": manual_allow_fallback,
        "allowed_provider_ids": normalized_allowed_provider_ids,
    }


def _resolve_external_base_url(request: Request) -> str:
    configured = get_settings().normalized_external_base_url()
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _build_self_test_chat_content(message_text: str, image_input: dict | None) -> str | list[dict]:
    if image_input is None:
        return message_text
    return [
        {"type": "text", "text": message_text},
        {
            "type": "image_url",
            "image_url": {
                "url": image_input["url"],
                "detail": image_input["detail"],
            },
        },
    ]


def _build_self_test_responses_input(message_text: str, image_input: dict | None) -> str | list[dict]:
    if image_input is None:
        return message_text
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": message_text},
                {
                    "type": "input_image",
                    "image_url": image_input["url"],
                    "detail": image_input["detail"],
                },
            ],
        }
    ]


async def _read_self_test_upload_as_data_url(image_file: UploadFile) -> dict:
    content_type = (image_file.content_type or "").strip().lower()
    if content_type not in AssetService.IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPEG/WEBP/GIF 图片")
    content = await image_file.read(AssetService.MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    if len(content) > AssetService.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="图片大小不能超过 10 MB")
    encoded = base64.b64encode(content).decode("ascii")
    return {
        "url": f"data:{content_type};base64,{encoded}",
        "detail": "auto",
        "source": "upload",
        "filename": image_file.filename or "upload-image",
        "content_type": content_type,
        "file_size_bytes": len(content),
    }


async def _resolve_self_test_image_input(
    *,
    image_mode: str,
    image_detail: str,
    image_url: str | None,
    image_file: UploadFile | None,
) -> dict | None:
    normalized_mode = (image_mode or "none").strip().lower()
    if normalized_mode not in SELF_TEST_IMAGE_MODES:
        raise HTTPException(status_code=400, detail="图片模式不合法")
    normalized_detail = (image_detail or "auto").strip().lower() or "auto"
    if normalized_mode in {"none", "generate"}:
        return None
    if normalized_mode == "url":
        normalized_url = (image_url or "").strip()
        if not normalized_url:
            raise HTTPException(status_code=400, detail="请选择图片链接，或切换为不附带图片")
        return {
            "url": normalized_url,
            "detail": normalized_detail,
            "source": "url",
            "filename": None,
            "content_type": None,
            "file_size_bytes": None,
        }
    if image_file is None:
        raise HTTPException(status_code=400, detail="请先选择一张本地图片")
    payload = await _read_self_test_upload_as_data_url(image_file)
    payload["detail"] = normalized_detail
    return payload


async def _consume_self_test_stream(stream: AsyncIterator[bytes]) -> dict:
    event_buffer = bytearray()
    output_parts: list[str] = []
    event_preview: list[dict | str] = []
    generated_images: list[dict] = []
    seen_generated_image_urls: set[str] = set()

    async for chunk in stream:
        if not chunk:
            continue
        event_buffer.extend(chunk)
        while True:
            separator_index = event_buffer.find(b"\n\n")
            if separator_index < 0:
                break
            raw_event = bytes(event_buffer[:separator_index])
            del event_buffer[: separator_index + 2]
            event_text = raw_event.decode("utf-8", errors="ignore")
            for line in event_text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    if len(event_preview) < 20:
                        event_preview.append(data)
                    continue
                parsed = safeJsonParse(data)
                if len(event_preview) < 20:
                    event_preview.append(_build_self_test_safe_preview(parsed if parsed is not None else data))
                if isinstance(parsed, dict):
                    text = ProxyService._extract_response_display_text(parsed, limit_bytes=600)
                    if text and (not output_parts or output_parts[-1] != text):
                        output_parts.append(text)
                    for image in ProxyService._extract_generated_images(parsed, limit_images=4):
                        url = image.get("url")
                        if not isinstance(url, str) or not url or url in seen_generated_image_urls:
                            continue
                        seen_generated_image_urls.add(url)
                        generated_images.append(image)
    return {
        "output_text": "".join(output_parts) or None,
        "stream_event_preview": event_preview,
        "generated_images": generated_images,
    }


def _build_self_test_safe_preview(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= 240:
            return value
        return f"{value[:240]}...[truncated]"
    if not isinstance(value, dict):
        return value
    sanitized = ProxyService._sanitize_for_logging(value, mask_sensitive=True)
    generated_images = ProxyService._extract_generated_images(value, limit_images=8)
    if generated_images and isinstance(sanitized, dict):
        sanitized["_generated_image_summary"] = {
            "image_count": len(generated_images),
            "mime_types": sorted(
                {
                    str(item.get("mime_type") or "image/png")
                    for item in generated_images
                    if isinstance(item, dict)
                }
            ),
        }
    return sanitized


def _self_test_provider_name_from_trace(trace) -> str | None:
    if not isinstance(trace, list):
        return None
    for item in reversed(trace):
        if isinstance(item, dict) and isinstance(item.get("provider_name"), str) and item["provider_name"].strip():
            return item["provider_name"].strip()
    return None


async def _run_user_self_test_probe(
    *,
    db: Session,
    auth_context,
    model_name: str,
    endpoint_path: str,
    stream: bool,
    image_input: dict | None,
    image_generation: bool = False,
) -> dict:
    normalized_endpoint_path = endpoint_path if endpoint_path == "/responses" else "/chat/completions"
    has_image = image_input is not None
    has_image_generation = image_generation and normalized_endpoint_path == "/responses"
    payload = {
        "model": model_name,
        "stream": stream,
    }
    prompt_text = SELF_TEST_IMAGE_PROMPT if has_image else SELF_TEST_TEXT_PROMPT
    if has_image_generation:
        prompt_text = SELF_TEST_IMAGE_GENERATION_PROMPT
    if normalized_endpoint_path == "/responses":
        payload["input"] = _build_self_test_responses_input(prompt_text, image_input)
        payload["max_output_tokens"] = 64 if has_image or has_image_generation else 16
        if has_image_generation:
            payload["tools"] = [
                {
                    "type": "image_generation",
                    "model": ProxyService.LEGACY_IMAGE_DEFAULT_TOOL_MODEL,
                    "action": "generate",
                }
            ]
            payload["tool_choice"] = {"type": "image_generation"}
        log_type = "responses"
        endpoint_label = "/v1/responses"
        scenario_key = f"{'imagegen' if has_image_generation else ('image' if has_image else 'text')}_{'stream' if stream else 'json'}_responses"
        scenario_label = f"{'生成图' if has_image_generation else ('图片' if has_image else '文本')} · {'流式' if stream else '非流式'} · responses"
    else:
        payload["messages"] = [
            {
                "role": "user",
                "content": _build_self_test_chat_content(prompt_text, image_input),
            }
        ]
        payload["max_tokens"] = 64 if has_image else 16
        log_type = "chat"
        endpoint_label = "/v1/chat/completions"
        scenario_key = f"{'image' if has_image else 'text'}_{'stream' if stream else 'json'}_chat"
        scenario_label = f"{'图片' if has_image else '文本'} · {'流式' if stream else '非流式'} · chat/completions"

    try:
        if stream:
            response_stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
                endpoint_path=normalized_endpoint_path,
                payload=payload,
                log_type=log_type,
                route_context=auth_context.route_context,
                api_client_auth=auth_context,
            )
            stream_result = await _consume_self_test_stream(response_stream)
            generated_images = list(stream_result.get("generated_images") or [])
            generated_images_count = len(generated_images)
            success = True
            status_code = 200
            message = "stream success"
            if has_image_generation and generated_images_count <= 0:
                success = False
                status_code = 502
                message = "请求已返回，但未提取到图片生成结果"
            return {
                "scenario_key": scenario_key,
                "scenario_label": scenario_label,
                "endpoint_path": endpoint_label,
                "stream": True,
                "has_image": has_image,
                "has_image_generation": has_image_generation,
                "success": success,
                "provider_name": provider.name,
                "model_name": model_name,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "output_text": stream_result["output_text"],
                "response_preview": stream_result["stream_event_preview"],
                "generated_images_count": generated_images_count,
                "trace": trace,
                "message": message,
            }

        response, provider, trace, latency_ms = await ProxyService.forward_json_request(
            endpoint_path=normalized_endpoint_path,
            payload=payload,
            log_type=log_type,
            route_context=auth_context.route_context,
            api_client_auth=auth_context,
        )
        generated_images = ProxyService._extract_generated_images(response, limit_images=4)
        generated_images_count = len(generated_images)
        success = True
        status_code = 200
        message = "json success"
        if has_image_generation and generated_images_count <= 0:
            success = False
            status_code = 502
            message = "请求已返回，但未提取到图片生成结果"
        return {
            "scenario_key": scenario_key,
            "scenario_label": scenario_label,
            "endpoint_path": endpoint_label,
            "stream": False,
            "has_image": has_image,
            "has_image_generation": has_image_generation,
            "success": success,
            "provider_name": provider.name,
            "model_name": model_name,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "output_text": ProxyService._extract_response_display_text(response, limit_bytes=600),
            "response_preview": _build_self_test_safe_preview(response),
            "generated_images_count": generated_images_count,
            "trace": trace,
            "message": message,
        }
    except HTTPException as exc:
        detail = exc.detail
        trace = detail.get("trace") if isinstance(detail, dict) else None
        return {
            "scenario_key": scenario_key,
            "scenario_label": scenario_label,
            "endpoint_path": endpoint_label,
            "stream": stream,
            "has_image": has_image,
            "has_image_generation": has_image_generation,
            "success": False,
            "provider_name": _self_test_provider_name_from_trace(trace),
            "model_name": model_name,
            "status_code": exc.status_code,
            "latency_ms": None,
            "output_text": None,
            "response_preview": detail,
            "trace": trace,
            "message": str(detail),
        }
    except Exception as exc:
        return {
            "scenario_key": scenario_key,
            "scenario_label": scenario_label,
            "endpoint_path": endpoint_label,
            "stream": stream,
            "has_image": has_image,
            "has_image_generation": has_image_generation,
            "success": False,
            "provider_name": None,
            "model_name": model_name,
            "status_code": 500,
            "latency_ms": None,
            "output_text": None,
            "response_preview": None,
            "trace": None,
            "message": str(exc),
        }


@router.get("/user", response_class=HTMLResponse)
def user_home(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    return templates.TemplateResponse(
        "user_home.html",
        {
            "request": request,
            "title": "用户中心",
            "page_name": "user-home",
            "portal_type": "user",
            "current_user": current_user,
            "external_base_url": _resolve_external_base_url(request),
            **overview,
        },
    )


@router.get("/user/profile", response_class=HTMLResponse)
def user_profile_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    selectable_providers = [item for item in ProviderService.list_provider_dicts(db) if item.get("enabled")]
    return templates.TemplateResponse(
        "user_profile.html",
        {
            "request": request,
            "title": "个人资料",
            "page_name": "user-profile",
            "portal_type": "user",
            "current_user": current_user,
            "error_message": request.query_params.get("error"),
            "success_message": request.query_params.get("success"),
            "editing_api_key_id": _parse_optional_int(request.query_params.get("edit")),
            "providers": selectable_providers,
            "external_base_url": _resolve_external_base_url(request),
            **overview,
        },
    )


@router.get("/user/api-keys", response_class=HTMLResponse)
def user_api_keys_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    selectable_providers = [item for item in ProviderService.list_provider_dicts(db) if item.get("enabled")]
    return templates.TemplateResponse(
        "user_api_keys.html",
        {
            "request": request,
            "title": "API Key 管理",
            "page_name": "user-api-keys",
            "portal_type": "user",
            "current_user": current_user,
            "error_message": request.query_params.get("error"),
            "success_message": request.query_params.get("success"),
            "editing_api_key_id": _parse_optional_int(request.query_params.get("edit")),
            "providers": selectable_providers,
            "external_base_url": _resolve_external_base_url(request),
            **overview,
        },
    )


@router.get("/user/models", response_class=HTMLResponse)
def user_models_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    models = ModelCatalogService.list_user_models(db, user=current_user)
    return templates.TemplateResponse(
        "user_models.html",
        {
            "request": request,
            "title": "可用模型",
            "page_name": "user-models",
            "portal_type": "user",
            "current_user": current_user,
            "models": models,
        },
    )


@router.get("/user/docs", response_class=HTMLResponse)
def user_docs_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    external_base_url = _resolve_external_base_url(request)
    external_v1_base_url = f"{external_base_url}/v1"
    return templates.TemplateResponse(
        "docs.html",
        {
            "request": request,
            "title": "使用文档",
            "page_name": "user-docs",
            "portal_type": "user",
            "current_user": current_user,
            "external_base_url": external_base_url,
            "external_v1_base_url": external_v1_base_url,
        },
    )


@router.post("/user/profile", response_class=HTMLResponse)
def update_user_profile(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    try:
        UserAuthService.update_profile(db, current_user, username=username, email=email)
    except ValueError as exc:
        return RedirectResponse(f"/user/profile?error={str(exc)}", status_code=303)
    return RedirectResponse("/user/profile?success=profile_updated", status_code=303)


@router.post("/user/password", response_class=HTMLResponse)
def update_user_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    if not UserAuthService.verify_password(current_password, current_user.password_hash):
        return RedirectResponse("/user/profile?error=current_password_invalid", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/user/profile?error=password_not_match", status_code=303)
    try:
        UserAuthService.update_password(db, current_user, password=new_password)
    except ValueError as exc:
        return RedirectResponse(f"/user/profile?error={str(exc)}", status_code=303)
    return RedirectResponse("/user/profile?success=password_updated", status_code=303)


@router.post("/user/api-keys/create", response_class=HTMLResponse)
def create_user_api_key(
    request: Request,
    name: str = Form(...),
    raw_api_key: str | None = Form(default=None),
    remark: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    route_mode: RouteMode = Form(default="failover"),
    default_provider_id: str | None = Form(default=None),
    manual_allow_fallback: str | None = Form(default=None),
    allowed_provider_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    selectable_provider_ids = {item["id"] for item in ProviderService.list_provider_dicts(db) if item.get("enabled")}
    try:
        payload = ApiKeyCreate(
            **_build_user_api_key_payload(
                current_user_id=current_user.id,
                selectable_provider_ids=selectable_provider_ids,
                name=name,
                raw_api_key=raw_api_key,
                remark=remark,
                enabled=enabled == "on",
                route_mode=route_mode,
                default_provider_id=default_provider_id,
                manual_allow_fallback=manual_allow_fallback == "on",
                allowed_provider_ids=allowed_provider_ids,
            )
        )
        ApiKeyAdminService.create_api_key(db, payload)
    except (ValueError, TypeError) as exc:
        return _redirect_user_api_keys(error=str(exc), edit="new")
    return _redirect_user_api_keys(success="api_key_created")


@router.post("/user/api-keys/{api_key_id}/update", response_class=HTMLResponse)
def update_user_api_key(
    api_key_id: int,
    request: Request,
    name: str = Form(...),
    raw_api_key: str | None = Form(default=None),
    remark: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    route_mode: RouteMode = Form(default="failover"),
    default_provider_id: str | None = Form(default=None),
    manual_allow_fallback: str | None = Form(default=None),
    allowed_provider_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    selectable_provider_ids = {item["id"] for item in ProviderService.list_provider_dicts(db) if item.get("enabled")}
    try:
        payload = ApiKeyUpdate(
            **_build_user_api_key_payload(
                current_user_id=current_user.id,
                selectable_provider_ids=selectable_provider_ids,
                name=name,
                raw_api_key=raw_api_key,
                remark=remark,
                enabled=enabled == "on",
                route_mode=route_mode,
                default_provider_id=default_provider_id,
                manual_allow_fallback=manual_allow_fallback == "on",
                allowed_provider_ids=allowed_provider_ids,
            )
        )
        ApiKeyAdminService.update_api_key(db, api_key, payload)
    except (ValueError, TypeError) as exc:
        return _redirect_user_api_keys(error=str(exc), edit=api_key_id)
    return _redirect_user_api_keys(success="api_key_updated", edit=api_key_id)


@router.post("/user/api-keys/{api_key_id}/toggle", response_class=HTMLResponse)
def toggle_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.set_enabled(db, api_key, not api_key.enabled)
    return _redirect_user_api_keys(success="api_key_status_updated", edit=api_key_id)


@router.post("/user/api-keys/{api_key_id}/delete", response_class=HTMLResponse)
def delete_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.delete_api_key(db, api_key)
    return _redirect_user_api_keys(success="api_key_deleted")


@router.get("/user/api-keys/{api_key_id}", response_class=HTMLResponse)
def user_api_key_detail_page(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    payload = UserPortalService.get_api_key_detail_payload(db, user=current_user, api_key_id=api_key_id)
    if payload is None:
        return RedirectResponse("/user/api-keys?error=api_key_not_found", status_code=303)
    return templates.TemplateResponse(
        "user_api_key_detail.html",
        {
            "request": request,
            "title": f"API Key 详情 · {payload['api_key'].name}",
            "page_name": "user-api-key-detail",
            "portal_type": "user",
            "current_user": current_user,
            "success_message": request.query_params.get("success"),
            "external_base_url": _resolve_external_base_url(request),
            **payload,
        },
    )


@router.post("/user/api-keys/{api_key_id}/rotate", response_class=HTMLResponse)
def rotate_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.rotate_api_key(db, api_key)
    return RedirectResponse(f"/user/api-keys/{api_key_id}?success=api_key_rotated", status_code=303)


@router.get("/user/logs", response_class=HTMLResponse)
def user_logs_page(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    _, recent_logs, _, _ = UserPortalService.list_logs(
        db,
        user=current_user,
        page=1,
        page_size=20,
        exclude_health_checks=True,
    )
    return templates.TemplateResponse(
        "user_logs.html",
        {
            "request": request,
            "title": "我的日志",
            "page_name": "user-logs",
            "portal_type": "user",
            "current_user": current_user,
            "recent_logs": recent_logs,
        },
    )


@router.get("/api/user/logs/filter-options", response_model=LogFilterOptionsResponse)
def user_log_filter_options(
    request: Request,
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> LogFilterOptionsResponse:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    return LogFilterOptionsResponse.model_validate(
        UserPortalService.get_log_filter_options(
            db,
            user=current_user,
            exclude_health_checks=exclude_health_checks,
        )
    )


@router.get("/api/user/logs", response_model=LogListResponse)
def user_logs_api(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    log_type: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    model_query: str | None = None,
    api_client_key_id: int | None = None,
    api_client_key_query: str | None = None,
    conversation_key: str | None = None,
    tenant_name: str | None = None,
    project_name: str | None = None,
    app_name: str | None = None,
    environment_name: str | None = None,
    success: bool | None = None,
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> LogListResponse:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    total, items, summary, _ = UserPortalService.list_logs(
        db,
        user=current_user,
        page=page,
        page_size=page_size,
        log_type=log_type,
        provider_id=provider_id,
        model_name=model_name,
        model_query=model_query,
        api_client_key_id=api_client_key_id,
        api_client_key_query=api_client_key_query,
        conversation_key=conversation_key,
        tenant_name=tenant_name,
        project_name=project_name,
        app_name=app_name,
        environment_name=environment_name,
        success=success,
        exclude_health_checks=exclude_health_checks,
    )
    return LogListResponse(
        total=total,
        items=items,
        summary=LogSummaryOut.model_validate(summary),
    )


@router.get("/api/user/metrics/summary", response_model=MetricListResponse)
def user_metrics_summary(
    request: Request,
    window_minutes: int = Query(default=60, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricListResponse:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    api_key_ids = UserPortalService.list_owned_api_key_ids(db, user_id=current_user.id)
    items = [
        MetricItem.model_validate(item)
        for item in LogService.metric_summary(
            db,
            window_minutes=window_minutes,
            user_account_id=current_user.id,
            api_client_key_ids=api_key_ids,
        )
    ]
    return MetricListResponse(window_minutes=window_minutes, items=items)


@router.get("/api/user/metrics/timeseries", response_model=MetricTimeSeriesResponse)
def user_metrics_timeseries(
    request: Request,
    window_minutes: int = Query(default=180, ge=5, le=43200),
    bucket_minutes: int = Query(default=15, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricTimeSeriesResponse:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    api_key_ids = UserPortalService.list_owned_api_key_ids(db, user_id=current_user.id)
    items = [
        MetricTimeSeriesItem.model_validate(item)
        for item in LogService.metric_timeseries(
            db,
            window_minutes=window_minutes,
            bucket_minutes=bucket_minutes,
            user_account_id=current_user.id,
            api_client_key_ids=api_key_ids,
        )
    ]
    return MetricTimeSeriesResponse(window_minutes=window_minutes, bucket_minutes=bucket_minutes, items=items)


@router.get("/user/logs/export")
def export_user_logs(
    request: Request,
    log_type: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    model_query: str | None = None,
    api_client_key_id: int | None = None,
    api_client_key_query: str | None = None,
    conversation_key: str | None = None,
    tenant_name: str | None = None,
    project_name: str | None = None,
    app_name: str | None = None,
    environment_name: str | None = None,
    success: bool | None = None,
    exclude_health_checks: bool = Query(default=True),
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    csv_text = LogService.export_logs_csv(
        db,
        log_type=log_type if log_type in LogService.USER_VISIBLE_LOG_TYPES else None,
        log_types=list(LogService.USER_VISIBLE_LOG_TYPES),
        provider_id=provider_id,
        model_name=model_name,
        model_query=model_query,
        conversation_key=conversation_key,
        api_client_key_id=api_client_key_id,
        api_client_key_query=api_client_key_query,
        user_account_id=current_user.id,
        user_account_query=None,
        tenant_name=tenant_name,
        project_name=project_name,
        app_name=app_name,
        environment_name=environment_name,
        success=success,
        exclude_health_checks=exclude_health_checks,
        api_client_key_ids=UserPortalService.list_owned_api_key_ids(db, user_id=current_user.id),
        limit=limit,
    )
    filename = f"user-logs-{current_user.username}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/user/logs/{log_id}", response_class=HTMLResponse)
def user_log_detail_page(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    log = UserPortalService.get_log_detail(db, user=current_user, log_id=log_id)
    if log is None:
        return RedirectResponse("/user/logs", status_code=303)
    return templates.TemplateResponse(
        "user_log_detail.html",
        {
            "request": request,
            "title": f"日志详情 · #{log.id}",
            "page_name": "user-log-detail",
            "portal_type": "user",
            "current_user": current_user,
            "log": log,
        },
    )


@router.get("/api/user/logs/{log_id}")
def user_log_detail_api(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    log = UserPortalService.get_log_detail(db, user=current_user, log_id=log_id)
    if log is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return JSONResponse(log.model_dump(mode="json"))


@router.get("/user/billing", response_class=HTMLResponse)
def user_billing_page(
    request: Request,
    limit: int = Query(default=50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    billing = UserPortalService.get_billing_overview(db, user=current_user, limit=limit)
    return templates.TemplateResponse(
        "user_billing.html",
        {
            "request": request,
            "title": "我的账单",
            "page_name": "user-billing",
            "portal_type": "user",
            "current_user": current_user,
            "limit": limit,
            **billing,
        },
    )


@router.get("/user/billing/export")
def export_user_billing(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    csv_text = UserPortalService.export_billing_csv(db, user=current_user, limit=limit)
    filename = f"user-billing-{current_user.username}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/user/conversations", response_class=HTMLResponse)
def user_conversations_page(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse(
        "user_conversations.html",
        {
            "request": request,
            "title": "我的会话回放",
            "page_name": "user-conversations",
            "portal_type": "user",
            "current_user": current_user,
        },
    )


@router.get("/api/user/conversations", response_model=ConversationSummaryList)
def user_conversations_api(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    query: str | None = None,
    db: Session = Depends(get_db),
) -> ConversationSummaryList:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    total, items = UserPortalService.list_conversations(
        db,
        user=current_user,
        page=page,
        page_size=page_size,
        query=query,
    )
    return ConversationSummaryList(total=total, items=items)


@router.get("/api/user/conversations/{conversation_key}", response_model=ConversationReplay)
def user_conversation_detail_api(
    conversation_key: str,
    request: Request,
    db: Session = Depends(get_db),
) -> ConversationReplay:
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="unauthorized")
    replay = UserPortalService.get_conversation_replay(db, user=current_user, conversation_key=conversation_key)
    if replay is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return replay


@router.get("/user/self-test", response_class=HTMLResponse)
def user_self_test_page(
    request: Request,
    api_key_id: int | None = Query(default=None),
    model_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    payload = UserPortalService.get_self_test_payload(
        db,
        user=current_user,
        selected_api_key_id=api_key_id,
        selected_model_name=model_name,
    )
    return templates.TemplateResponse(
        "user_self_test.html",
        {
            "request": request,
            "title": "接入自检",
            "page_name": "user-self-test",
            "portal_type": "user",
            "current_user": current_user,
            "external_base_url": _resolve_external_base_url(request),
            **payload,
        },
    )


@router.post("/api/user/self-test/run")
async def run_user_self_test(
    request: Request,
    api_key_id: int = Form(...),
    model_name: str = Form(...),
    image_mode: str = Form(default="none"),
    image_detail: str = Form(default="auto"),
    image_url: str | None = Form(default=None),
    image_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"success": False, "message": "未登录或权限不足"}, status_code=401)
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return JSONResponse({"success": False, "message": "目标 API Key 不存在"}, status_code=404)
    raw_api_key = ApiKeyService.decrypt_raw_api_key(api_key.raw_key_encrypted)
    if not raw_api_key:
        return JSONResponse({"success": False, "message": "当前密钥无法回显明文，请先轮换后再测试"}, status_code=400)
    try:
        normalized_model_name = model_name.strip()
        normalized_image_mode = (image_mode or "none").strip().lower()
        image_input = await _resolve_self_test_image_input(
            image_mode=normalized_image_mode,
            image_detail=image_detail,
            image_url=image_url,
            image_file=image_file,
        )
        auth_context = ApiKeyService.authenticate_request(db, f"Bearer {raw_api_key}")
        scenario_specs = [
            {"endpoint_path": "/chat/completions", "stream": False, "image_input": None},
            {"endpoint_path": "/chat/completions", "stream": True, "image_input": None},
            {"endpoint_path": "/responses", "stream": False, "image_input": None},
            {"endpoint_path": "/responses", "stream": True, "image_input": None},
        ]
        if normalized_image_mode == "generate":
            scenario_specs.extend(
                [
                    {"endpoint_path": "/responses", "stream": False, "image_input": None, "image_generation": True},
                    {"endpoint_path": "/responses", "stream": True, "image_input": None, "image_generation": True},
                ]
            )
        elif image_input is not None:
            scenario_specs.extend(
                [
                    {"endpoint_path": "/chat/completions", "stream": False, "image_input": image_input, "image_generation": False},
                    {"endpoint_path": "/responses", "stream": False, "image_input": image_input, "image_generation": False},
                ]
            )
        scenarios = []
        for item in scenario_specs:
            scenarios.append(
                await _run_user_self_test_probe(
                    db=db,
                    auth_context=auth_context,
                    model_name=normalized_model_name,
                    endpoint_path=item["endpoint_path"],
                    stream=item["stream"],
                    image_input=item["image_input"],
                    image_generation=bool(item.get("image_generation")),
                )
            )
        success_count = sum(1 for item in scenarios if item["success"])
        failed_count = len(scenarios) - success_count
        overall_success = failed_count == 0 and len(scenarios) > 0
        first_failure = next((item for item in scenarios if not item["success"]), None)
        first_success = next((item for item in scenarios if item["success"]), None)
        image_input_summary = None
        if image_input is not None:
            image_input_summary = {
                "source": image_input.get("source"),
                "detail": image_input.get("detail"),
                "filename": image_input.get("filename"),
                "content_type": image_input.get("content_type"),
                "file_size_bytes": image_input.get("file_size_bytes"),
            }
        return JSONResponse(
            {
                "success": overall_success,
                "provider_name": (
                    (first_failure or first_success or {}).get("provider_name")
                ),
                "model_name": normalized_model_name,
                "latency_ms": (first_success or first_failure or {}).get("latency_ms"),
                "trace": (first_failure or first_success or {}).get("trace"),
                "output_text": (first_failure or first_success or {}).get("output_text"),
                "response_preview": (first_failure or first_success or {}).get("response_preview"),
                "message": (
                    "全部自检场景通过"
                    if overall_success
                    else f"共有 {failed_count} 个自检场景失败，请查看明细和修复建议"
                ),
                "summary": {
                    "total_scenarios": len(scenarios),
                    "success_scenarios": success_count,
                    "failed_scenarios": failed_count,
                    "image_enabled": image_input is not None or normalized_image_mode == "generate",
                    "image_generation_enabled": normalized_image_mode == "generate",
                    "image_mode": normalized_image_mode,
                    "image_mode_label": (
                        "图片生成"
                        if normalized_image_mode == "generate"
                        else ("图片理解" if image_input is not None else "仅文本")
                    ),
                },
                "image_input": image_input_summary,
                "scenarios": scenarios,
            }
        )
    except ApiClientAuthError as exc:
        return JSONResponse(
            {
                "success": False,
                "message": exc.message,
                "code": exc.code,
                "status_code": exc.status_code,
            }
        )
    except HTTPException as exc:
        return JSONResponse(
            {
                "success": False,
                "message": str(exc.detail),
                "status_code": exc.status_code,
            }
        )
