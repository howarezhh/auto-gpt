from __future__ import annotations

import asyncio
import threading
import time

from redis import Redis as SyncRedis
from redis.asyncio import Redis

from app.config import get_settings


class RedisService:
    _client: Redis | None = None
    _sync_client: SyncRedis | None = None
    _last_error: str | None = None
    _last_error_at: float | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread_id: int | None = None

    @classmethod
    def client_kwargs(cls) -> dict:
        settings = get_settings()
        return {
            "decode_responses": True,
            "max_connections": settings.redis_max_connections,
            "socket_connect_timeout": settings.redis_socket_connect_timeout_seconds,
            "socket_timeout": settings.redis_socket_timeout_seconds,
            "health_check_interval": settings.redis_health_check_interval_seconds,
        }

    @classmethod
    def create_async_client(cls) -> Redis:
        settings = get_settings()
        if not settings.redis_url.strip():
            raise RuntimeError("REDIS_URL is empty")
        return Redis.from_url(settings.redis_url, **cls.client_kwargs())

    @classmethod
    def create_sync_client(cls) -> SyncRedis:
        settings = get_settings()
        if not settings.redis_url.strip():
            raise RuntimeError("REDIS_URL is empty")
        return SyncRedis.from_url(settings.redis_url, **cls.client_kwargs())

    @classmethod
    async def init(cls) -> None:
        cls._loop = asyncio.get_running_loop()
        cls._loop_thread_id = threading.get_ident()
        settings = get_settings()
        if not settings.redis_url.strip():
            cls._client = None
            cls._last_error = "REDIS_URL is empty"
            return
        if cls._client is None:
            cls._client = cls.create_async_client()
        try:
            await cls._client.ping()
            cls.clear_last_error()
        except Exception as exc:
            cls.mark_error(exc)

    @classmethod
    def get_client(cls) -> Redis:
        if cls._client is None:
            cls._client = cls.create_async_client()
        return cls._client

    @classmethod
    def get_sync_client(cls) -> SyncRedis:
        if cls._sync_client is None:
            cls._sync_client = cls.create_sync_client()
        return cls._sync_client

    @classmethod
    async def ping(cls) -> bool:
        try:
            await cls.get_client().ping()
            cls.clear_last_error()
            return True
        except Exception as exc:
            cls.mark_error(exc)
            return False

    @classmethod
    def last_error(cls) -> str | None:
        return cls._last_error

    @classmethod
    def mark_error(cls, exc: Exception | str) -> None:
        cls._last_error = str(exc)
        cls._last_error_at = time.monotonic()

    @classmethod
    def clear_last_error(cls) -> None:
        cls._last_error = None
        cls._last_error_at = None

    @classmethod
    def should_skip_after_recent_error(cls, *, cooldown_seconds: float = 5.0) -> bool:
        if cls._last_error is None or cls._last_error_at is None:
            return False
        return time.monotonic() - cls._last_error_at < max(0.1, cooldown_seconds)

    @classmethod
    def event_loop(cls) -> asyncio.AbstractEventLoop | None:
        return cls._loop

    @classmethod
    def event_loop_thread_id(cls) -> int | None:
        return cls._loop_thread_id

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
        if cls._sync_client is not None:
            cls._sync_client.close()
            cls._sync_client = None
        cls._loop = None
        cls._loop_thread_id = None
