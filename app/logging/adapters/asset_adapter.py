from __future__ import annotations

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher


class AssetLogRecorder:
    @staticmethod
    def record_asset_event(db: Session, *, asset_id: int | None, asset_event_type: str, actor_type: str, actor_id: int | str | None = None, filename: str | None = None, content_type: str | None = None, file_size_bytes: int | None = None, sha256_hex: str | None = None, storage_scope: str, result: str = "success", error: str | None = None, trace_id: str | None = None, request_log_id: int | None = None, auto_commit: bool = True):
        event = LoggingDispatcher.build_event(
            event_type="asset",
            event_name="asset",
            trace_id=trace_id,
            correlation_id=str(asset_id) if asset_id is not None else None,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id is not None else None,
            module="asset",
            result=result,
            payload={
                "asset_id": asset_id,
                "asset_event_type": asset_event_type,
                "actor_type": actor_type,
                "actor_id": str(actor_id) if actor_id is not None else None,
                "filename": filename,
                "content_type": content_type,
                "file_size_bytes": file_size_bytes,
                "sha256_prefix": sha256_hex[:16] if sha256_hex else None,
                "sha256_hex": sha256_hex,
                "storage_scope": storage_scope,
                "result": result,
                "error": error,
                "trace_id": trace_id,
                "request_log_id": request_log_id,
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)
