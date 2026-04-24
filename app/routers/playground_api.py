from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.proxy_service import ProxyService


router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("/chat-completions", response_model=None)
async def playground_chat_completions(
    payload: dict,
    response: Response,
    db: Session = Depends(get_db),
    x_aotu_provider_id: int | None = Header(default=None),
):
    if payload.get("stream") is True:
        stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
            db,
            endpoint_path="/chat/completions",
            payload=payload,
            log_type="chat",
            forced_provider_id=x_aotu_provider_id,
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
        forced_provider_id=x_aotu_provider_id,
    )
    response.headers["X-Proxy-Provider-Id"] = str(provider.id)
    response.headers["X-Proxy-Provider-Name"] = provider.name
    response.headers["X-Proxy-Latency-Ms"] = str(latency_ms)
    response.headers["X-Proxy-Trace-Length"] = str(len(trace))
    return result


@router.post("/responses", response_model=None)
async def playground_responses(
    payload: dict,
    response: Response,
    db: Session = Depends(get_db),
    x_aotu_provider_id: int | None = Header(default=None),
):
    if payload.get("stream") is True:
        stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
            db,
            endpoint_path="/responses",
            payload=payload,
            log_type="responses",
            forced_provider_id=x_aotu_provider_id,
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
        forced_provider_id=x_aotu_provider_id,
    )
    response.headers["X-Proxy-Provider-Id"] = str(provider.id)
    response.headers["X-Proxy-Provider-Name"] = provider.name
    response.headers["X-Proxy-Latency-Ms"] = str(latency_ms)
    response.headers["X-Proxy-Trace-Length"] = str(len(trace))
    return result
