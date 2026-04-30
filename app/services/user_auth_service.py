from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime
from urllib.parse import quote, urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from fastapi import Depends, HTTPException, Request, status

from app.database import get_db
from app.models.api_client_key import ApiClientKey
from app.models.user_account import UserAccount
from app.services.api_key_auth_cache import ApiKeyAuthCache


SESSION_USER_ID_KEY = "session_user_id"
PASSWORD_HASH_ITERATIONS = 120000
USER_ROLE_ADMIN = "admin"
USER_ROLE_USER = "user"


class UserAuthService:
    ADMIN_PORTAL_PREFIXES = (
        "/providers",
        "/models",
        "/settings",
        "/playground",
        "/docs",
        "/api-keys",
        "/logs",
        "/conversations",
        "/users",
    )

    @staticmethod
    def normalize_username(value: str) -> str:
        return value.strip().lower()

    @staticmethod
    def normalize_email(value: str) -> str:
        return value.strip().lower()

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
        salt_text = base64.b64encode(salt).decode("utf-8")
        hash_text = base64.b64encode(derived).decode("utf-8")
        return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt_text}${hash_text}"

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            algorithm, iteration_text, salt_text, hash_text = password_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            salt = base64.b64decode(salt_text.encode("utf-8"))
            expected = base64.b64decode(hash_text.encode("utf-8"))
            derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iteration_text))
            return hmac.compare_digest(derived, expected)
        except Exception:
            return False

    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> UserAccount | None:
        return db.get(UserAccount, user_id)

    @staticmethod
    def get_user_by_login(db: Session, identifier: str) -> UserAccount | None:
        normalized = identifier.strip().lower()
        return db.scalar(
            select(UserAccount).where(
                or_(
                    func.lower(UserAccount.username) == normalized,
                    func.lower(UserAccount.email) == normalized,
                )
            )
        )

    @staticmethod
    def authenticate(db: Session, identifier: str, password: str) -> UserAccount | None:
        user = UserAuthService.get_user_by_login(db, identifier)
        if user is None or not user.enabled:
            return None
        if not UserAuthService.verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def create_user(
        db: Session,
        *,
        username: str,
        email: str,
        password: str,
        role: str = USER_ROLE_USER,
        enabled: bool = True,
        created_by_user_id: int | None = None,
    ) -> UserAccount:
        normalized_username = UserAuthService.normalize_username(username)
        normalized_email = UserAuthService.normalize_email(email)
        UserAuthService.validate_new_user(
            db,
            username=normalized_username,
            email=normalized_email,
            password=password,
        )
        user = UserAccount(
            username=normalized_username,
            email=normalized_email,
            password_hash=UserAuthService.hash_password(password),
            role=role,
            enabled=enabled,
            created_by_user_id=created_by_user_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def validate_new_user(db: Session, *, username: str, email: str, password: str) -> None:
        if len(username) < 3:
            raise ValueError("用户名至少 3 个字符")
        if len(password) < 8:
            raise ValueError("密码至少 8 个字符")
        existing = db.scalar(
            select(UserAccount.id).where(
                or_(
                    func.lower(UserAccount.username) == username,
                    func.lower(UserAccount.email) == email,
                )
            )
        )
        if existing is not None:
            raise ValueError("用户名或邮箱已存在")

    @staticmethod
    def update_password(db: Session, user: UserAccount, *, password: str) -> UserAccount:
        if len(password) < 8:
            raise ValueError("密码至少 8 个字符")
        user.password_hash = UserAuthService.hash_password(password)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def update_profile(
        db: Session,
        user: UserAccount,
        *,
        username: str,
        email: str,
    ) -> UserAccount:
        normalized_username = UserAuthService.normalize_username(username)
        normalized_email = UserAuthService.normalize_email(email)
        if len(normalized_username) < 3:
            raise ValueError("用户名至少 3 个字符")
        existing = db.scalar(
            select(UserAccount.id).where(
                or_(
                    func.lower(UserAccount.username) == normalized_username,
                    func.lower(UserAccount.email) == normalized_email,
                ),
                UserAccount.id != user.id,
            )
        )
        if existing is not None:
            raise ValueError("用户名或邮箱已存在")
        user.username = normalized_username
        user.email = normalized_email
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def set_enabled(db: Session, user: UserAccount, enabled: bool) -> UserAccount:
        user.enabled = enabled
        db.commit()
        db.refresh(user)
        ApiKeyAuthCache.invalidate_user(user.id)
        return user

    @staticmethod
    def set_role(db: Session, user: UserAccount, role: str) -> UserAccount:
        user.role = role
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def delete_user(db: Session, user: UserAccount) -> None:
        deleted_user_id = user.id
        deleted_key_refs: list[tuple[int, str]] = []
        owned_api_keys = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.owner_user_id == user.id)
            )
        )
        for item in owned_api_keys:
            deleted_key_refs.append((item.id, item.key_hash))
            db.delete(item)

        created_users = list(
            db.scalars(
                select(UserAccount).where(UserAccount.created_by_user_id == user.id)
            )
        )
        for item in created_users:
            item.created_by_user_id = None

        db.delete(user)
        db.commit()
        for api_key_id, key_hash in deleted_key_refs:
            ApiKeyAuthCache.invalidate_api_key(api_key_id, key_hash)
        ApiKeyAuthCache.invalidate_user(deleted_user_id)

    @staticmethod
    def list_users(db: Session) -> list[UserAccount]:
        return list(db.scalars(select(UserAccount).order_by(UserAccount.id.desc())))

    @staticmethod
    def has_any_user(db: Session) -> bool:
        return (db.scalar(select(func.count()).select_from(UserAccount)) or 0) > 0

    @staticmethod
    def has_any_admin(db: Session) -> bool:
        return (
            db.scalar(select(func.count()).select_from(UserAccount).where(UserAccount.role == USER_ROLE_ADMIN)) or 0
        ) > 0

    @staticmethod
    def login_user(request: Request, user: UserAccount) -> None:
        request.session.clear()
        request.session[SESSION_USER_ID_KEY] = user.id

    @staticmethod
    def logout_user(request: Request) -> None:
        request.session.clear()

    @staticmethod
    def get_current_user(request: Request, db: Session) -> UserAccount | None:
        session_user_id = request.session.get(SESSION_USER_ID_KEY)
        if not session_user_id:
            return None
        user = UserAuthService.get_user_by_id(db, int(session_user_id))
        if user is None or not user.enabled:
            request.session.clear()
            return None
        return user

    @staticmethod
    def normalize_next_path(next_path: str | None) -> str | None:
        text = (next_path or "").strip()
        if not text:
            return None
        parsed = urlsplit(text)
        if parsed.scheme or parsed.netloc:
            return None
        if not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return None
        normalized = parsed.path
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        return normalized

    @staticmethod
    def get_role_home_path(role: str) -> str:
        return "/" if role == USER_ROLE_ADMIN else "/user"

    @staticmethod
    def _match_path(path: str, prefix: str) -> bool:
        return path == prefix or path.startswith(f"{prefix}/")

    @staticmethod
    def is_route_allowed_for_role(path: str, role: str) -> bool:
        normalized = UserAuthService.normalize_next_path(path)
        if not normalized:
            return False
        route_path = urlsplit(normalized).path
        if role == USER_ROLE_ADMIN:
            return route_path == "/" or any(UserAuthService._match_path(route_path, prefix) for prefix in UserAuthService.ADMIN_PORTAL_PREFIXES)
        if role == USER_ROLE_USER:
            return UserAuthService._match_path(route_path, "/user")
        return False

    @staticmethod
    def resolve_post_login_path(role: str, next_path: str | None = None) -> str:
        normalized = UserAuthService.normalize_next_path(next_path)
        if normalized and UserAuthService.is_route_allowed_for_role(normalized, role):
            return normalized
        return UserAuthService.get_role_home_path(role)

    @staticmethod
    def build_login_redirect_path(request: Request) -> str:
        current_path = request.url.path
        if request.url.query:
            current_path = f"{current_path}?{request.url.query}"
        return f"/login?next={quote(current_path, safe='')}"


def require_admin_api_user(
    request: Request,
    db: Session = Depends(get_db),
) -> UserAccount:
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录管理后台")
    if user.role != USER_ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号无管理后台权限")
    return user


def require_session_api_user(
    request: Request,
    db: Session = Depends(get_db),
) -> UserAccount:
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user
