from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.provider import Provider
from app.models.user_account import UserAccount
from app.schemas.provider import (
    ProviderCredentialBatchUpdateRequest,
    ProviderCredentialBatchUpdateResponse,
    ProviderCredentialExternalItem,
    ProviderCredentialListRequest,
    ProviderCredentialListResponse,
    ProviderCredentialUpdateRequest,
    ProviderCredentialUpdateResult,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.provider_service import ProviderService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


router = APIRouter(prefix="/api/provider-credentials", tags=["provider-credentials"])


def _authenticate_admin(db: Session, *, username: str, password: str) -> UserAccount:
    user = UserAuthService.authenticate(db, username, password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    if user.role != USER_ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号无提供商密钥管理权限")
    return user


def _serialize_provider_credential(provider: Provider) -> ProviderCredentialExternalItem:
    api_key = provider.api_key or ""
    return ProviderCredentialExternalItem(
        id=provider.id,
        name=provider.name,
        base_url=provider.base_url,
        enabled=provider.enabled,
        group_name=provider.group_name,
        region_tag=provider.region_tag,
        api_key=api_key,
        masked_api_key=ProviderService.mask_api_key(api_key),
        credential_hint=provider.credential_hint,
        credential_rotated_at=provider.credential_rotated_at,
        updated_at=provider.updated_at,
    )


def _find_provider_for_update(db: Session, *, provider_id: int | None, provider_name: str | None) -> Provider | None:
    if provider_id is not None:
        return ProviderService.get_provider(db, provider_id)
    normalized_name = str(provider_name or "").strip()
    if not normalized_name:
        return None
    return db.scalar(select(Provider).where(func.lower(Provider.name) == normalized_name.lower()))


def _record_provider_credential_audit(
    db: Session,
    *,
    request: Request,
    actor: UserAccount,
    provider: Provider,
    action: str,
    credential_hint: str | None,
    old_api_key: str,
    old_credential_hint: str | None,
) -> None:
    new_api_key = provider.api_key or ""
    AdminAuditService.create_log(
        db,
        actor_user_id=actor.id,
        actor_username=actor.username,
        action=action,
        entity_type="provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"外部接口更新提供商密钥：{provider.name}",
        before={
            "masked_api_key": ProviderService.mask_api_key(old_api_key),
            "credential_hint": old_credential_hint,
        },
        after={
            "masked_api_key": ProviderService.mask_api_key(new_api_key),
            "credential_hint": credential_hint,
            "credential_rotated_at": provider.credential_rotated_at.isoformat() if provider.credential_rotated_at else None,
        },
        changed_fields={
            "api_key": ProviderService.mask_api_key(new_api_key),
            "credential_hint": credential_hint,
        },
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level="high",
    )


@router.post("/list", response_model=ProviderCredentialListResponse)
def list_provider_credentials(
    payload: ProviderCredentialListRequest,
    db: Session = Depends(get_db),
) -> ProviderCredentialListResponse:
    _authenticate_admin(db, username=payload.username, password=payload.password)
    filters = []
    if payload.provider_ids:
        filters.append(Provider.id.in_(payload.provider_ids))
    if payload.provider_names:
        normalized_names = [item.lower() for item in payload.provider_names]
        filters.append(func.lower(Provider.name).in_(normalized_names))
    stmt = select(Provider)
    if filters:
        stmt = stmt.where(or_(*filters))
    providers = list(db.scalars(stmt.order_by(Provider.priority.asc(), Provider.id.asc())))
    return ProviderCredentialListResponse(
        total=len(providers),
        providers=[_serialize_provider_credential(provider) for provider in providers],
    )


@router.post("/update", response_model=ProviderCredentialUpdateResult)
def update_provider_credential(
    payload: ProviderCredentialUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderCredentialUpdateResult:
    actor = _authenticate_admin(db, username=payload.username, password=payload.password)
    provider = _find_provider_for_update(db, provider_id=payload.provider_id, provider_name=payload.provider_name)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="提供商不存在")
    old_api_key = provider.api_key or ""
    old_credential_hint = provider.credential_hint
    provider = ProviderService.rotate_provider_credential(
        db,
        provider,
        api_key=payload.api_key,
        credential_hint=payload.credential_hint,
    )
    _record_provider_credential_audit(
        db,
        request=request,
        actor=actor,
        provider=provider,
        action="external_update_provider_credential",
        credential_hint=payload.credential_hint,
        old_api_key=old_api_key,
        old_credential_hint=old_credential_hint,
    )
    return ProviderCredentialUpdateResult(
        success=True,
        provider_id=provider.id,
        provider_name=provider.name,
        message="提供商密钥已更新",
        provider=_serialize_provider_credential(provider),
    )


@router.post("/batch-update", response_model=ProviderCredentialBatchUpdateResponse)
def batch_update_provider_credentials(
    payload: ProviderCredentialBatchUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderCredentialBatchUpdateResponse:
    actor = _authenticate_admin(db, username=payload.username, password=payload.password)
    results: list[ProviderCredentialUpdateResult] = []
    for item in payload.items:
        provider = _find_provider_for_update(db, provider_id=item.provider_id, provider_name=item.provider_name)
        if provider is None:
            results.append(
                ProviderCredentialUpdateResult(
                    success=False,
                    provider_id=item.provider_id,
                    provider_name=item.provider_name,
                    message="提供商不存在",
                )
            )
            continue
        old_api_key = provider.api_key or ""
        old_credential_hint = provider.credential_hint
        provider = ProviderService.rotate_provider_credential(
            db,
            provider,
            api_key=item.api_key,
            credential_hint=item.credential_hint,
        )
        _record_provider_credential_audit(
            db,
            request=request,
            actor=actor,
            provider=provider,
            action="external_batch_update_provider_credential",
            credential_hint=item.credential_hint,
            old_api_key=old_api_key,
            old_credential_hint=old_credential_hint,
        )
        results.append(
            ProviderCredentialUpdateResult(
                success=True,
                provider_id=provider.id,
                provider_name=provider.name,
                message="提供商密钥已更新",
                provider=_serialize_provider_credential(provider),
            )
        )
    success_count = sum(1 for item in results if item.success)
    return ProviderCredentialBatchUpdateResponse(
        total=len(results),
        success_count=success_count,
        failed_count=len(results) - success_count,
        results=results,
    )
