from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.models.api_client_key import ApiClientKey
from app.models.user_account import UserAccount
from app.services.billing_service import BillingService
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


class ApiKeyAuthCache:
    _last_error: str | None = None

    @classmethod
    def _run_async_compat(cls, coro) -> Any:
        try:
            loop = RedisService.event_loop()
            if loop is not None and loop.is_running():
                if RedisService.event_loop_thread_id() == threading.get_ident():
                    loop.create_task(coro)
                    return None
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)
            return asyncio.run(coro)
        except TimeoutError as exc:
            cls._last_error = str(exc) or "api key auth cache async bridge timed out"
            return None
        except Exception as exc:
            cls._last_error = str(exc)
            return None

    @classmethod
    def last_error(cls) -> str | None:
        return cls._last_error

    @staticmethod
    def key_hash_cache_key(key_hash: str) -> str:
        return f"auth:key_hash:{key_hash}"

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
    async def async_set_auth_context(
        cls,
        *,
        key_hash: str,
        api_key: ApiClientKey,
        allowed_provider_ids: list[int],
        default_provider_id: int | None,
        remaining_tokens: int | None,
        remaining_balance: float | None,
        remaining_requests_daily: int | None,
        remaining_cost_daily: float | None,
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
                "token_limit_total": api_key.token_limit_total,
                "request_limit_daily": api_key.request_limit_daily,
                "token_limit_daily": api_key.token_limit_daily,
                "cost_limit_daily": cls._to_float(api_key.cost_limit_daily),
                "qps_limit": api_key.qps_limit,
                "rpm_limit": api_key.rpm_limit,
                "tpm_limit": api_key.tpm_limit,
                "prompt_tokens_used": api_key.prompt_tokens_used,
                "completion_tokens_used": api_key.completion_tokens_used,
                "total_tokens_used": api_key.total_tokens_used,
                "cost_limit_total": cls._to_float(api_key.cost_limit_total),
                "total_cost_used": cls._to_float(api_key.total_cost_used) or 0,
                "balance_amount": cls._to_float(api_key.balance_amount),
                "total_recharge_amount": cls._to_float(api_key.total_recharge_amount) or 0,
                "route_mode": api_key.route_mode,
                "default_provider_id": default_provider_id,
                "owner_user_id": api_key.owner_user_id,
                "manual_allow_fallback": api_key.manual_allow_fallback,
                "allowed_model_names_json": api_key.allowed_model_names_json,
                "allowed_endpoint_paths_json": api_key.allowed_endpoint_paths_json,
                "allowed_source_ips_json": api_key.allowed_source_ips_json,
                "preferred_provider_ids_json": api_key.preferred_provider_ids_json,
                "preferred_region_tags_json": api_key.preferred_region_tags_json,
                "max_candidate_count": api_key.max_candidate_count,
                "latency_bias": api_key.latency_bias,
                "success_rate_bias": api_key.success_rate_bias,
                "cost_bias": api_key.cost_bias,
            },
            "owner_user": (
                {
                    "id": owner_user.id,
                    "username": owner_user.username,
                    "enabled": owner_user.enabled,
                    "balance_amount": cls._to_float(owner_user.balance_amount),
                    "frozen_amount": cls._to_float(owner_user.frozen_amount) or 0,
                    "total_recharge_amount": cls._to_float(owner_user.total_recharge_amount) or 0,
                    "request_limit_total": owner_user.request_limit_total,
                    "request_limit_daily": owner_user.request_limit_daily,
                    "request_limit_monthly": owner_user.request_limit_monthly,
                    "token_limit_total": owner_user.token_limit_total,
                    "token_limit_daily": owner_user.token_limit_daily,
                    "token_limit_monthly": owner_user.token_limit_monthly,
                    "cost_limit_total": cls._to_float(owner_user.cost_limit_total),
                    "cost_limit_daily": cls._to_float(owner_user.cost_limit_daily),
                    "cost_limit_monthly": cls._to_float(owner_user.cost_limit_monthly),
                }
                if owner_user is not None
                else None
            ),
            "owner_quota_snapshot": owner_quota_snapshot,
            "allowed_provider_ids": allowed_provider_ids,
            "remaining_tokens": None,
            "remaining_balance": None,
            "remaining_requests_daily": None,
            "remaining_cost_daily": None,
            "policy_snapshot_json": policy_snapshot_json,
        }
        try:
            client = RedisService.get_client()
            cache_key = cls.key_hash_cache_key(key_hash)
            await client.setex(cache_key, ttl, dumps_json(payload))
            if api_key.id is not None:
                await client.set(cls.api_key_hash_key(api_key.id), key_hash)
            if api_key.owner_user_id is not None and api_key.id is not None:
                user_key = cls.user_api_keys_key(api_key.owner_user_id)
                await client.sadd(user_key, api_key.id)
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    def build_auth_context(cls, data: dict[str, Any]):
        from app.services.router_service import RoutePolicyContext

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
        route_context = RoutePolicyContext(
            route_mode=api_key.route_mode,
            default_provider_id=api_key.default_provider_id,
            manual_allow_fallback=api_key.manual_allow_fallback,
            allowed_provider_ids=allowed_provider_ids,
            preferred_provider_ids=loads_json(api_key.preferred_provider_ids_json, []),
            preferred_region_tags=loads_json(api_key.preferred_region_tags_json, []),
            max_candidate_count=api_key.max_candidate_count,
            latency_bias=api_key.latency_bias,
            success_rate_bias=api_key.success_rate_bias,
            cost_bias=api_key.cost_bias,
        )
        return api_key, route_context

    @classmethod
    async def async_invalidate_hash(cls, key_hash: str | None) -> None:
        if not key_hash:
            return
        try:
            await RedisService.get_client().delete(cls.key_hash_cache_key(key_hash))
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
            keys = [cls.key_hash_cache_key(item) for item in hashes if item]
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
            api_key_ids = [int(item) for item in await client.smembers(set_key) or []]
            for api_key_id in api_key_ids:
                await cls.async_invalidate_api_key(api_key_id)
            await client.delete(set_key)
            cls._last_error = None
        except Exception as exc:
            cls._last_error = str(exc)

    @classmethod
    async def aclose(cls) -> None:
        cls._last_error = None

    @classmethod
    def get_by_hash(cls, key_hash: str) -> dict[str, Any] | None:
        return cls._run_async_compat(cls.async_get_by_hash(key_hash))

    @classmethod
    def set_auth_context(cls, **kwargs) -> None:
        cls._run_async_compat(cls.async_set_auth_context(**kwargs))

    @classmethod
    def invalidate_hash(cls, key_hash: str | None) -> None:
        cls._run_async_compat(cls.async_invalidate_hash(key_hash))

    @classmethod
    def invalidate_api_key(cls, api_key_id: int | None, key_hash: str | None = None) -> None:
        cls._run_async_compat(cls.async_invalidate_api_key(api_key_id, key_hash))

    @classmethod
    def invalidate_user(cls, user_id: int | None) -> None:
        cls._run_async_compat(cls.async_invalidate_user(user_id))

    @classmethod
    def close(cls) -> None:
        cls._last_error = None
