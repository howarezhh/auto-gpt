from __future__ import annotations

import time
from dataclasses import dataclass
from random import uniform
from threading import Lock
from typing import Any

from app.config import get_settings
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


@dataclass
class CacheEntry:
    """表示内存缓存中的单条记录。"""

    expires_at: float
    value: Any


class CacheService:
    """提供 Redis + 进程内存的双层缓存能力。"""

    _lock = Lock()
    _store: dict[str, CacheEntry] = {}
    _redis_prefix = "shared-cache:"
    _redis_generation_prefix = "shared-cache-generation:"
    _stats: dict[str, int] = {
        "memory_hits": 0,
        "redis_hits": 0,
        "misses": 0,
        "sets": 0,
        "invalidations": 0,
    }

    @classmethod
    def get(cls, key: str) -> Any | None:
        """优先读取短 TTL 本地缓存，再回退到 Redis，降低热路径网络往返。"""
        memory_value = cls._memory_get(key)
        if memory_value is not None:
            cls._record_stat("memory_hits")
            return memory_value
        redis_value = cls._redis_get(key)
        if redis_value is not None:
            cls._record_stat("redis_hits")
            cls._memory_set(
                key,
                redis_value,
                ttl_seconds=cls._local_ttl_seconds(default_ttl=getattr(get_settings(), "cache_l1_ttl_cap_seconds", 5.0)),
            )
            return redis_value
        cls._record_stat("misses")
        return None

    @classmethod
    def _memory_get(cls, key: str) -> Any | None:
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
        """写入缓存；可 JSON 序列化值进入 Redis，所有值都进入短 TTL L1。"""
        if ttl_seconds <= 0:
            return value
        cls._record_stat("sets")
        cls._memory_set(key, value, ttl_seconds=cls._local_ttl_seconds(default_ttl=ttl_seconds))
        if cls._is_redis_safe_value(value) and cls._redis_set(key, value, ttl_seconds=ttl_seconds):
            return value
        return value

    @classmethod
    def _memory_set(cls, key: str, value: Any, *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        with cls._lock:
            cls._prune_expired_locked(now=time.time())
            cls._store[key] = CacheEntry(expires_at=time.time() + ttl_seconds, value=value)
            cls._enforce_max_entries_locked()

    @staticmethod
    def _local_ttl_seconds(*, default_ttl: int | float) -> float:
        cap = float(getattr(get_settings(), "cache_l1_ttl_cap_seconds", 1.0) or 0)
        if cap <= 0:
            return 0.0
        return max(0.0, min(float(default_ttl), cap))

    @classmethod
    def invalidate_prefix(cls, prefix: str) -> None:
        """按前缀失效内存与 Redis 中的缓存项。"""
        cls._record_stat("invalidations")
        with cls._lock:
            keys = [key for key in cls._store.keys() if key.startswith(prefix)]
            for key in keys:
                cls._store.pop(key, None)
        cls._redis_invalidate_prefix(prefix)

    @classmethod
    def invalidate(cls, key: str) -> None:
        """失效内存与 Redis 中的单条缓存项。"""
        cls._record_stat("invalidations")
        cls._memory_delete(key)
        cls._redis_delete(key)

    @classmethod
    def _memory_delete(cls, key: str) -> None:
        """删除进程内存中的单条缓存。"""
        with cls._lock:
            cls._store.pop(key, None)

    @classmethod
    def stats_snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            cls._prune_expired_locked(now=time.time())
            stats = dict(cls._stats)
            memory_entries = len(cls._store)
        total_reads = stats["memory_hits"] + stats["redis_hits"] + stats["misses"]
        hit_count = stats["memory_hits"] + stats["redis_hits"]
        return {
            **stats,
            "memory_entries": memory_entries,
            "total_reads": total_reads,
            "hit_count": hit_count,
            "hit_ratio": round(hit_count / total_reads, 6) if total_reads else None,
            "memory_hit_ratio": round(stats["memory_hits"] / total_reads, 6) if total_reads else None,
        }

    @classmethod
    def reset_stats(cls) -> None:
        with cls._lock:
            for key in cls._stats:
                cls._stats[key] = 0

    @classmethod
    def _record_stat(cls, key: str) -> None:
        with cls._lock:
            cls._stats[key] = int(cls._stats.get(key, 0)) + 1

    @classmethod
    def _prune_expired_locked(cls, *, now: float) -> None:
        expired_keys = [key for key, entry in cls._store.items() if entry.expires_at <= now]
        for key in expired_keys:
            cls._store.pop(key, None)

    @classmethod
    def _enforce_max_entries_locked(cls) -> None:
        max_entries = int(getattr(get_settings(), "cache_l1_max_entries", 10000) or 0)
        if max_entries <= 0 or len(cls._store) <= max_entries:
            return
        overflow = len(cls._store) - max_entries
        oldest_keys = sorted(cls._store, key=lambda item: cls._store[item].expires_at)[:overflow]
        for key in oldest_keys:
            cls._store.pop(key, None)

    @classmethod
    def _redis_key(cls, key: str) -> str:
        """生成 Redis 中使用的真实缓存键名。"""
        namespace = cls._key_namespace(key)
        generation = cls._redis_generation(namespace)
        return f"{cls._redis_prefix}{namespace}:v{generation}:{key}"

    @staticmethod
    def _key_namespace(key: str) -> str:
        """按缓存键首段划分失效命名空间。"""
        normalized = str(key or "").strip()
        if not normalized:
            return "default"
        return normalized.split(":", 1)[0]

    @classmethod
    def _redis_generation_key(cls, namespace: str) -> str:
        return f"{cls._redis_generation_prefix}{namespace}"

    @classmethod
    def _redis_generation(cls, namespace: str) -> int:
        try:
            raw_value = RedisService.get_sync_client().get(cls._redis_generation_key(namespace))
        except Exception:
            return 0
        try:
            return max(0, int(raw_value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _redis_get(cls, key: str) -> Any | None:
        """从 Redis 读取缓存值。"""
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
        """写入 Redis 缓存。"""
        try:
            RedisService.get_sync_client().setex(
                cls._redis_key(key),
                cls._redis_ttl_seconds(ttl_seconds),
                dumps_json(value),
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _redis_ttl_seconds(ttl_seconds: int) -> int:
        base_ttl = max(1, int(ttl_seconds or 1))
        ratio = float(getattr(get_settings(), "cache_redis_ttl_jitter_ratio", 0.0) or 0.0)
        if ratio <= 0:
            return base_ttl
        bounded_ratio = max(0.0, min(ratio, 0.5))
        jitter = uniform(0.0, bounded_ratio)
        return max(1, int(round(base_ttl * (1.0 + jitter))))

    @classmethod
    def _redis_delete(cls, key: str) -> None:
        """删除 Redis 中的单条缓存。"""
        try:
            RedisService.get_sync_client().delete(cls._redis_key(key))
        except Exception:
            return

    @classmethod
    def _redis_invalidate_prefix(cls, prefix: str) -> None:
        """通过共享版本号失效 Redis 中指定前缀的缓存项，避免扫描大 keyspace。"""
        try:
            namespace = cls._key_namespace(prefix)
            RedisService.get_sync_client().incr(cls._redis_generation_key(namespace))
        except Exception:
            return

    @staticmethod
    def _is_redis_safe_value(value: Any) -> bool:
        """判断值是否适合直接以 JSON 形式写入 Redis。"""
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
