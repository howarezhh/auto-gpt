from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.api_key_service import ApiClientAuthContext, require_api_client_auth
from app.services.proxy_service import ProxyService
from app.utils.http_headers import build_proxy_response_headers


router = APIRouter(tags=["proxy"])


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    payload: dict,
    response: Response,
    db: Session = Depends(get_db),
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    if payload.get("stream") is True:
        stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
            db,
            endpoint_path="/chat/completions",
            payload=payload,
            log_type="chat",
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
        )
        headers = build_proxy_response_headers(
            provider_id=provider.id,
            provider_name=provider.name,
            latency_ms=latency_ms,
            trace_length=len(trace),
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    result, provider, trace, latency_ms = await ProxyService.forward_json_request(
        db,
        endpoint_path="/chat/completions",
        payload=payload,
        log_type="chat",
        route_context=api_client_auth.route_context,
        api_client_auth=api_client_auth,
    )
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
    ).items():
        response.headers[key] = value
    return result


@router.post("/v1/responses", response_model=None)
async def responses(
    payload: dict,
    response: Response,
    db: Session = Depends(get_db),
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    if payload.get("stream") is True:
        stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
            db,
            endpoint_path="/responses",
            payload=payload,
            log_type="responses",
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
        )
        headers = build_proxy_response_headers(
            provider_id=provider.id,
            provider_name=provider.name,
            latency_ms=latency_ms,
            trace_length=len(trace),
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    result, provider, trace, latency_ms = await ProxyService.forward_json_request(
        db,
        endpoint_path="/responses",
        payload=payload,
        log_type="responses",
        route_context=api_client_auth.route_context,
        api_client_auth=api_client_auth,
    )
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
    ).items():
        response.headers[key] = value
    return result


@router.get("/v1/models")
def list_models(
    db: Session = Depends(get_db),
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
) -> dict:
    return ProxyService.list_models(db, route_context=api_client_auth.route_context)
