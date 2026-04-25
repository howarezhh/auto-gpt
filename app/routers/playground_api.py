from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.asset_service import AssetService
from app.services.proxy_service import ProxyService


router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("/assets/upload")
def upload_playground_asset(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    asset = AssetService.create_uploaded_image(db, upload_file=file)
    return {
        "id": asset.id,
        "filename": asset.filename,
        "content_type": asset.content_type,
        "file_size_bytes": asset.file_size_bytes,
        "public_path": asset.public_path,
        "asset_url": str(request.base_url).rstrip("/") + asset.public_path,
    }


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
