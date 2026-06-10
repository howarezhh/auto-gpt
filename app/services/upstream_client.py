from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib.util import find_spec
import json

import aiohttp
import httpx

from app.config import get_settings


class UpstreamClientService:
    """统一管理访问上游模型服务的 httpx 客户端实例。"""

    _client: httpx.AsyncClient | None = None
    _http1_client: httpx.AsyncClient | None = None
    _aiohttp_session: aiohttp.ClientSession | None = None
    _client_fingerprint: tuple | None = None
    _http1_client_fingerprint: tuple | None = None
    _aiohttp_fingerprint: tuple | None = None

    @staticmethod
    def _build_fingerprint(*, http2: bool) -> tuple:
        """根据当前配置生成客户端参数指纹。"""
        settings = get_settings()
        return (
            settings.request_timeout_ms,
            settings.upstream_pool_timeout_s,
            settings.upstream_max_connections,
            settings.upstream_max_keepalive_connections,
            settings.upstream_keepalive_expiry_seconds,
            settings.upstream_dns_cache_ttl_seconds,
            http2,
        )

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        """返回支持 HTTP/2 的默认上游客户端。"""
        fingerprint = cls._build_fingerprint(http2=find_spec("h2") is not None)
        if cls._client is None or cls._client_fingerprint != fingerprint:
            if cls._client is not None:
                cls._schedule_client_close(cls._client)
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
                keepalive_expiry=settings.upstream_keepalive_expiry_seconds,
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
        """返回强制 HTTP/1.1 的上游客户端。"""
        fingerprint = cls._build_fingerprint(http2=False)
        if cls._http1_client is None or cls._http1_client_fingerprint != fingerprint:
            if cls._http1_client is not None:
                cls._schedule_client_close(cls._http1_client)
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
                keepalive_expiry=settings.upstream_keepalive_expiry_seconds,
            )
            cls._http1_client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                http2=False,
            )
            cls._http1_client_fingerprint = fingerprint
        return cls._http1_client

    @classmethod
    def get_aiohttp_session(cls) -> aiohttp.ClientSession:
        """返回纯异步 HTTP/1.1 上游客户端，避免非流式 JSON 请求占用线程池。"""
        fingerprint = cls._build_fingerprint(http2=False)
        if cls._aiohttp_session is None or cls._aiohttp_session.closed or cls._aiohttp_fingerprint != fingerprint:
            if cls._aiohttp_session is not None and not cls._aiohttp_session.closed:
                cls._schedule_client_close(cls._aiohttp_session)
            settings = get_settings()
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=settings.request_timeout_ms / 1000,
                sock_connect=settings.request_timeout_ms / 1000,
                sock_read=settings.request_timeout_ms / 1000,
            )
            connector = aiohttp.TCPConnector(
                limit=settings.upstream_max_connections,
                limit_per_host=settings.upstream_max_connections,
                ttl_dns_cache=settings.upstream_dns_cache_ttl_seconds,
                use_dns_cache=settings.upstream_dns_cache_ttl_seconds > 0,
                keepalive_timeout=settings.upstream_keepalive_expiry_seconds,
                enable_cleanup_closed=True,
            )
            cls._aiohttp_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            cls._aiohttp_fingerprint = fingerprint
        return cls._aiohttp_session

    @staticmethod
    def _schedule_client_close(client: httpx.AsyncClient | aiohttp.ClientSession) -> None:
        """在配置热变更导致客户端重建时，异步释放旧连接池。"""
        import asyncio

        async def close_client() -> None:
            if isinstance(client, httpx.AsyncClient):
                await client.aclose()
            else:
                await client.close()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(close_client())
            return
        task = loop.create_task(close_client())
        task.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)

    @classmethod
    async def aclose(cls) -> None:
        """关闭所有已创建的上游客户端实例。"""
        if cls._client is None and cls._http1_client is None and cls._aiohttp_session is None:
            return
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
            cls._client_fingerprint = None
        if cls._http1_client is not None:
            await cls._http1_client.aclose()
            cls._http1_client = None
            cls._http1_client_fingerprint = None
        if cls._aiohttp_session is not None:
            await cls._aiohttp_session.close()
            cls._aiohttp_session = None
            cls._aiohttp_fingerprint = None


@dataclass(slots=True)
class AiohttpStreamResponse:
    """对齐 httpx 常用接口的轻量 aiohttp 流式响应包装。"""

    response: aiohttp.ClientResponse
    request_method: str
    request_url: str
    _cached_body: bytes | None = None

    @property
    def status_code(self) -> int:
        return int(self.response.status)

    @property
    def headers(self) -> aiohttp.typedefs.LooseHeaders:
        return self.response.headers

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._iter_bytes()

    async def _iter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.content.iter_any():
            if chunk:
                yield chunk

    async def aread(self) -> bytes:
        if self._cached_body is None:
            self._cached_body = await self.response.read()
        return self._cached_body

    def to_httpx_response(self, *, content: bytes | None = None) -> httpx.Response:
        body = self._cached_body if content is None else content
        return httpx.Response(
            self.status_code,
            headers=dict(self.response.headers),
            content=body,
            request=httpx.Request(self.request_method, self.request_url),
        )


@dataclass(slots=True)
class AiohttpJsonResponse:
    """对齐非流式 JSON 热路径所需的轻量响应接口。"""

    status_code: int
    headers: dict[str, str]
    content: bytes
    request_method: str
    request_url: str
    _cached_json: dict | list | None = None
    _json_loaded: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    async def aread(self) -> bytes:
        return self.content

    def json(self) -> dict | list:
        if not self._json_loaded:
            self._cached_json = json.loads(self.text)
            self._json_loaded = True
        return self._cached_json

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        self.to_httpx_response().raise_for_status()

    def to_httpx_response(self) -> httpx.Response:
        return httpx.Response(
            self.status_code,
            headers=self.headers,
            content=self.content,
            request=httpx.Request(self.request_method, self.request_url),
        )
