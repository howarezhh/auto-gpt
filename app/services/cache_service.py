from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


@dataclass
class CacheEntry:
    expires_at: float
    value: Any


class CacheService:
    _lock = Lock()
    _store: dict[str, CacheEntry] = {}
    _redis_prefix = "shared-cache:"

    @classmethod
    def get(cls, key: str) -> Any | None:
        redis_value = cls._redis_get(key)
        if redis_value is not None:
            return redis_value
        now = time.time()
        with cls._lock:
            entry = cls._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                cls._store.pop(key, None)
                return None
            return entry.value

    @classmethod
    def set(cls, key: str, value: Any, *, ttl_seconds: int) -> Any:
        if ttl_seconds <= 0:
            return value
        if cls._is_redis_safe_value(value) and cls._redis_set(key, value, ttl_seconds=ttl_seconds):
            cls._memory_delete(key)
            return value
        with cls._lock:
            cls._store[key] = CacheEntry(expires_at=time.time() + ttl_seconds, value=value)
        return value

    @classmethod
    def invalidate_prefix(cls, prefix: str) -> None:
        with cls._lock:
            keys = [key for key in cls._store.keys() if key.startswith(prefix)]
            for key in keys:
                cls._store.pop(key, None)
        cls._redis_invalidate_prefix(prefix)

    @classmethod
    def _memory_delete(cls, key: str) -> None:
        with cls._lock:
            cls._store.pop(key, None)

    @classmethod
    def _redis_key(cls, key: str) -> str:
        return f"{cls._redis_prefix}{key}"

    @classmethod
    def _redis_get(cls, key: str) -> Any | None:
        try:
            client = RedisService.get_sync_client()
            raw_value = client.get(cls._redis_key(key))
        except Exception:
            return None
        if raw_value is None:
            return None
        return loads_json(str(raw_value), None)

    @classmethod
    def _redis_set(cls, key: str, value: Any, *, ttl_seconds: int) -> bool:
        try:
            RedisService.get_sync_client().setex(cls._redis_key(key), int(ttl_seconds), dumps_json(value))
            return True
        except Exception:
            return False

    @classmethod
    def _redis_invalidate_prefix(cls, prefix: str) -> None:
        try:
            client = RedisService.get_sync_client()
            pattern = cls._redis_key(prefix) + "*"
            keys = list(client.scan_iter(match=pattern, count=100))
            if keys:
                client.delete(*keys)
        except Exception:
            return

    @staticmethod
    def _is_redis_safe_value(value: Any) -> bool:
        if value is None or isinstance(value, (str, int, float, bool)):
            return True
        if isinstance(value, list):
            return all(CacheService._is_redis_safe_value(item) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str) and CacheService._is_redis_safe_value(item)
                for key, item in value.items()
            )
        return False
