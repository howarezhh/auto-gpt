from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key_policy_template import ApiKeyPolicyTemplate
from app.models.model_catalog import ModelCatalog
from app.schemas.api_key import ApiKeyCreate, ApiKeyUpdate
from app.schemas.api_key_policy_template import (
    ApiKeyPolicyTemplateCreate,
    ApiKeyPolicyTemplateOut,
    ApiKeyPolicyTemplateUpdate,
)
from app.utils.json_utils import dumps_json, loads_json


class ApiKeyPolicyTemplateService:
    @staticmethod
    def list_templates(db: Session) -> list[ApiKeyPolicyTemplateOut]:
        items = list(
            db.scalars(
                select(ApiKeyPolicyTemplate).order_by(ApiKeyPolicyTemplate.enabled.desc(), ApiKeyPolicyTemplate.name.asc())
            )
        )
        return [ApiKeyPolicyTemplateService.serialize(item) for item in items]

    @staticmethod
    def get_template(db: Session, template_id: int) -> ApiKeyPolicyTemplate | None:
        return db.get(ApiKeyPolicyTemplate, template_id)

    @staticmethod
    def create_template(db: Session, payload: ApiKeyPolicyTemplateCreate) -> ApiKeyPolicyTemplateOut:
        ApiKeyPolicyTemplateService._validate_model_names(db, payload.allowed_model_names)
        item = ApiKeyPolicyTemplate(
            name=payload.name,
            remark=payload.remark,
            enabled=payload.enabled,
            route_mode=payload.route_mode,
            default_provider_id=payload.default_provider_id,
            manual_allow_fallback=payload.manual_allow_fallback,
            token_limit_total=payload.token_limit_total,
            cost_limit_total=payload.cost_limit_total,
            expires_in_days=payload.expires_in_days,
            allowed_provider_ids_json=dumps_json(payload.allowed_provider_ids),
            allowed_model_names_json=dumps_json(payload.allowed_model_names),
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return ApiKeyPolicyTemplateService.serialize(item)

    @staticmethod
    def update_template(db: Session, item: ApiKeyPolicyTemplate, payload: ApiKeyPolicyTemplateUpdate) -> ApiKeyPolicyTemplateOut:
        data = payload.model_dump(exclude_unset=True)
        if "allowed_model_names" in data:
            ApiKeyPolicyTemplateService._validate_model_names(db, data["allowed_model_names"] or [])
        for field, value in data.items():
            if field == "allowed_provider_ids":
                item.allowed_provider_ids_json = dumps_json(value or [])
                continue
            if field == "allowed_model_names":
                item.allowed_model_names_json = dumps_json(value or [])
                continue
            setattr(item, field, value)
        db.commit()
        db.refresh(item)
        return ApiKeyPolicyTemplateService.serialize(item)

    @staticmethod
    def delete_template(db: Session, item: ApiKeyPolicyTemplate) -> None:
        db.delete(item)
        db.commit()

    @staticmethod
    def serialize(item: ApiKeyPolicyTemplate) -> ApiKeyPolicyTemplateOut:
        return ApiKeyPolicyTemplateOut(
            id=item.id,
            name=item.name,
            remark=item.remark,
            enabled=item.enabled,
            route_mode=item.route_mode,
            default_provider_id=item.default_provider_id,
            manual_allow_fallback=item.manual_allow_fallback,
            token_limit_total=item.token_limit_total,
            cost_limit_total=float(item.cost_limit_total) if item.cost_limit_total is not None else None,
            expires_in_days=item.expires_in_days,
            allowed_provider_ids=list(loads_json(item.allowed_provider_ids_json, [])),
            allowed_model_names=list(loads_json(item.allowed_model_names_json, [])),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    @staticmethod
    def build_create_payload_from_template(
        template: ApiKeyPolicyTemplateOut,
        *,
        name: str,
        raw_api_key: str | None,
        remark: str | None,
        enabled: bool,
        owner_user_id: int | None,
        balance_amount: float | None = None,
    ) -> ApiKeyCreate:
        expires_at = (
            datetime.utcnow() + timedelta(days=template.expires_in_days)
            if template.expires_in_days and template.expires_in_days > 0
            else None
        )
        return ApiKeyCreate(
            name=name,
            raw_api_key=raw_api_key,
            remark=remark,
            enabled=enabled,
            expires_at=expires_at,
            token_limit_total=template.token_limit_total,
            cost_limit_total=template.cost_limit_total,
            balance_amount=balance_amount,
            route_mode=template.route_mode,
            default_provider_id=template.default_provider_id,
            owner_user_id=owner_user_id,
            manual_allow_fallback=template.manual_allow_fallback,
            allowed_provider_ids=template.allowed_provider_ids,
            allowed_model_names=template.allowed_model_names,
        )

    @staticmethod
    def build_update_payload_from_template(
        template: ApiKeyPolicyTemplateOut,
        *,
        enabled: bool | None = None,
    ) -> ApiKeyUpdate:
        expires_at = (
            datetime.utcnow() + timedelta(days=template.expires_in_days)
            if template.expires_in_days and template.expires_in_days > 0
            else None
        )
        return ApiKeyUpdate(
            enabled=enabled,
            expires_at=expires_at,
            token_limit_total=template.token_limit_total,
            cost_limit_total=template.cost_limit_total,
            route_mode=template.route_mode,
            default_provider_id=template.default_provider_id,
            manual_allow_fallback=template.manual_allow_fallback,
            allowed_provider_ids=template.allowed_provider_ids,
            allowed_model_names=template.allowed_model_names,
        )

    @staticmethod
    def _validate_model_names(db: Session, allowed_model_names: list[str]) -> None:
        if not allowed_model_names:
            return
        existing_names = set(
            db.scalars(select(ModelCatalog.model_name).where(ModelCatalog.model_name.in_(allowed_model_names))).all()
        )
        missing_names = [model_name for model_name in allowed_model_names if model_name not in existing_names]
        if missing_names:
            raise ValueError(f"模型不存在: {', '.join(missing_names)}")
