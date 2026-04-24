from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.api_key_service import ApiClientAuthContext, require_api_client_auth
from app.services.proxy_service import ProxyService


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
        headers = {
            "X-Proxy-Provider-Id": str(provider.id),
            "X-Proxy-Provider-Name": provider.name,
            "X-Proxy-Latency-Ms": str(latency_ms),
            "X-Proxy-Trace-Length": str(len(trace)),
        }
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    result, provider, trace, latency_ms = await ProxyService.forward_json_request(
        db,
        endpoint_path="/chat/completions",
        payload=payload,
        log_type="chat",
        route_context=api_client_auth.route_context,
        api_client_auth=api_client_auth,
    )
    response.headers["X-Proxy-Provider-Id"] = str(provider.id)
    response.headers["X-Proxy-Provider-Name"] = provider.name
    response.headers["X-Proxy-Latency-Ms"] = str(latency_ms)
    response.headers["X-Proxy-Trace-Length"] = str(len(trace))
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
        headers = {
            "X-Proxy-Provider-Id": str(provider.id),
            "X-Proxy-Provider-Name": provider.name,
            "X-Proxy-Latency-Ms": str(latency_ms),
            "X-Proxy-Trace-Length": str(len(trace)),
        }
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    result, provider, trace, latency_ms = await ProxyService.forward_json_request(
        db,
        endpoint_path="/responses",
        payload=payload,
        log_type="responses",
        route_context=api_client_auth.route_context,
        api_client_auth=api_client_auth,
    )
    response.headers["X-Proxy-Provider-Id"] = str(provider.id)
    response.headers["X-Proxy-Provider-Name"] = provider.name
    response.headers["X-Proxy-Latency-Ms"] = str(latency_ms)
    response.headers["X-Proxy-Trace-Length"] = str(len(trace))
    return result


@router.get("/v1/models")
def list_models(
    db: Session = Depends(get_db),
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
) -> dict:
    return ProxyService.list_models(db, route_context=api_client_auth.route_context)
