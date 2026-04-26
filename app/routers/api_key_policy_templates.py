from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.api_key_policy_template import (
    ApiKeyPolicyTemplateCreate,
    ApiKeyPolicyTemplateOut,
    ApiKeyPolicyTemplateUpdate,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.api_key_policy_template_service import ApiKeyPolicyTemplateService
from app.services.user_auth_service import require_admin_api_user


router = APIRouter(prefix="/api/api-key-policy-templates", tags=["api-key-policy-templates"])


@router.get("", response_model=list[ApiKeyPolicyTemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[ApiKeyPolicyTemplateOut]:
    return ApiKeyPolicyTemplateService.list_templates(db)


@router.post("", response_model=ApiKeyPolicyTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: ApiKeyPolicyTemplateCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyPolicyTemplateOut:
    item = ApiKeyPolicyTemplateService.create_template(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="api_key_policy_template",
        entity_id=item.id,
        entity_name=item.name,
        summary=f"创建 API Key 策略模板 {item.name}",
        detail=payload.model_dump(),
    )
    return item


@router.put("/{template_id}", response_model=ApiKeyPolicyTemplateOut)
def update_template(
    template_id: int,
    payload: ApiKeyPolicyTemplateUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyPolicyTemplateOut:
    item = ApiKeyPolicyTemplateService.get_template(db, template_id)
    if item is None:
        raise HTTPException(status_code=404, detail="策略模板不存在")
    serialized = ApiKeyPolicyTemplateService.update_template(db, item, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="api_key_policy_template",
        entity_id=serialized.id,
        entity_name=serialized.name,
        summary=f"更新 API Key 策略模板 {serialized.name}",
        detail=payload.model_dump(exclude_unset=True),
    )
    return serialized


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    item = ApiKeyPolicyTemplateService.get_template(db, template_id)
    if item is None:
        raise HTTPException(status_code=404, detail="策略模板不存在")
    template_name = item.name
    ApiKeyPolicyTemplateService.delete_template(db, item)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="api_key_policy_template",
        entity_id=template_id,
        entity_name=template_name,
        summary=f"删除 API Key 策略模板 {template_name}",
    )
    return {"message": "deleted"}
