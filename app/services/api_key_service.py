from dataclasses import dataclass
from datetime import datetime
import hashlib
import secrets

from fastapi import Depends, Header, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.api_client_key import ApiClientKey
from app.services.router_service import RoutePolicyContext
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
        remaining_tokens: int | None = None,
        policy_snapshot_json: str | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.api_client_key_id = api_client_key_id
        self.api_client_key_name = api_client_key_name
        self.api_client_key_prefix = api_client_key_prefix
        self.remaining_tokens = remaining_tokens
        self.policy_snapshot_json = policy_snapshot_json
        super().__init__(message)


@dataclass(slots=True)
class ApiClientAuthContext:
    api_client_key: ApiClientKey
    route_context: RoutePolicyContext
    remaining_tokens: int | None
    policy_snapshot_json: str


class ApiKeyService:
    DEFAULT_KEY_PREFIX = "sk-aotu-"

    @staticmethod
    def generate_api_key() -> str:
        return f"{ApiKeyService.DEFAULT_KEY_PREFIX}{secrets.token_urlsafe(24)}"

    @staticmethod
    def extract_key_prefix(raw_key: str) -> str:
        return raw_key[:16]

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        normalized = raw_key.strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

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
            .options(selectinload(ApiClientKey.provider_bindings))
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
        policy_snapshot_json = dumps_json(
            {
                "route_mode": api_client_key.route_mode,
                "default_provider_id": default_provider_id,
                "manual_allow_fallback": api_client_key.manual_allow_fallback,
                "allowed_provider_ids": allowed_provider_ids,
            }
        )
        if not api_client_key.enabled:
            raise ApiClientAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="key_disabled",
                message="Api key is disabled",
                api_client_key_id=api_client_key.id,
                api_client_key_name=api_client_key.name,
                api_client_key_prefix=api_client_key.key_prefix,
                remaining_tokens=remaining_tokens,
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
                remaining_tokens=remaining_tokens,
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
                remaining_tokens=remaining_tokens,
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
                remaining_tokens=remaining_tokens,
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
            db.commit()


def require_api_client_auth(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiClientAuthContext:
    return ApiKeyService.authenticate_request(db, authorization)
