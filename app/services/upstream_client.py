from importlib.util import find_spec

import httpx

from app.config import get_settings


class UpstreamClientService:
    _client: httpx.AsyncClient | None = None
    _http1_client: httpx.AsyncClient | None = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            settings = get_settings()
            timeout = httpx.Timeout(
                timeout=settings.request_timeout_ms / 1000,
                pool=settings.upstream_pool_timeout_s,
            )
            limits = httpx.Limits(
                max_connections=settings.upstream_max_connections,
                max_keepalive_connections=settings.upstream_max_keepalive_connections,
                keepalive_expiry=30,
            )
            cls._client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                http2=find_spec("h2") is not None,
            )
        return cls._client

    @classmethod
    def get_http1_client(cls) -> httpx.AsyncClient:
        if cls._http1_client is None:
            settings = get_settings()
            timeout = httpx.Timeout(
                timeout=settings.request_timeout_ms / 1000,
                pool=settings.upstream_pool_timeout_s,
            )
            limits = httpx.Limits(
                max_connections=settings.upstream_max_connections,
                max_keepalive_connections=settings.upstream_max_keepalive_connections,
                keepalive_expiry=30,
            )
            cls._http1_client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                http2=False,
            )
        return cls._http1_client

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is None:
            if cls._http1_client is None:
                return
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
        if cls._http1_client is not None:
            await cls._http1_client.aclose()
            cls._http1_client = None
