from importlib.util import find_spec

import httpx

from app.config import get_settings


class UpstreamClientService:
    _client: httpx.AsyncClient | None = None
    _http1_client: httpx.AsyncClient | None = None
    _client_fingerprint: tuple | None = None
    _http1_client_fingerprint: tuple | None = None

    @staticmethod
    def _build_fingerprint(*, http2: bool) -> tuple:
        settings = get_settings()
        return (
            settings.request_timeout_ms,
            settings.upstream_pool_timeout_s,
            settings.upstream_max_connections,
            settings.upstream_max_keepalive_connections,
            http2,
        )

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        fingerprint = cls._build_fingerprint(http2=find_spec("h2") is not None)
        if cls._client is None or cls._client_fingerprint != fingerprint:
            settings = get_settings()
            timeout = httpx.Timeout(
                connect=settings.request_timeout_ms / 1000,
                write=settings.request_timeout_ms / 1000,
                read=settings.request_timeout_ms / 1000,
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
            cls._client_fingerprint = fingerprint
        return cls._client

    @classmethod
    def get_http1_client(cls) -> httpx.AsyncClient:
        fingerprint = cls._build_fingerprint(http2=False)
        if cls._http1_client is None or cls._http1_client_fingerprint != fingerprint:
            settings = get_settings()
            timeout = httpx.Timeout(
                connect=settings.request_timeout_ms / 1000,
                write=settings.request_timeout_ms / 1000,
                read=settings.request_timeout_ms / 1000,
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
            cls._http1_client_fingerprint = fingerprint
        return cls._http1_client

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is None:
            if cls._http1_client is None:
                return
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
            cls._client_fingerprint = None
        if cls._http1_client is not None:
            await cls._http1_client.aclose()
            cls._http1_client = None
            cls._http1_client_fingerprint = None
