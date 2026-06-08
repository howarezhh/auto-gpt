from dataclasses import dataclass
from datetime import datetime, timedelta
import base64
import hashlib
import ipaddress
import re
import secrets
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Header, Request, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.scheduler import scheduler
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.billing_service import BillingService
from app.services.log_service import LogService
from app.services.rate_limit_service import RateLimitExceededError, RateLimitService
from app.services.router_service import RoutePolicyContext
from app.services.setting_service import SettingService
from app.services.user_quota_service import UserQuotaService
from app.utils.json_utils import dumps_json, loads_json


class ApiClientAuthError(Exception):
    """表示 API Key 鉴权失败，并携带可返回给调用方的上下文。"""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        api_client_key_id: int | None = None,
        api_client_key_name: str | None = None,
        api_client_key_prefix: str | None = None,
        user_account_id: int | None = None,
        user_account_name: str | None = None,
        remaining_balance: float | None = None,
        policy_snapshot_json: str | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.api_client_key_id = api_client_key_id
        self.api_client_key_name = api_client_key_name
        self.api_client_key_prefix = api_client_key_prefix
        self.user_account_id = user_account_id
        self.user_account_name = user_account_name
        self.remaining_balance = remaining_balance
        self.policy_snapshot_json = policy_snapshot_json
        super().__init__(message)


@dataclass(slots=True)
class ApiClientAuthContext:
    """表示一次鉴权通过后的调用上下文。"""

    api_client_key: ApiClientKey
    route_context: RoutePolicyContext
    remaining_balance: float | None
    policy_snapshot_json: str


