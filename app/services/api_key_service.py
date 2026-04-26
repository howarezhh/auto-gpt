from dataclasses import dataclass
from datetime import datetime
import base64
import hashlib
import re
import secrets
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Header, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.router_service import RoutePolicyContext
from app.services.user_quota_service import UserQuotaService
from app.utils.json_utils import dumps_json


class ApiClientAuthError(Exception):
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
        remaining_tokens: int | None = None,
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
        self.remaining_tokens = remaining_tokens
        self.remaining_balance = remaining_balance
        self.policy_snapshot_json = policy_snapshot_json
        super().__init__(message)


@dataclass(slots=True)
class ApiClientAuthContext:
    api_client_key: ApiClientKey
    route_context: RoutePolicyContext
    remaining_tokens: int | None
    remaining_balance: float | None
    policy_snapshot_json: str


class ApiKeyService:
    DEFAULT_KEY_PREFIX = "sk-aotu-"
    MIN_KEY_LENGTH = 24
    MAX_KEY_LENGTH = 128
    KEY_PATTERN = re.compile(r"^[A-Za-z0-9\-_]+$")

    @staticmethod
    def generate_api_key() -> str:
        return f"{ApiKeyService.DEFAULT_KEY_PREFIX}{secrets.token_urlsafe(24)}"

    @staticmethod
    def normalize_raw_key(raw_key: str) -> str:
        return raw_key.strip()

    @staticmethod
    def extract_key_prefix(raw_key: str) -> str:
        return raw_key[:16]

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        normalized = ApiKeyService.normalize_raw_key(raw_key)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_raw_api_key(raw_key: str) -> str:
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
        if raw_key is None or not str(raw_key).strip():
            return ApiKeyService.generate_api_key()
        return ApiKeyService.validate_raw_api_key(str(raw_key))

    @staticmethod
    def _get_fernet() -> Fernet:
        settings = get_settings()
        secret = settings.api_key_encryption_secret or settings.session_secret_key
        derived = hashlib.sha256(secret.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    @staticmethod
    def encrypt_raw_api_key(raw_key: str) -> str:
        normalized = ApiKeyService.normalize_raw_key(raw_key)
        return ApiKeyService._get_fernet().encrypt(normalized.encode("utf-8")).decode("utf-8")

    @staticmethod
    def decrypt_raw_api_key(ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return ApiKeyService._get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return None

    @staticmethod
    def mask_key_prefix(key_prefix: str) -> str:
        if len(key_prefix) <= 8:
            return "******"
        return f"{key_prefix[:6]}...{key_prefix[-4:]}"

    @staticmethod
    def parse_bearer_token(authorization: str | None) -> str:
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
    def authenticate_request(db: Session, authorization: str | None) -> ApiClientAuthContext:
        raw_key = ApiKeyService.parse_bearer_token(authorization)
        key_hash = ApiKeyService.hash_api_key(raw_key)
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
        remaining_tokens = None
        if api_client_key.token_limit_total is not None:
            remaining_tokens = max(0, api_client_key.token_limit_total - api_client_key.total_tokens_used)
        allowed_provider_ids = [binding.provider_id for binding in api_client_key.provider_bindings]
        default_provider_id = api_client_key.default_provider_id
        if default_provider_id not in allowed_provider_ids:
            default_provider_id = None
        owner_user = db.get(UserAccount, api_client_key.owner_user_id) if api_client_key.owner_user_id is not None else None
        owner_user_id = owner_user.id if owner_user is not None else None
        owner_user_name = owner_user.username if owner_user is not None else None
        user_quota_snapshot = UserQuotaService.get_usage_snapshot(db, user=owner_user) if owner_user is not None else None
        remaining_balance = None
        if user_quota_snapshot is not None and user_quota_snapshot.available_balance is not None:
            remaining_balance = float(user_quota_snapshot.available_balance)
        elif api_client_key.balance_amount is not None:
            remaining_balance = float(api_client_key.balance_amount)
        policy_snapshot = {
            "route_mode": api_client_key.route_mode,
            "default_provider_id": default_provider_id,
            "manual_allow_fallback": api_client_key.manual_allow_fallback,
            "allowed_provider_ids": allowed_provider_ids,
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
                remaining_tokens=remaining_tokens,
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
                remaining_tokens=remaining_tokens,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if (
            api_client_key.token_limit_total is not None
            and api_client_key.total_tokens_used >= api_client_key.token_limit_total
        ):
            raise ApiClientAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="insufficient_quota",
                message="Api key token quota exhausted",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_tokens=remaining_tokens,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if api_client_key.cost_limit_total is not None and Decimal(str(api_client_key.total_cost_used or 0)) >= Decimal(str(api_client_key.cost_limit_total)):
            raise ApiClientAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="insufficient_quota",
                message="Api key billing quota exhausted",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_tokens=remaining_tokens,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )
        if owner_user is None and api_client_key.balance_amount is not None and Decimal(str(api_client_key.balance_amount)) <= Decimal("0"):
            raise ApiClientAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="insufficient_balance",
                message="Api key balance exhausted",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                user_account_id=owner_user_id,
                user_account_name=owner_user_name,
                remaining_tokens=remaining_tokens,
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
                    remaining_tokens=remaining_tokens,
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
                remaining_tokens=remaining_tokens,
                remaining_balance=remaining_balance,
                policy_snapshot_json=policy_snapshot_json,
            )

        route_context = RoutePolicyContext(
            route_mode=api_client_key.route_mode,
            default_provider_id=default_provider_id,
            manual_allow_fallback=api_client_key.manual_allow_fallback,
            allowed_provider_ids=allowed_provider_ids,
        )
        api_client_key.last_used_at = datetime.utcnow()
        db.commit()
        db.refresh(api_client_key)
        return ApiClientAuthContext(
            api_client_key=api_client_key,
            route_context=route_context,
            remaining_tokens=remaining_tokens,
            remaining_balance=remaining_balance,
            policy_snapshot_json=policy_snapshot_json,
        )

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
        totals = db.execute(
            select(
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0).label("prompt_tokens_used"),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0).label("completion_tokens_used"),
                func.coalesce(func.sum(RequestLog.total_tokens), 0).label("total_tokens_used"),
            )
            .where(RequestLog.api_client_key_id == target_id)
            .where(RequestLog.success.is_(True))
            .where(RequestLog.request_path != "/v1/models")
        ).one()
        target.prompt_tokens_used = int(totals.prompt_tokens_used or 0)
        target.completion_tokens_used = int(totals.completion_tokens_used or 0)
        target.total_tokens_used = int(totals.total_tokens_used or 0)
        if auto_commit:
            db.commit()
        return target


def require_api_client_auth(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiClientAuthContext:
    return ApiKeyService.authenticate_request(db, authorization)
