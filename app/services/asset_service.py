import hashlib
import secrets
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.uploaded_asset import UploadedAsset
from app.logging.adapters.asset_adapter import AssetLogRecorder


class AssetService:
    IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    MAX_IMAGE_BYTES = 10 * 1024 * 1024

    @staticmethod
    def create_uploaded_image(
        db: Session,
        *,
        upload_file: UploadFile,
        actor_type: str = "system",
        actor_id: int | str | None = None,
        storage_scope: str = "playground",
        trace_id: str | None = None,
    ) -> UploadedAsset:
        content_type = (upload_file.content_type or "").strip().lower()
        if content_type not in AssetService.IMAGE_CONTENT_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持 PNG/JPEG/WEBP/GIF 图片")

        content = upload_file.file.read(AssetService.MAX_IMAGE_BYTES + 1)
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件不能为空")
        if len(content) > AssetService.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="图片大小不能超过 10 MB")

        sha256_hex = hashlib.sha256(content).hexdigest()
        existing = db.scalar(select(UploadedAsset).where(UploadedAsset.sha256_hex == sha256_hex))
        if existing is not None:
            AssetLogRecorder.record_asset_event(
                db,
                asset_id=existing.id,
                asset_event_type="reuse_existing",
                actor_type=actor_type,
                actor_id=actor_id,
                filename=existing.filename,
                content_type=existing.content_type,
                file_size_bytes=existing.file_size_bytes,
                sha256_hex=existing.sha256_hex,
                storage_scope=storage_scope,
                trace_id=trace_id,
            )
            return existing

        uploads_dir = Path(get_settings().uploads_dir)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        safe_suffix = Path(upload_file.filename or "upload.bin").suffix.lower() or ".bin"
        relative_name = f"{sha256_hex[:16]}-{secrets.token_hex(4)}{safe_suffix}"
        storage_path = uploads_dir / relative_name
        storage_path.write_bytes(content)

        asset = UploadedAsset(
            filename=upload_file.filename or relative_name,
            content_type=content_type,
            storage_path=str(storage_path),
            public_path=f"/uploaded-assets/{relative_name}",
            file_size_bytes=len(content),
            media_kind="image",
            sha256_hex=sha256_hex,
            enabled=True,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        AssetLogRecorder.record_asset_event(
            db,
            asset_id=asset.id,
            asset_event_type="upload",
            actor_type=actor_type,
            actor_id=actor_id,
            filename=asset.filename,
            content_type=asset.content_type,
            file_size_bytes=asset.file_size_bytes,
            sha256_hex=asset.sha256_hex,
            storage_scope=storage_scope,
            trace_id=trace_id,
        )
        return asset
