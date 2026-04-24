from fastapi import Header, HTTPException, status

from app.config import get_settings


def verify_local_api_key(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.local_proxy_api_key:
        return
    expected = f"Bearer {settings.local_proxy_api_key}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local proxy api key")
