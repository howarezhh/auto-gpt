from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class CacheEntry:
    expires_at: float
    value: Any


class CacheService:
    _lock = Lock()
    _store: dict[str, CacheEntry] = {}

    @classmethod
    def get(cls, key: str) -> Any | None:
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
        with cls._lock:
            cls._store[key] = CacheEntry(expires_at=time.time() + ttl_seconds, value=value)
        return value

    @classmethod
    def invalidate_prefix(cls, prefix: str) -> None:
        with cls._lock:
            keys = [key for key in cls._store.keys() if key.startswith(prefix)]
            for key in keys:
                cls._store.pop(key, None)