class ApiKeyService:
    """负责 API Key 生成、校验、鉴权与访问策略控制。"""

    DEFAULT_KEY_PREFIX = "sk-aotu-"
    MIN_KEY_LENGTH = 24
    MAX_KEY_LENGTH = 128
    KEY_PATTERN = re.compile(r"^[A-Za-z0-9\-_]+$")
    LAST_USED_TOUCH_DELAY_SECONDS = 15
    _pending_last_used_ids: set[int] = set()
    _last_used_flush_scheduled = False

    @staticmethod
    def generate_api_key() -> str:
        """生成新的原始 API Key。"""
        return f"{ApiKeyService.DEFAULT_KEY_PREFIX}{secrets.token_urlsafe(24)}"

    @staticmethod
    def normalize_raw_key(raw_key: str) -> str:
        """标准化原始 API Key 文本。"""
        return raw_key.strip()

    @staticmethod
    def extract_key_prefix(raw_key: str) -> str:
        """提取用于展示和索引的密钥前缀。"""
        return raw_key[:16]

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        """对 API Key 做 SHA-256 哈希。"""
        normalized = ApiKeyService.normalize_raw_key(raw_key)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_raw_api_key(raw_key: str) -> str:
        """校验 API Key 格式是否合法。"""
        normalized = ApiKeyService.normalize_raw_key(raw_key)
        if len(normalized) < ApiKeyService.MIN_KEY_LENGTH:
            raise ValueError(f"API 密钥长度至少 {ApiKeyService.MIN_KEY_LENGTH} 位")
        if len(normalized) > ApiKeyService.MAX_KEY_LENGTH:
            raise ValueError(f"API 密钥长度不能超过 {ApiKeyService.MAX_KEY_LENGTH} 位")
        if not normalized.startswith(ApiKeyService.DEFAULT_KEY_PREFIX):
            raise ValueError(f"API 密钥必须以 {ApiKeyService.DEFAULT_KEY_PREFIX} 开头")
        if not ApiKeyService.KEY_PATTERN.fullmatch(normalized):
            raise ValueError("API 密钥仅允许字母、数字、连字符和下划线")
        return normalized

    @staticmethod
    def build_raw_api_key(raw_key: str | None) -> str:
        """为空时自动生成 API Key，否则校验后返回。"""
        if raw_key is None or not str(raw_key).strip():
            return ApiKeyService.generate_api_key()
        return ApiKeyService.validate_raw_api_key(str(raw_key))

    @staticmethod
    def extract_source_ip(request: Request) -> str | None:
        """优先从代理头中提取来源 IP，失败时回退到直连地址。"""
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            candidate = forwarded_for.split(",")[0].strip()
            if candidate:
                return candidate
        if request.client is None:
            return None
        return request.client.host

    @staticmethod
    def _get_fernet() -> Fernet:
        """基于当前配置推导用于密钥加解密的 Fernet 实例。"""
        settings = get_settings()
        secret = settings.api_key_encryption_secret or settings.session_secret_key
        derived = hashlib.sha256(secret.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    @staticmethod
    def encrypt_raw_api_key(raw_key: str) -> str:
        """加密原始 API Key，便于安全存储。"""
        normalized = ApiKeyService.normalize_raw_key(raw_key)
        return ApiKeyService._get_fernet().encrypt(normalized.encode("utf-8")).decode("utf-8")

    @staticmethod
    def decrypt_raw_api_key(ciphertext: str | None) -> str | None:
        """解密 API Key；失败时返回 None。"""
        if not ciphertext:
            return None
        try:
            return ApiKeyService._get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return None

    @staticmethod
    def mask_key_prefix(key_prefix: str) -> str:
        """脱敏展示 API Key 前缀。"""
        if len(key_prefix) <= 8:
            return "******"
        return f"{key_prefix[:6]}...{key_prefix[-4:]}"

    @staticmethod
    def parse_bearer_token(authorization: str | None) -> str:
        """从 Authorization 头中解析 Bearer Token。"""
        if not authorization:
            raise ApiClientAuthError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_api_key",
                message="Missing bearer api key",
            )
        prefix, _, value = authorization.partition(" ")
        if prefix.lower() != "bearer" or not value.strip():
            raise ApiClientAuthError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_api_key",
                message="Invalid bearer api key",
            )
        return value.strip()

    @staticmethod
    def _build_auth_context_from_cached_payload(
        *,
        key_hash: str,
        cached_auth: dict[str, object],
        request_path: str | None,
        source_ip: str | None,
    ) -> ApiClientAuthContext:
        api_client_key, route_context = ApiKeyAuthCache.build_auth_context(cached_auth)
        owner_user = api_client_key.owner_user
        owner_user_name = owner_user.username if owner_user else None
        remaining_balance = cached_auth.get("remaining_balance")
        if owner_user is not None:
            remaining_balance = float(BillingService.to_decimal(owner_user.balance_amount) - BillingService.to_decimal(owner_user.frozen_amount))
        policy_snapshot_json = str(cached_auth.get("policy_snapshot_json") or "{}")
        if owner_user is None:
            ApiKeyAuthCache.invalidate_hash(key_hash)
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="owner_user_required",
                message="API Key must be bound to an owner account",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if not api_client_key.enabled:
            ApiKeyAuthCache.invalidate_hash(key_hash)
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="key_disabled",
                message="Api key is disabled",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if api_client_key.expires_at is not None and api_client_key.expires_at <= datetime.utcnow():
            ApiKeyAuthCache.invalidate_hash(key_hash)
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="key_expired",
                message="Api key is expired",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if request_path and not ApiKeyService.is_endpoint_allowed(api_client_key, request_path):
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="endpoint_not_allowed",
                message="Api key is not allowed to access this endpoint",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if source_ip and not ApiKeyService.is_source_ip_allowed(api_client_key, source_ip):
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="source_ip_not_allowed",
                message="Api key is not allowed to call from this source ip",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if owner_user is not None and not owner_user.enabled:
            ApiKeyAuthCache.invalidate_hash(key_hash)
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="owner_user_disabled",
                message="Owner account is disabled",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if not route_context.allowed_provider_ids:
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="no_authorized_provider",
                message="Api key has no authorized providers",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if owner_user is not None:
            available_balance = BillingService.to_decimal(owner_user.balance_amount) - BillingService.to_decimal(owner_user.frozen_amount)
            if available_balance <= Decimal("0"):
                raise ApiClientAuthError(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="insufficient_balance",
                    message="Owner account available balance exhausted",
                    api_client_key_id=api_client_key.id,
                    api_client_key_name=api_client_key.name,
                    api_client_key_prefix=api_client_key.key_prefix,
                    user_account_id=api_client_key.owner_user_id,
                    user_account_name=owner_user_name,
                    remaining_balance=remaining_balance,
                    policy_snapshot_json=policy_snapshot_json,
                )
        ApiKeyService.enqueue_last_used_touch(api_client_key.id)
        return ApiClientAuthContext(
            api_client_key=api_client_key,
            route_context=route_context,
            remaining_balance=remaining_balance,
            policy_snapshot_json=policy_snapshot_json,
        )

    @staticmethod
    def authenticate_request(
        db: Session,
        authorization: str | None,
        *,
        request_path: str | None = None,
        source_ip: str | None = None,
    ) -> ApiClientAuthContext:
        """鉴权 API 调用请求，并返回后续路由与配额所需上下文。"""
        raw_key = ApiKeyService.parse_bearer_token(authorization)
        key_hash = ApiKeyService.hash_api_key(raw_key)
        cached_auth = ApiKeyAuthCache.get_by_hash(key_hash)
        if cached_auth is not None:
            return ApiKeyService._build_auth_context_from_cached_payload(
                key_hash=key_hash,
                cached_auth=cached_auth,
                request_path=request_path,
                source_ip=source_ip,
            )
        api_client_key = db.scalar(
            select(ApiClientKey)
            .options(selectinload(ApiClientKey.provider_bindings), selectinload(ApiClientKey.owner_user))
            .where(ApiClientKey.key_hash == key_hash)
        )
        if api_client_key is None:
            raise ApiClientAuthError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_api_key",
                message="Invalid api key",
                api_client_key_prefix=ApiKeyService.extract_key_prefix(raw_key),
            )
        allowed_provider_ids = [binding.provider_id for binding in api_client_key.provider_bindings]
        owner_user = db.get(UserAccount, api_client_key.owner_user_id) if api_client_key.owner_user_id is not None else None
        owner_user_id = owner_user.id if owner_user is not None else None
        owner_user_name = owner_user.username if owner_user is not None else None
        if owner_user is None:
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="owner_user_required",
                message="API Key must be bound to an owner account",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=None,
                policy_snapshot_json="{}",
            )
        user_quota_snapshot = UserQuotaService.get_realtime_usage_snapshot(db, user=owner_user) if owner_user is not None else None
        remaining_balance = None
        if user_quota_snapshot is not None and user_quota_snapshot.available_balance is not None:
            remaining_balance = float(user_quota_snapshot.available_balance)
        policy_snapshot = {
            "allowed_provider_ids": allowed_provider_ids,
            "allowed_model_names": loads_json(api_client_key.allowed_model_names_json, []),
            "allowed_endpoint_paths": loads_json(api_client_key.allowed_endpoint_paths_json, []),
            "allowed_source_ips": loads_json(api_client_key.allowed_source_ips_json, []),
            "preferred_provider_ids": loads_json(api_client_key.preferred_provider_ids_json, []),
            "preferred_region_tags": loads_json(api_client_key.preferred_region_tags_json, []),
            "latency_bias": api_client_key.latency_bias,
            "success_rate_bias": api_client_key.success_rate_bias,
            "cost_bias": api_client_key.cost_bias,
            "tenant_name": api_client_key.tenant_name,
            "project_name": api_client_key.project_name,
            "app_name": api_client_key.app_name,
            "environment_name": api_client_key.environment_name,
        }
        if owner_user is not None and user_quota_snapshot is not None:
            policy_snapshot["owner_user"] = UserQuotaService.serialize_policy(user=owner_user, snapshot=user_quota_snapshot)
        policy_snapshot_json = dumps_json(policy_snapshot)
        if not api_client_key.enabled:
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="key_disabled",
                message="Api key is disabled",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if api_client_key.expires_at is not None and api_client_key.expires_at <= datetime.utcnow():
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="key_expired",
                message="Api key is expired",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if request_path and not ApiKeyService.is_endpoint_allowed(api_client_key, request_path):
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="endpoint_not_allowed",
                message="Api key is not allowed to access this endpoint",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if source_ip and not ApiKeyService.is_source_ip_allowed(api_client_key, source_ip):
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="source_ip_not_allowed",
                message="Api key is not allowed to call from this source ip",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if owner_user is not None and user_quota_snapshot is not None:
            violation = UserQuotaService.evaluate_violation(user=owner_user, snapshot=user_quota_snapshot)
            if violation is not None:
                status_code = status.HTTP_403_FORBIDDEN if violation.code == "owner_user_disabled" else status.HTTP_429_TOO_MANY_REQUESTS
                raise ApiClientAuthError(
                    status_code=status_code,
                    code=violation.code,
                    message=violation.message,
                    api_client_key_id=api_client_key.id,
                    api_client_key_name=api_client_key.name,
                    api_client_key_prefix=api_client_key.key_prefix,
                    user_account_id=owner_user_id,
                    user_account_name=owner_user_name,
                    remaining_balance=(
                        float(user_quota_snapshot.available_balance)
                        if user_quota_snapshot.available_balance is not None
                        else remaining_balance
                    ),
                    policy_snapshot_json=policy_snapshot_json,
                )
        if not allowed_provider_ids:
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="no_authorized_provider",
                message="Api key has no authorized providers",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )

        route_setting = SettingService.get_cached()
        route_context = RoutePolicyContext(
            allowed_provider_ids=allowed_provider_ids,
            require_trusted_provider=bool(getattr(route_setting, "trusted_providers_only", False)),
            content_guard_required=True,
            preferred_provider_ids=loads_json(api_client_key.preferred_provider_ids_json, []),
            preferred_region_tags=loads_json(api_client_key.preferred_region_tags_json, []),
            latency_bias=api_client_key.latency_bias,
            success_rate_bias=api_client_key.success_rate_bias,
            cost_bias=api_client_key.cost_bias,
        )
        ApiKeyAuthCache.set_auth_context(
            key_hash=key_hash,
            api_key=api_client_key,
            owner_user=owner_user,
            owner_quota_snapshot=policy_snapshot.get("owner_user"),
            allowed_provider_ids=allowed_provider_ids,
            remaining_balance=remaining_balance,
            policy_snapshot_json=policy_snapshot_json,
        )
        ApiKeyService.enqueue_last_used_touch(api_client_key.id)
        return ApiClientAuthContext(
            api_client_key=api_client_key,
            route_context=route_context,
            remaining_balance=remaining_balance,
            policy_snapshot_json=policy_snapshot_json,
        )

    @staticmethod
    def try_authenticate_request_local_cache(
        authorization: str | None,
        *,
        request_path: str | None = None,
        source_ip: str | None = None,
    ) -> ApiClientAuthContext | None:
        raw_key = ApiKeyService.parse_bearer_token(authorization)
        key_hash = ApiKeyService.hash_api_key(raw_key)
        cached_auth = ApiKeyAuthCache.get_local_by_hash(key_hash)
        if cached_auth is None:
            return None
        return ApiKeyService._build_auth_context_from_cached_payload(
            key_hash=key_hash,
            cached_auth=cached_auth,
            request_path=request_path,
            source_ip=source_ip,
        )

    @staticmethod
    def is_endpoint_allowed(api_client_key: ApiClientKey, request_path: str) -> bool:
        allowed_paths = loads_json(api_client_key.allowed_endpoint_paths_json, [])
        if not allowed_paths:
            return True
        normalized_path = request_path.strip()
        if normalized_path in allowed_paths:
            return True
        if normalized_path.startswith("/v1/chat/completions/") and "/v1/chat/completions" in allowed_paths:
            return True
        if normalized_path.startswith("/v1/responses/") and "/v1/responses" in allowed_paths:
            return True
        if normalized_path.startswith("/v1/models/") and "/v1/models" in allowed_paths:
            return True
        if normalized_path.startswith("/v1/files/") and "/v1/files" in allowed_paths:
            return True
        if normalized_path in {"/v1/images/generations", "/v1/images/edits", "/v1/images/variations"} and "/v1/responses" in allowed_paths:
            return True
        return False

    @staticmethod
    def is_source_ip_allowed(api_client_key: ApiClientKey, source_ip: str) -> bool:
        allowed_items = loads_json(api_client_key.allowed_source_ips_json, [])
        if not allowed_items:
            return True
        try:
            ip_obj = ipaddress.ip_address(source_ip)
        except ValueError:
            return False
        for item in allowed_items:
            text = str(item).strip()
            if not text:
                continue
            try:
                if "/" in text:
                    if ip_obj in ipaddress.ip_network(text, strict=False):
                        return True
                elif ip_obj == ipaddress.ip_address(text):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def is_model_allowed(api_client_key: ApiClientKey, model_name: str | None) -> bool:
        allowed_models = loads_json(api_client_key.allowed_model_names_json, [])
        if not allowed_models or not model_name:
            return True
        return model_name in allowed_models

    @staticmethod
    async def validate_redis_rate_limits(auth_context: ApiClientAuthContext, *, request_path: str | None = None) -> None:
        api_client_key = auth_context.api_client_key
        has_api_key_limits = any(
            value is not None
            for value in (
                api_client_key.qps_limit,
                api_client_key.rpm_limit,
            )
        )
        if not has_api_key_limits:
            return
        try:
            await RateLimitService.check_api_key_limits(
                api_key_id=api_client_key.id,
                qps_limit=api_client_key.qps_limit,
                rpm_limit=api_client_key.rpm_limit,
            )
        except RateLimitExceededError as exc:
            raise ApiClientAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code=exc.code,
                message=exc.message,
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=api_client_key.owner_user.username if api_client_key.owner_user else None,
                remaining_balance=auth_context.remaining_balance,
                policy_snapshot_json=auth_context.policy_snapshot_json,
            ) from exc
        except Exception as exc:
            raise ApiClientAuthError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="redis_unavailable",
                message="Redis rate limit service is unavailable",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=api_client_key.owner_user_id,
                user_account_name=api_client_key.owner_user.username if api_client_key.owner_user else None,
                remaining_balance=auth_context.remaining_balance,
                policy_snapshot_json=auth_context.policy_snapshot_json,
            ) from exc

    @classmethod
    def enqueue_last_used_touch(cls, api_client_key_id: int | None) -> None:
        if api_client_key_id is None or not scheduler.running:
            return
        cls._pending_last_used_ids.add(int(api_client_key_id))
        if cls._last_used_flush_scheduled:
            return
        scheduler.add_job(
            cls.flush_pending_last_used_touches,
            "date",
            run_date=datetime.now() + timedelta(seconds=cls.LAST_USED_TOUCH_DELAY_SECONDS),
            id="api_key_last_used_flush",
            replace_existing=True,
            misfire_grace_time=30,
        )
        cls._last_used_flush_scheduled = True

    @classmethod
    def flush_pending_last_used_touches(cls) -> None:
        pending_ids = list(cls._pending_last_used_ids)
        cls._pending_last_used_ids.clear()
        cls._last_used_flush_scheduled = False
        if not pending_ids:
            return
        db = SessionLocal()
        try:
            touched_at = datetime.utcnow()
            api_keys = list(
                db.scalars(select(ApiClientKey).where(ApiClientKey.id.in_(pending_ids)))
            )
            for api_key in api_keys:
                api_key.last_used_at = touched_at
            if api_keys:
                db.commit()
        finally:
            db.close()

    @staticmethod
    def apply_token_usage(
        db: Session,
        *,
        api_client_key: ApiClientKey | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        auto_commit: bool = True,
    ) -> None:
        if api_client_key is None:
            return
        persistent_api_client_key = db.get(ApiClientKey, api_client_key.id) if api_client_key.id is not None else None
        target = persistent_api_client_key or api_client_key
        changed = False
        if prompt_tokens is not None:
            target.prompt_tokens_used += max(0, prompt_tokens)
            changed = True
        if completion_tokens is not None:
            target.completion_tokens_used += max(0, completion_tokens)
            changed = True
        if total_tokens is not None:
            target.total_tokens_used += max(0, total_tokens)
            changed = True
        elif prompt_tokens is not None or completion_tokens is not None:
            target.total_tokens_used += max(0, (prompt_tokens or 0) + (completion_tokens or 0))
            changed = True
        if changed:
            target.last_used_at = datetime.utcnow()
            if auto_commit:
                db.commit()

    @staticmethod
    def reconcile_usage_counters(
        db: Session,
        *,
        api_client_key: ApiClientKey | None = None,
        api_client_key_id: int | None = None,
        auto_commit: bool = False,
    ) -> ApiClientKey | None:
        target_id = api_client_key_id or (api_client_key.id if api_client_key is not None else None)
        if target_id is None:
            return None
        target = db.get(ApiClientKey, target_id)
        if target is None:
            return None
        now = datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        route_filters = (
            RequestLog.api_client_key_id == target_id,
            LogService._route_traffic_expr(),
            LogService._non_model_list_request_expr(),
        )
        totals = db.execute(
            select(
                func.count(RequestLog.id).label("total_requests"),
                func.coalesce(
                    func.sum(case((RequestLog.created_at >= day_start, 1), else_=0)),
                    0,
                ).label("day_requests"),
                func.coalesce(
                    func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.prompt_tokens, 0)), else_=0)),
                    0,
                ).label("prompt_tokens_used"),
                func.coalesce(
                    func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.completion_tokens, 0)), else_=0)),
                    0,
                ).label("completion_tokens_used"),
                func.coalesce(
                    func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.total_tokens, 0)), else_=0)),
                    0,
                ).label("total_tokens_used"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (RequestLog.success.is_(True)) & (RequestLog.created_at >= day_start),
                                func.coalesce(RequestLog.total_tokens, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("day_tokens_used"),
                func.coalesce(
                    func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.total_cost, 0)), else_=0)),
                    0,
                ).label("total_cost_used"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (RequestLog.success.is_(True)) & (RequestLog.created_at >= day_start),
                                func.coalesce(RequestLog.total_cost, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("day_cost_used"),
            )
            .where(*route_filters)
        ).one()
        target.prompt_tokens_used = int(totals.prompt_tokens_used or 0)
        target.completion_tokens_used = int(totals.completion_tokens_used or 0)
        target.total_tokens_used = int(totals.total_tokens_used or 0)
        target.total_cost_used = BillingService.to_decimal(totals.total_cost_used)
        account_totals = None
        if target.owner_user_id is not None:
            owned_key_ids = list(
                db.scalars(
                    select(ApiClientKey.id).where(ApiClientKey.owner_user_id == target.owner_user_id)
                )
            )
            account_scope = [RequestLog.user_account_id == target.owner_user_id]
            if owned_key_ids:
                account_scope.append(RequestLog.api_client_key_id.in_(owned_key_ids))
            account_totals = db.execute(
                select(
                    func.count(RequestLog.id).label("total_requests"),
                    func.coalesce(func.sum(case((RequestLog.created_at >= day_start, 1), else_=0)), 0).label("day_requests"),
                    func.coalesce(func.sum(case((RequestLog.created_at >= month_start, 1), else_=0)), 0).label("month_requests"),
                    func.coalesce(
                        func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.total_tokens, 0)), else_=0)),
                        0,
                    ).label("total_tokens"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    (RequestLog.success.is_(True)) & (RequestLog.created_at >= day_start),
                                    func.coalesce(RequestLog.total_tokens, 0),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("day_tokens"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    (RequestLog.success.is_(True)) & (RequestLog.created_at >= month_start),
                                    func.coalesce(RequestLog.total_tokens, 0),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("month_tokens"),
                    func.coalesce(
                        func.sum(case((RequestLog.success.is_(True), func.coalesce(RequestLog.total_cost, 0)), else_=0)),
                        0,
                    ).label("total_cost"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    (RequestLog.success.is_(True)) & (RequestLog.created_at >= day_start),
                                    func.coalesce(RequestLog.total_cost, 0),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("day_cost"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    (RequestLog.success.is_(True)) & (RequestLog.created_at >= month_start),
                                    func.coalesce(RequestLog.total_cost, 0),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("month_cost"),
                ).where(
                    LogService._route_traffic_expr(),
                    LogService._non_model_list_request_expr(),
                    or_(*account_scope),
                )
            ).one()
        if auto_commit:
            db.commit()
            db.refresh(target)
            RateLimitService.reset_realtime_quota_counters(
                api_key_id=target.id,
                api_key_total_tokens_used=target.total_tokens_used,
                api_key_day_tokens_used=int(totals.day_tokens_used or 0),
                api_key_total_cost_used=target.total_cost_used,
                api_key_day_cost_used=BillingService.to_decimal(totals.day_cost_used),
                api_key_day_requests=int(totals.day_requests or 0),
                account_id=target.owner_user_id,
                account_total_tokens_used=int(account_totals.total_tokens or 0) if account_totals is not None else None,
                account_day_tokens_used=int(account_totals.day_tokens or 0) if account_totals is not None else None,
                account_month_tokens_used=int(account_totals.month_tokens or 0) if account_totals is not None else None,
                account_total_cost_used=BillingService.to_decimal(account_totals.total_cost) if account_totals is not None else None,
                account_day_cost_used=BillingService.to_decimal(account_totals.day_cost) if account_totals is not None else None,
                account_month_cost_used=BillingService.to_decimal(account_totals.month_cost) if account_totals is not None else None,
                account_total_requests=int(account_totals.total_requests or 0) if account_totals is not None else None,
                account_day_requests=int(account_totals.day_requests or 0) if account_totals is not None else None,
                account_month_requests=int(account_totals.month_requests or 0) if account_totals is not None else None,
            )
            ApiKeyAuthCache.invalidate_api_key(target.id, target.key_hash)
            ApiKeyAuthCache.invalidate_user(target.owner_user_id)
        return target


def _authenticate_request_with_scoped_session(
    authorization: str | None,
    *,
    request_path: str | None,
    source_ip: str | None,
) -> ApiClientAuthContext:
    db = SessionLocal()
    try:
        return ApiKeyService.authenticate_request(
            db,
            authorization,
            request_path=request_path,
            source_ip=source_ip,
        )
    finally:
        db.close()


async def require_api_client_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ApiClientAuthContext:
    source_ip = ApiKeyService.extract_source_ip(request)
    auth_context = ApiKeyService.try_authenticate_request_local_cache(
        authorization,
        request_path=request.url.path,
        source_ip=source_ip,
    )
    if auth_context is None:
        auth_context = await run_in_threadpool(
            _authenticate_request_with_scoped_session,
            authorization,
            request_path=request.url.path,
            source_ip=source_ip,
        )
    await ApiKeyService.validate_redis_rate_limits(auth_context, request_path=request.url.path)
    return auth_context
