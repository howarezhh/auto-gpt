from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from redis import Redis

from app.config import get_settings
from app.models.api_client_key import ApiClientKey
from app.models.user_account import UserAccount
from app.services.billing_service import BillingService
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


class ApiKeyAuthCache:
    _last_error: str | None = None
    _sync_client: Redis | None = None
    _local_cache: dict[str, tuple[float, dict[str, Any]]] = {}
    _local_negative_cache: dict[str, float] = {}
    _local_api_key_hashes: dict[int, str] = {}

    @classmethod
    def _run_async_compat(cls, coro) -> Any:
        scheduled = False
        try:
            redis_loop = RedisService.event_loop()
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            loop = redis_loop if redis_loop is not None and redis_loop.is_running() else current_loop
            if loop is not None and loop.is_running():
                if loop is current_loop or RedisService.event_loop_thread_id() == threading.get_ident():
                    loop.create_task(coro)
                    scheduled = True
                    return None
                scheduled = True
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)
            scheduled = True
            return asyncio.run(coro)
        except TimeoutError as exc:
            cls._last_error = str(exc) or "api key auth cache async bridge timed out"
            return None
        except Exception as exc:
            cls._last_error = str(exc)
            return None
        finally:
            if not scheduled:
                coro.close()

    @classmethod
    def last_error(cls) -> str | None:
        return cls._last_error

    @staticmethod
    def key_hash_cache_key(key_hash: str) -> str:
        return f"auth:key_hash:{key_hash}"

    @staticmethod
    def invalid_key_hash_cache_key(key_hash: str) -> str:
        return f"auth:invalid_key_hash:{key_hash}"

    @staticmethod
    def api_key_hash_key(api_key_id: int) -> str:
        return f"auth:api_key:{api_key_id}:hash"

    @staticmethod
    def user_api_keys_key(user_id: int) -> str:
        return f"auth:user:{user_id}:api_keys"

    @staticmethod
    def _serialize_datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _to_float(value) -> float | None:
        return BillingService.to_float(value)

    @classmethod
    async def async_get_by_hash(cls, key_hash: str) -> dict[str, Any] | None:
        try:
            raw_value = await RedisService.get_client().get(cls.key_hash_cache_key(key_hash))
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)
            return None
        if not raw_value:
            return None
        data = loads_json(raw_value, None)
        return data if isinstance(data, dict) else None

    @classmethod
    def _get_sync_client(cls) -> Redis | None:
        if not get_settings().redis_url.strip():
            return None
        if cls._sync_client is None:
            cls._sync_client = RedisService.create_sync_client()
        return cls._sync_client

    @classmethod
    def _sync_get_by_hash(cls, key_hash: str) -> dict[str, Any] | None:
        local_value = cls._get_local(key_hash)
        if local_value is not None:
            return local_value
        try:
            client = cls._get_sync_client()
            if client is None:
                return None
            raw_value = client.get(cls.key_hash_cache_key(key_hash))
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)
            return None
        if not raw_value:
            return None
        data = loads_json(raw_value, None)
        if not isinstance(data, dict):
            return None
        cls._set_local(key_hash, data)
        return data

    @classmethod
    def is_invalid_hash_cached(cls, key_hash: str) -> bool:
        ttl = int(getattr(get_settings(), "api_key_auth_negative_cache_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return False
        now = time.monotonic()
        expires_at = cls._local_negative_cache.get(key_hash)
        if expires_at is not None:
            if expires_at > now:
                return True
            cls._local_negative_cache.pop(key_hash, None)
        try:
            client = cls._get_sync_client()
            if client is None:
                return False
            cached = client.get(cls.invalid_key_hash_cache_key(key_hash))
        except Exception as exc:
            cls._last_error = str(exc)
            return False
        if not cached:
            return False
        cls._local_negative_cache[key_hash] = now + ttl
        cls._enforce_local_negative_cache_limit()
        return True

    @classmethod
    async def async_set_invalid_hash(cls, key_hash: str) -> None:
        ttl = int(getattr(get_settings(), "api_key_auth_negative_cache_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return
        try:
            await RedisService.get_client().setex(cls.invalid_key_hash_cache_key(key_hash), ttl, "1")
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    def set_invalid_hash(cls, key_hash: str) -> None:
        ttl = int(getattr(get_settings(), "api_key_auth_negative_cache_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return
        cls._local_negative_cache[key_hash] = time.monotonic() + ttl
        cls._enforce_local_negative_cache_limit()
        cls._run_async_compat(cls.async_set_invalid_hash(key_hash))

    @classmethod
    def _get_local(cls, key_hash: str) -> dict[str, Any] | None:
        ttl = float(getattr(get_settings(), "api_key_auth_l1_cache_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return None
        item = cls._local_cache.get(key_hash)
        if item is None:
            return None
        expires_at, data = item
        if expires_at <= time.monotonic():
            cls._local_cache.pop(key_hash, None)
            return None
        return data

    @classmethod
    def get_local_by_hash(cls, key_hash: str) -> dict[str, Any] | None:
        return cls._get_local(key_hash)

    @classmethod
    def _set_local(cls, key_hash: str, data: dict[str, Any]) -> None:
        ttl = float(getattr(get_settings(), "api_key_auth_l1_cache_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return
        cls._local_negative_cache.pop(key_hash, None)
        cls._prune_local_cache()
        cls._local_cache[key_hash] = (time.monotonic() + ttl, data)
        api_key = data.get("api_key") if isinstance(data, dict) else None
        api_key_id = api_key.get("id") if isinstance(api_key, dict) else None
        if api_key_id is not None:
            cls._local_api_key_hashes[int(api_key_id)] = key_hash
        cls._enforce_local_cache_limit()

    @classmethod
    def _prune_local_cache(cls) -> None:
        now = time.monotonic()
        expired_keys = [key for key, item in cls._local_cache.items() if item[0] <= now]
        for key in expired_keys:
            cls._local_cache.pop(key, None)
        if expired_keys:
            live_hashes = set(cls._local_cache)
            cls._local_api_key_hashes = {
                api_key_id: key_hash
                for api_key_id, key_hash in cls._local_api_key_hashes.items()
                if key_hash in live_hashes
            }
        expired_negative_keys = [key for key, expires_at in cls._local_negative_cache.items() if expires_at <= now]
        for key in expired_negative_keys:
            cls._local_negative_cache.pop(key, None)

    @classmethod
    def _enforce_local_cache_limit(cls) -> None:
        max_entries = int(getattr(get_settings(), "api_key_auth_l1_max_entries", 10000) or 0)
        if max_entries <= 0 or len(cls._local_cache) <= max_entries:
            return
        overflow = len(cls._local_cache) - max_entries
        oldest_keys = sorted(cls._local_cache, key=lambda key: cls._local_cache[key][0])[:overflow]
        for key in oldest_keys:
            cls._local_cache.pop(key, None)
        live_hashes = set(cls._local_cache)
        cls._local_api_key_hashes = {
            api_key_id: key_hash
            for api_key_id, key_hash in cls._local_api_key_hashes.items()
            if key_hash in live_hashes
        }

    @classmethod
    def _enforce_local_negative_cache_limit(cls) -> None:
        max_entries = int(getattr(get_settings(), "api_key_auth_l1_max_entries", 10000) or 0)
        if max_entries <= 0 or len(cls._local_negative_cache) <= max_entries:
            return
        overflow = len(cls._local_negative_cache) - max_entries
        oldest_keys = sorted(cls._local_negative_cache, key=lambda key: cls._local_negative_cache[key])[:overflow]
        for key in oldest_keys:
            cls._local_negative_cache.pop(key, None)

    @classmethod
    async def async_set_auth_context(
        cls,
        *,
        key_hash: str,
        api_key: ApiClientKey,
        allowed_provider_ids: list[int],
        remaining_balance: float | None,
        policy_snapshot_json: str,
        owner_user: UserAccount | None = None,
        owner_quota_snapshot: dict | None = None,
    ) -> None:
        settings = get_settings()
        ttl = max(30, min(int(settings.api_key_auth_cache_ttl_seconds), 120))
        payload = {
            "api_key": {
                "id": api_key.id,
                "name": api_key.name,
                "remark": api_key.remark,
                "tenant_name": api_key.tenant_name,
                "project_name": api_key.project_name,
                "app_name": api_key.app_name,
                "environment_name": api_key.environment_name,
                "key_prefix": api_key.key_prefix,
                "key_hash": key_hash,
                "enabled": api_key.enabled,
                "expires_at": cls._serialize_datetime(api_key.expires_at),
                "qps_limit": api_key.qps_limit,
                "rpm_limit": api_key.rpm_limit,
                "prompt_tokens_used": api_key.prompt_tokens_used,
                "completion_tokens_used": api_key.completion_tokens_used,
                "total_tokens_used": api_key.total_tokens_used,
                "total_cost_used": cls._to_float(api_key.total_cost_used) or 0,
                "owner_user_id": api_key.owner_user_id,
                "allowed_model_names_json": api_key.allowed_model_names_json,
                "allowed_endpoint_paths_json": api_key.allowed_endpoint_paths_json,
                "allowed_source_ips_json": api_key.allowed_source_ips_json,
                "preferred_provider_ids_json": api_key.preferred_provider_ids_json,
                "preferred_region_tags_json": api_key.preferred_region_tags_json,
                "latency_bias": api_key.latency_bias,
                "success_rate_bias": api_key.success_rate_bias,
            },
            "owner_user": (
                {
                    "id": owner_user.id,
                    "username": owner_user.username,
                    "enabled": owner_user.enabled,
                    "balance_amount": cls._to_float(owner_user.balance_amount),
                    "frozen_amount": cls._to_float(owner_user.frozen_amount) or 0,
                    "total_recharge_amount": cls._to_float(owner_user.total_recharge_amount) or 0,
                }
                if owner_user is not None
                else None
            ),
            "owner_quota_snapshot": owner_quota_snapshot,
            "allowed_provider_ids": allowed_provider_ids,
            "remaining_balance": remaining_balance,
            "policy_snapshot_json": policy_snapshot_json,
        }
        try:
            client = RedisService.get_client()
            cache_key = cls.key_hash_cache_key(key_hash)
            auxiliary_ttl = ttl + 60
            await client.setex(cache_key, ttl, dumps_json(payload))
            await client.delete(cls.invalid_key_hash_cache_key(key_hash))
            if api_key.id is not None:
                await client.set(cls.api_key_hash_key(api_key.id), key_hash, ex=auxiliary_ttl)
            if api_key.owner_user_id is not None and api_key.id is not None:
                user_key = cls.user_api_keys_key(api_key.owner_user_id)
                await client.sadd(user_key, api_key.id)
                await client.expire(user_key, auxiliary_ttl)
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    def build_auth_context(cls, data: dict[str, Any]):
        from app.services.router_service import RoutePolicyContext
        from app.services.setting_service import SettingService

        api_key_data = data.get("api_key") or {}
        owner_user_data = data.get("owner_user")
        owner_user = SimpleNamespace(**owner_user_data) if isinstance(owner_user_data, dict) else None
        api_key = SimpleNamespace(
            **{
                **api_key_data,
                "expires_at": cls._parse_datetime(api_key_data.get("expires_at")),
                "owner_user": owner_user,
                "provider_bindings": [],
            }
        )
        allowed_provider_ids = [int(item) for item in data.get("allowed_provider_ids") or []]
        route_setting = SettingService.get_cached()
        route_context = RoutePolicyContext(
            allowed_provider_ids=allowed_provider_ids,
            require_trusted_provider=bool(getattr(route_setting, "trusted_providers_only", False)),
            preferred_provider_ids=loads_json(api_key.preferred_provider_ids_json, []),
            preferred_region_tags=loads_json(api_key.preferred_region_tags_json, []),
            latency_bias=api_key.latency_bias,
            success_rate_bias=api_key.success_rate_bias,
        )
        return api_key, route_context

    @classmethod
    async def async_invalidate_hash(cls, key_hash: str | None) -> None:
        if not key_hash:
            return
        try:
            await RedisService.get_client().delete(cls.key_hash_cache_key(key_hash), cls.invalid_key_hash_cache_key(key_hash))
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    async def async_invalidate_api_key(cls, api_key_id: int | None, key_hash: str | None = None) -> None:
        if api_key_id is None and not key_hash:
            return
        try:
            client = RedisService.get_client()
            hashes: set[str] = set()
            if key_hash:
                hashes.add(key_hash)
            if api_key_id is not None:
                mapped_hash = await client.get(cls.api_key_hash_key(api_key_id))
                if mapped_hash:
                    hashes.add(str(mapped_hash))
                await client.delete(cls.api_key_hash_key(api_key_id))
            keys = [
                key
                for item in hashes
                if item
                for key in (cls.key_hash_cache_key(item), cls.invalid_key_hash_cache_key(item))
            ]
            if keys:
                await client.delete(*keys)
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    async def async_invalidate_user(cls, user_id: int | None) -> None:
        if user_id is None:
            return
        try:
            client = RedisService.get_client()
            set_key = cls.user_api_keys_key(user_id)
            scanned = 0
            scan_limit = int(getattr(get_settings(), "api_key_auth_user_invalidate_scan_limit", 5000) or 5000)
            async for raw_api_key_id in client.sscan_iter(set_key, count=100):
                scanned += 1
                if scanned > scan_limit:
                    break
                await cls.async_invalidate_api_key(int(raw_api_key_id))
            await client.delete(set_key)
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    async def aclose(cls) -> None:
        cls._last_error = None

    @classmethod
    def get_by_hash(cls, key_hash: str) -> dict[str, Any] | None:
        return cls._sync_get_by_hash(key_hash)

    @classmethod
    def set_auth_context(cls, **kwargs) -> None:
        cls._run_async_compat(cls.async_set_auth_context(**kwargs))

    @classmethod
    def invalidate_hash(cls, key_hash: str | None) -> None:
        if key_hash:
            cls._local_cache.pop(key_hash, None)
            cls._local_negative_cache.pop(key_hash, None)
        cls._run_async_compat(cls.async_invalidate_hash(key_hash))

    @classmethod
    def invalidate_api_key(cls, api_key_id: int | None, key_hash: str | None = None) -> None:
        if key_hash:
            cls._local_cache.pop(key_hash, None)
            cls._local_negative_cache.pop(key_hash, None)
        if api_key_id is not None:
            mapped_hash = cls._local_api_key_hashes.pop(int(api_key_id), None)
            if mapped_hash:
                cls._local_cache.pop(mapped_hash, None)
                cls._local_negative_cache.pop(mapped_hash, None)
        cls._run_async_compat(cls.async_invalidate_api_key(api_key_id, key_hash))

    @classmethod
    def invalidate_user(cls, user_id: int | None) -> None:
        cls._local_cache.clear()
        cls._local_negative_cache.clear()
        cls._local_api_key_hashes.clear()
        cls._run_async_compat(cls.async_invalidate_user(user_id))

    @classmethod
    def close(cls) -> None:
        cls._last_error = None
        cls._local_cache.clear()
        cls._local_negative_cache.clear()
        cls._local_api_key_hashes.clear()
        if cls._sync_client is not None:
            cls._sync_client.close()
            cls._sync_client = None
