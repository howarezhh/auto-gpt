from __future__ import annotations

import asyncio
import threading

from redis import Redis as SyncRedis
from redis.asyncio import Redis

from app.config import get_settings


class RedisService:
    _client: Redis | None = None
    _sync_client: SyncRedis | None = None
    _last_error: str | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread_id: int | None = None

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
            cls._client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await cls._client.ping()
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    def get_client(cls) -> Redis:
        if cls._client is None:
            settings = get_settings()
            if not settings.redis_url.strip():
                raise RuntimeError("REDIS_URL is empty")
            cls._client = Redis.from_url(settings.redis_url, decode_responses=True)
        return cls._client

    @classmethod
    def get_sync_client(cls) -> SyncRedis:
        if cls._sync_client is None:
            settings = get_settings()
            if not settings.redis_url.strip():
                raise RuntimeError("REDIS_URL is empty")
            cls._sync_client = SyncRedis.from_url(settings.redis_url, decode_responses=True)
        return cls._sync_client

    @classmethod
    async def ping(cls) -> bool:
        try:
            await cls.get_client().ping()
            cls._last_error = None
            return True
        except Exception as exc:
            cls._last_error = str(exc)
            return False

    @classmethod
    def last_error(cls) -> str | None:
        return cls._last_error

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
