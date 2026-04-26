from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.api_client_key_provider_binding import ApiClientKeyProviderBinding
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.schemas.api_key import (
    ApiKeyAnalyticsOut,
    ApiKeyBalanceAdjustmentIn,
    ApiKeyBatchActionIn,
    ApiKeyBatchActionResultOut,
    ApiKeyBatchTemplateApplyIn,
    ApiKeyBatchRotateItemOut,
    ApiKeyBatchRotateResultOut,
    ApiKeyBatchProviderUpdateIn,
    ApiKeyBillingSummaryOut,
    ApiKeyCostInsightItemOut,
    ApiKeyCostInsightResponseOut,
    ApiKeyCreate,
    ApiKeyDetailOut,
    ApiKeyListResponse,
    ApiKeyModelDistributionItemOut,
    ApiKeyOut,
    ApiKeyRecentErrorOut,
    ApiKeyRecentUsageOut,
    ApiKeyStatsOut,
    ApiKeySummaryOut,
    ApiKeyUpdate,
)
from app.services.api_key_service import ApiKeyService
from app.services.billing_service import BillingService
from app.services.log_service import LogService
from app.utils.json_utils import dumps_json, loads_json


class ApiKeyAdminService:
    @staticmethod
    def get_summary(db: Session) -> ApiKeySummaryOut:
        api_keys = ApiKeyAdminService.list_api_keys(db)
        now = datetime.utcnow()
        enabled_keys = 0
        disabled_keys = 0
        expired_keys = 0
        quota_exhausted_keys = 0
        unbound_keys = 0
        for api_key in api_keys:
            if api_key.enabled:
                enabled_keys += 1
            else:
                disabled_keys += 1
            if api_key.expires_at is not None and api_key.expires_at <= now:
                expired_keys += 1
            if api_key.token_limit_total is not None and api_key.total_tokens_used >= api_key.token_limit_total:
                quota_exhausted_keys += 1
            if not api_key.provider_bindings:
                unbound_keys += 1

        aggregate_row = db.execute(
            select(
                func.count(RequestLog.id).label("total_requests"),
                func.sum(RequestLog.prompt_tokens).label("total_prompt_tokens"),
                func.sum(RequestLog.completion_tokens).label("total_completion_tokens"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost_used"),
            ).where(RequestLog.api_client_key_id.is_not(None))
        ).one()
        total_balance_amount = 0.0
        total_recharge_amount = 0.0
        counted_user_ids: set[int] = set()
        for item in api_keys:
            if item.owner_user_id is not None and item.owner_user is not None:
                if item.owner_user_id in counted_user_ids:
                    continue
                counted_user_ids.add(item.owner_user_id)
                total_balance_amount += BillingService.to_float(item.owner_user.balance_amount) or 0
                total_recharge_amount += BillingService.to_float(item.owner_user.total_recharge_amount) or 0
                continue
            total_balance_amount += float(item.balance_amount or 0)
            total_recharge_amount += float(item.total_recharge_amount or 0)

        return ApiKeySummaryOut(
            total_keys=len(api_keys),
            enabled_keys=enabled_keys,
            disabled_keys=disabled_keys,
            expired_keys=expired_keys,
            quota_exhausted_keys=quota_exhausted_keys,
            unbound_keys=unbound_keys,
            total_requests=int(aggregate_row.total_requests or 0),
            total_prompt_tokens=int(aggregate_row.total_prompt_tokens or 0),
            total_completion_tokens=int(aggregate_row.total_completion_tokens or 0),
            total_tokens=int(aggregate_row.total_tokens or 0),
            total_cost_used=float(aggregate_row.total_cost_used or 0),
            total_balance_amount=total_balance_amount,
            total_recharge_amount=total_recharge_amount,
        )

    @staticmethod
    def list_api_keys(db: Session) -> list[ApiClientKey]:
        return list(
            db.scalars(
                select(ApiClientKey)
                .options(
                    selectinload(ApiClientKey.provider_bindings).selectinload(ApiClientKeyProviderBinding.provider),
                    selectinload(ApiClientKey.owner_user),
                )
                .order_by(ApiClientKey.id.desc())
            )
        )

    @staticmethod
    def list_api_keys_paginated(
        db: Session,
        *,
        keyword: str | None,
        status: str | None,
        enabled: bool | None,
        owner_user_id: int | None,
        page: int,
        page_size: int,
    ) -> ApiKeyListResponse:
        normalized_keyword = keyword.strip().lower() if keyword and keyword.strip() else None
        stmt = (
            select(ApiClientKey)
            .options(
                selectinload(ApiClientKey.provider_bindings).selectinload(ApiClientKeyProviderBinding.provider),
                selectinload(ApiClientKey.owner_user),
            )
            .outerjoin(UserAccount, ApiClientKey.owner_user_id == UserAccount.id)
            .distinct()
            .order_by(ApiClientKey.id.desc())
        )
        if normalized_keyword:
            like_value = f"%{normalized_keyword}%"
            keyword_filter = or_(
                func.lower(ApiClientKey.name).like(like_value),
                func.lower(ApiClientKey.remark).like(like_value),
                func.lower(ApiClientKey.key_prefix).like(like_value),
                func.lower(UserAccount.username).like(like_value),
            )
            stmt = stmt.where(keyword_filter)
        if enabled is not None:
            stmt = stmt.where(ApiClientKey.enabled == enabled)
        if owner_user_id is not None:
            stmt = stmt.where(ApiClientKey.owner_user_id == owner_user_id)
        serialized_items = [ApiKeyAdminService.serialize_api_key(item) for item in db.scalars(stmt).unique()]
        if status:
            serialized_items = [item for item in serialized_items if item["status"] == status]
        total = len(serialized_items)
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        page_items = serialized_items[start:end]
        return ApiKeyListResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[ApiKeyOut(**item) for item in page_items],
        )

    @staticmethod
    def get_api_key(db: Session, api_key_id: int) -> ApiClientKey | None:
        return db.scalar(
            select(ApiClientKey)
            .options(
                selectinload(ApiClientKey.provider_bindings).selectinload(ApiClientKeyProviderBinding.provider),
                selectinload(ApiClientKey.owner_user),
            )
            .where(ApiClientKey.id == api_key_id)
        )

    @staticmethod
    def create_api_key(db: Session, payload: ApiKeyCreate) -> tuple[ApiClientKey, str]:
        ApiKeyAdminService._validate_provider_configuration(
            db,
            route_mode=payload.route_mode,
            allowed_provider_ids=payload.allowed_provider_ids,
            default_provider_id=payload.default_provider_id,
        )
        ApiKeyAdminService._validate_owner_user(db, payload.owner_user_id)
        raw_api_key = ApiKeyService.build_raw_api_key(payload.raw_api_key)
        ApiKeyAdminService._validate_raw_api_key_uniqueness(db, raw_api_key)
        use_shared_wallet = payload.owner_user_id is not None
        if use_shared_wallet and payload.balance_amount is not None:
            raise ValueError("共享钱包模式下禁止在 API Key 上单独设置 balance_amount，请改为给用户账户充值")
        api_key = ApiClientKey(
            name=payload.name,
            remark=payload.remark,
            tenant_name=payload.tenant_name,
            project_name=payload.project_name,
            app_name=payload.app_name,
            environment_name=payload.environment_name,
            key_prefix=ApiKeyService.extract_key_prefix(raw_api_key),
            key_hash=ApiKeyService.hash_api_key(raw_api_key),
            raw_key_encrypted=ApiKeyService.encrypt_raw_api_key(raw_api_key),
            enabled=payload.enabled,
            expires_at=payload.expires_at,
            token_limit_total=payload.token_limit_total,
            request_limit_daily=payload.request_limit_daily,
            token_limit_daily=payload.token_limit_daily,
            cost_limit_daily=BillingService.to_decimal(payload.cost_limit_daily) if payload.cost_limit_daily is not None else None,
            qps_limit=payload.qps_limit,
            rpm_limit=payload.rpm_limit,
            tpm_limit=payload.tpm_limit,
            cost_limit_total=BillingService.to_decimal(payload.cost_limit_total) if payload.cost_limit_total is not None else None,
            balance_amount=None if use_shared_wallet else (BillingService.to_decimal(payload.balance_amount) if payload.balance_amount is not None else None),
            total_cost_used=BillingService.to_decimal(0),
            total_recharge_amount=BillingService.to_decimal(0) if use_shared_wallet else (BillingService.to_decimal(payload.balance_amount) if payload.balance_amount is not None else BillingService.to_decimal(0)),
            route_mode=payload.route_mode,
            default_provider_id=payload.default_provider_id,
            owner_user_id=payload.owner_user_id,
            manual_allow_fallback=payload.manual_allow_fallback,
            allowed_model_names_json=dumps_json(payload.allowed_model_names),
            allowed_endpoint_paths_json=dumps_json(payload.allowed_endpoint_paths),
            allowed_source_ips_json=dumps_json(payload.allowed_source_ips),
            preferred_provider_ids_json=dumps_json(payload.preferred_provider_ids),
            preferred_region_tags_json=dumps_json(payload.preferred_region_tags),
            max_candidate_count=payload.max_candidate_count,
            latency_bias=payload.latency_bias,
            success_rate_bias=payload.success_rate_bias,
            cost_bias=payload.cost_bias,
        )
        db.add(api_key)
        db.flush()
        if not use_shared_wallet and payload.balance_amount is not None and payload.balance_amount > 0:
            db.add(
                ApiClientBillingRecord(
                    api_client_key_id=api_key.id,
                    request_log_id=None,
                    record_type="top_up",
                    amount=BillingService.to_decimal(payload.balance_amount),
                    balance_after=BillingService.to_decimal(payload.balance_amount),
                    remark="创建 API Key 初始余额",
                )
            )
        ApiKeyAdminService._replace_provider_bindings(db, api_key, payload.allowed_provider_ids)
        db.commit()
        db.refresh(api_key)
        return ApiKeyAdminService.get_api_key(db, api_key.id), raw_api_key

    @staticmethod
    def update_api_key(db: Session, api_key: ApiClientKey, payload: ApiKeyUpdate) -> ApiClientKey:
        data = payload.model_dump(exclude_unset=True)
        allowed_provider_ids = data.get("allowed_provider_ids")
        route_mode = data.get("route_mode", api_key.route_mode)
        default_provider_id = data.get("default_provider_id", api_key.default_provider_id)
        owner_user_id = data.get("owner_user_id", api_key.owner_user_id)
        if allowed_provider_ids is None:
            allowed_provider_ids = [binding.provider_id for binding in api_key.provider_bindings]
        ApiKeyAdminService._validate_provider_configuration(
            db,
            route_mode=route_mode,
            allowed_provider_ids=allowed_provider_ids,
            default_provider_id=default_provider_id,
        )
        ApiKeyAdminService._validate_owner_user(db, owner_user_id)
        use_shared_wallet = owner_user_id is not None
        if use_shared_wallet and "balance_amount" in data:
            raise ValueError("共享钱包模式下禁止在 API Key 上单独设置 balance_amount，请改为给用户账户充值")
        raw_api_key = data.get("raw_api_key")
        if raw_api_key is not None:
            normalized_raw_api_key = ApiKeyService.build_raw_api_key(raw_api_key)
            ApiKeyAdminService._validate_raw_api_key_uniqueness(db, normalized_raw_api_key, exclude_api_key_id=api_key.id)
            api_key.key_prefix = ApiKeyService.extract_key_prefix(normalized_raw_api_key)
            api_key.key_hash = ApiKeyService.hash_api_key(normalized_raw_api_key)
            api_key.raw_key_encrypted = ApiKeyService.encrypt_raw_api_key(normalized_raw_api_key)
        for field, value in data.items():
            if field in {"allowed_provider_ids", "raw_api_key"}:
                continue
            if field in {"cost_limit_total", "balance_amount", "cost_limit_daily"} and value is not None:
                value = BillingService.to_decimal(value)
            if field == "balance_amount" and use_shared_wallet:
                value = None
            setattr(api_key, field, value)
        json_list_fields = {
            "allowed_model_names": "allowed_model_names_json",
            "allowed_endpoint_paths": "allowed_endpoint_paths_json",
            "allowed_source_ips": "allowed_source_ips_json",
            "preferred_provider_ids": "preferred_provider_ids_json",
            "preferred_region_tags": "preferred_region_tags_json",
        }
        for payload_field, model_field in json_list_fields.items():
            if payload_field in data:
                setattr(api_key, model_field, dumps_json(data[payload_field] or []))
        if use_shared_wallet:
            api_key.balance_amount = None
            api_key.total_recharge_amount = BillingService.to_decimal(0)
        if "allowed_provider_ids" in data:
            ApiKeyAdminService._replace_provider_bindings(db, api_key, allowed_provider_ids)
        db.commit()
        db.refresh(api_key)
        return ApiKeyAdminService.get_api_key(db, api_key.id)

    @staticmethod
    def set_enabled(db: Session, api_key: ApiClientKey, enabled: bool) -> ApiClientKey:
        api_key.enabled = enabled
        db.commit()
        db.refresh(api_key)
        return ApiKeyAdminService.get_api_key(db, api_key.id)

    @staticmethod
    def delete_api_key(db: Session, api_key: ApiClientKey) -> None:
        db.delete(api_key)
        db.commit()

    @staticmethod
    def rotate_api_key(db: Session, api_key: ApiClientKey) -> tuple[ApiClientKey, str]:
        raw_api_key = ApiKeyService.build_raw_api_key(None)
        ApiKeyAdminService._validate_raw_api_key_uniqueness(db, raw_api_key, exclude_api_key_id=api_key.id)
        api_key.key_prefix = ApiKeyService.extract_key_prefix(raw_api_key)
        api_key.key_hash = ApiKeyService.hash_api_key(raw_api_key)
        api_key.raw_key_encrypted = ApiKeyService.encrypt_raw_api_key(raw_api_key)
        db.commit()
        db.refresh(api_key)
        return ApiKeyAdminService.get_api_key(db, api_key.id), raw_api_key

    @staticmethod
    def batch_enable(db: Session, payload: ApiKeyBatchActionIn) -> ApiKeyBatchActionResultOut:
        items = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        for item in items:
            item.enabled = True
        db.commit()
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(items),
            api_key_ids=[item.id for item in items],
        )

    @staticmethod
    def batch_disable(db: Session, payload: ApiKeyBatchActionIn) -> ApiKeyBatchActionResultOut:
        items = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        for item in items:
            item.enabled = False
        db.commit()
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(items),
            api_key_ids=[item.id for item in items],
        )

    @staticmethod
    def batch_delete(db: Session, payload: ApiKeyBatchActionIn) -> ApiKeyBatchActionResultOut:
        items = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        deleted_ids = [item.id for item in items]
        for item in items:
            db.delete(item)
        db.commit()
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(deleted_ids),
            api_key_ids=deleted_ids,
        )

    @staticmethod
    def batch_update_providers(db: Session, payload: ApiKeyBatchProviderUpdateIn) -> ApiKeyBatchActionResultOut:
        ApiKeyAdminService._validate_provider_configuration(
            db,
            route_mode=payload.route_mode,
            allowed_provider_ids=payload.allowed_provider_ids,
            default_provider_id=payload.default_provider_id,
        )
        items = list(
            db.scalars(
                select(ApiClientKey)
                .options(selectinload(ApiClientKey.provider_bindings))
                .where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        for item in items:
            item.route_mode = payload.route_mode
            item.default_provider_id = payload.default_provider_id
            item.manual_allow_fallback = payload.manual_allow_fallback
            ApiKeyAdminService._replace_provider_bindings(db, item, payload.allowed_provider_ids)
        db.commit()
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(items),
            api_key_ids=[item.id for item in items],
        )

    @staticmethod
    def batch_apply_template(db: Session, payload: ApiKeyBatchTemplateApplyIn) -> ApiKeyBatchActionResultOut:
        from app.services.api_key_policy_template_service import ApiKeyPolicyTemplateService

        template = ApiKeyPolicyTemplateService.get_template(db, payload.template_id)
        if template is None or not template.enabled:
            raise ValueError("策略模板不存在或未启用")
        serialized_template = ApiKeyPolicyTemplateService.serialize(template)
        items = list(
            db.scalars(
                select(ApiClientKey)
                .options(selectinload(ApiClientKey.provider_bindings))
                .where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        for item in items:
            update_payload = ApiKeyPolicyTemplateService.build_update_payload_from_template(serialized_template)
            ApiKeyAdminService.update_api_key(db, item, update_payload)
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(items),
            api_key_ids=[item.id for item in items],
        )

    @staticmethod
    def batch_rotate(db: Session, payload: ApiKeyBatchActionIn) -> ApiKeyBatchRotateResultOut:
        items = list(
            db.scalars(
                select(ApiClientKey)
                .options(
                    selectinload(ApiClientKey.provider_bindings).selectinload(ApiClientKeyProviderBinding.provider),
                    selectinload(ApiClientKey.owner_user),
                )
                .where(ApiClientKey.id.in_(payload.api_key_ids))
            )
        )
        rotated_items: list[ApiKeyBatchRotateItemOut] = []
        affected_ids: list[int] = []
        for item in items:
            updated, raw_api_key = ApiKeyAdminService.rotate_api_key(db, item)
            rotated_items.append(
                ApiKeyBatchRotateItemOut(
                    id=updated.id,
                    name=updated.name,
                    raw_api_key=raw_api_key,
                    key_masked=ApiKeyService.mask_key_prefix(updated.key_prefix),
                )
            )
            affected_ids.append(updated.id)
        return ApiKeyBatchRotateResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(affected_ids),
            api_key_ids=affected_ids,
            items=rotated_items,
        )

    @staticmethod
    def batch_expire(db: Session, payload: ApiKeyBatchActionIn) -> ApiKeyBatchActionResultOut:
        items = list(db.scalars(select(ApiClientKey).where(ApiClientKey.id.in_(payload.api_key_ids))))
        now = datetime.utcnow()
        for item in items:
            item.expires_at = now
        db.commit()
        return ApiKeyBatchActionResultOut(
            requested_count=len(payload.api_key_ids),
            affected_count=len(items),
            api_key_ids=[item.id for item in items],
        )

    @staticmethod
    def get_logs(
        db: Session,
        *,
        api_key_id: int,
        page: int,
        page_size: int,
        log_type: str | None,
        success: bool | None,
    ) -> tuple[int, list[RequestLog]]:
        stmt = select(RequestLog).where(RequestLog.api_client_key_id == api_key_id)
        count_stmt = select(func.count()).select_from(RequestLog).where(RequestLog.api_client_key_id == api_key_id)
        if log_type:
            stmt = stmt.where(RequestLog.log_type == log_type)
            count_stmt = count_stmt.where(RequestLog.log_type == log_type)
        if success is not None:
            stmt = stmt.where(RequestLog.success == success)
            count_stmt = count_stmt.where(RequestLog.success == success)
        total = db.scalar(count_stmt) or 0
        items = list(
            db.scalars(
                stmt.order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return total, items

    @staticmethod
    def get_stats(db: Session, *, api_key_id: int, window_hours: int = 24) -> ApiKeyStatsOut:
        aggregate_stmt = select(
            func.count(RequestLog.id).label("total_requests"),
            func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_requests"),
            func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
            func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
        ).where(RequestLog.api_client_key_id == api_key_id)
        aggregate_row = db.execute(aggregate_stmt).one()

        recent_usage = ApiKeyAdminService.get_recent_usage(db, api_key_id=api_key_id, window_hours=window_hours)

        return ApiKeyStatsOut(
            api_client_key_id=api_key_id,
            total_requests=int(aggregate_row.total_requests or 0),
            success_requests=int(aggregate_row.success_requests or 0),
            failed_requests=int(aggregate_row.failed_requests or 0),
            avg_latency_ms=round(float(aggregate_row.avg_latency_ms), 2) if aggregate_row.avg_latency_ms is not None else None,
            recent_window_hours=recent_usage.recent_window_hours,
            recent_requests=recent_usage.recent_requests,
            recent_failed_requests=recent_usage.recent_failed_requests,
            recent_prompt_tokens=recent_usage.recent_prompt_tokens,
            recent_completion_tokens=recent_usage.recent_completion_tokens,
            recent_total_tokens=recent_usage.recent_total_tokens,
            recent_total_cost=recent_usage.recent_total_cost,
        )

    @staticmethod
    def get_analytics(
        db: Session,
        *,
        api_key_id: int,
        recent_error_limit: int = 8,
        model_limit: int = 12,
    ) -> ApiKeyAnalyticsOut:
        model_name_expr = func.coalesce(RequestLog.requested_model, RequestLog.model_name, "unknown")
        model_distribution_rows = db.execute(
            select(
                model_name_expr.label("model_name"),
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost"),
                func.max(RequestLog.created_at).label("last_requested_at"),
            )
            .where(RequestLog.api_client_key_id == api_key_id)
            .group_by(model_name_expr)
            .order_by(func.count(RequestLog.id).desc(), model_name_expr.asc())
            .limit(max(1, model_limit))
        ).all()

        recent_error_rows = db.execute(
            select(RequestLog)
            .where(
                RequestLog.api_client_key_id == api_key_id,
                RequestLog.success.is_(False),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(max(1, recent_error_limit))
        ).scalars().all()

        return ApiKeyAnalyticsOut(
            api_client_key_id=api_key_id,
            model_distribution=[
                ApiKeyModelDistributionItemOut(
                    model_name=str(row.model_name or "unknown"),
                    total_requests=int(row.total_requests or 0),
                    failed_requests=int(row.failed_requests or 0),
                    total_tokens=int(row.total_tokens or 0),
                    total_cost=float(row.total_cost or 0),
                    last_requested_at=row.last_requested_at,
                )
                for row in model_distribution_rows
            ],
            recent_errors=[
                ApiKeyRecentErrorOut(
                    id=item.id,
                    created_at=item.created_at,
                    request_path=item.request_path,
                    model_name=item.requested_model or item.model_name,
                    provider_name=item.provider_name,
                    status_code=item.status_code,
                    api_client_auth_result=item.api_client_auth_result,
                    message=item.message,
                )
                for item in recent_error_rows
            ],
        )

    @staticmethod
    def serialize_api_key(api_key: ApiClientKey) -> dict:
        raw_api_key = ApiKeyService.decrypt_raw_api_key(api_key.raw_key_encrypted)
        owner_balance_amount = BillingService.to_decimal(api_key.owner_user.balance_amount) if api_key.owner_user is not None else None
        owner_total_recharge_amount = BillingService.to_decimal(api_key.owner_user.total_recharge_amount) if api_key.owner_user is not None else None
        allowed_providers = [
            {
                "id": binding.provider.id,
                "name": binding.provider.name,
                "enabled": binding.provider.enabled,
                "health_status": binding.provider.health_status,
            }
            for binding in api_key.provider_bindings
            if binding.provider is not None
        ]
        remaining_tokens = None
        if api_key.token_limit_total is not None:
            remaining_tokens = max(0, api_key.token_limit_total - api_key.total_tokens_used)
        remaining_cost_quota = None
        if api_key.cost_limit_total is not None:
            remaining_cost_quota = max(
                Decimal("0"),
                BillingService.to_decimal(api_key.cost_limit_total) - BillingService.to_decimal(api_key.total_cost_used),
            )
        balance_amount = BillingService.to_float(owner_balance_amount) if owner_balance_amount is not None else BillingService.to_float(api_key.balance_amount)
        total_recharge_amount = BillingService.to_float(owner_total_recharge_amount) if owner_total_recharge_amount is not None else (BillingService.to_float(api_key.total_recharge_amount) or 0)
        status = "active"
        if not api_key.enabled:
            status = "disabled"
        elif api_key.expires_at is not None and api_key.expires_at <= datetime.utcnow():
            status = "expired"
        elif api_key.token_limit_total is not None and api_key.total_tokens_used >= api_key.token_limit_total:
            status = "quota_exhausted"
        elif api_key.cost_limit_total is not None and BillingService.to_decimal(api_key.total_cost_used) >= BillingService.to_decimal(api_key.cost_limit_total):
            status = "cost_quota_exhausted"
        elif owner_balance_amount is not None and owner_balance_amount <= Decimal("0"):
            status = "balance_exhausted"
        elif owner_balance_amount is None and api_key.balance_amount is not None and BillingService.to_decimal(api_key.balance_amount) <= Decimal("0"):
            status = "balance_exhausted"
        elif not allowed_providers:
            status = "unbound"
        return {
            "id": api_key.id,
            "name": api_key.name,
            "remark": api_key.remark,
            "tenant_name": api_key.tenant_name,
            "project_name": api_key.project_name,
            "app_name": api_key.app_name,
            "environment_name": api_key.environment_name,
            "enabled": api_key.enabled,
            "status": status,
            "key_prefix": api_key.key_prefix,
            "key_masked": ApiKeyService.mask_key_prefix(api_key.key_prefix),
            "raw_api_key": raw_api_key,
            "has_stored_raw_key": raw_api_key is not None,
            "expires_at": api_key.expires_at,
            "token_limit_total": api_key.token_limit_total,
            "request_limit_daily": api_key.request_limit_daily,
            "token_limit_daily": api_key.token_limit_daily,
            "cost_limit_daily": BillingService.to_float(api_key.cost_limit_daily),
            "qps_limit": api_key.qps_limit,
            "rpm_limit": api_key.rpm_limit,
            "tpm_limit": api_key.tpm_limit,
            "prompt_tokens_used": api_key.prompt_tokens_used,
            "completion_tokens_used": api_key.completion_tokens_used,
            "total_tokens_used": api_key.total_tokens_used,
            "remaining_tokens": remaining_tokens,
            "cost_limit_total": BillingService.to_float(api_key.cost_limit_total),
            "total_cost_used": BillingService.to_float(api_key.total_cost_used) or 0,
            "balance_amount": balance_amount,
            "total_recharge_amount": total_recharge_amount,
            "remaining_cost_quota": BillingService.to_float(remaining_cost_quota),
            "route_mode": api_key.route_mode,
            "default_provider_id": api_key.default_provider_id,
            "owner_user_id": api_key.owner_user_id,
            "owner_user_name": api_key.owner_user.username if api_key.owner_user else None,
            "manual_allow_fallback": api_key.manual_allow_fallback,
            "allowed_provider_ids": [binding.provider_id for binding in api_key.provider_bindings],
            "allowed_model_names": loads_json(api_key.allowed_model_names_json, []),
            "allowed_endpoint_paths": loads_json(api_key.allowed_endpoint_paths_json, []),
            "allowed_source_ips": loads_json(api_key.allowed_source_ips_json, []),
            "preferred_provider_ids": loads_json(api_key.preferred_provider_ids_json, []),
            "preferred_region_tags": loads_json(api_key.preferred_region_tags_json, []),
            "max_candidate_count": api_key.max_candidate_count,
            "latency_bias": api_key.latency_bias,
            "success_rate_bias": api_key.success_rate_bias,
            "cost_bias": api_key.cost_bias,
            "allowed_providers": allowed_providers,
            "last_used_at": api_key.last_used_at,
            "created_at": api_key.created_at,
            "updated_at": api_key.updated_at,
        }

    @staticmethod
    def serialize_api_key_detail(
        db: Session,
        api_key: ApiClientKey,
        *,
        window_hours: int = 24,
    ) -> ApiKeyDetailOut:
        recent_usage = ApiKeyAdminService.get_recent_usage(
            db,
            api_key_id=api_key.id,
            window_hours=window_hours,
        )
        return ApiKeyDetailOut(
            **ApiKeyAdminService.serialize_api_key(api_key),
            recent_usage=recent_usage,
        )

    @staticmethod
    def get_recent_usage(
        db: Session,
        *,
        api_key_id: int,
        window_hours: int = 24,
    ) -> ApiKeyRecentUsageOut:
        normalized_window_hours = max(1, window_hours)
        since = datetime.utcnow() - timedelta(hours=normalized_window_hours)
        recent_stmt = select(
            func.count(RequestLog.id).label("recent_requests"),
            func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("recent_failed_requests"),
            func.sum(RequestLog.prompt_tokens).label("recent_prompt_tokens"),
            func.sum(RequestLog.completion_tokens).label("recent_completion_tokens"),
            func.sum(RequestLog.total_tokens).label("recent_total_tokens"),
            func.sum(RequestLog.total_cost).label("recent_total_cost"),
        ).where(
            RequestLog.api_client_key_id == api_key_id,
            RequestLog.created_at >= since,
        )
        recent_row = db.execute(recent_stmt).one()
        return ApiKeyRecentUsageOut(
            recent_window_hours=normalized_window_hours,
            recent_requests=int(recent_row.recent_requests or 0),
            recent_failed_requests=int(recent_row.recent_failed_requests or 0),
            recent_prompt_tokens=int(recent_row.recent_prompt_tokens or 0),
            recent_completion_tokens=int(recent_row.recent_completion_tokens or 0),
            recent_total_tokens=int(recent_row.recent_total_tokens or 0),
            recent_total_cost=float(recent_row.recent_total_cost or 0),
        )

    @staticmethod
    def get_billing_summary(
        db: Session,
        *,
        api_key_id: int,
        limit: int = 50,
    ) -> ApiKeyBillingSummaryOut:
        return BillingService.list_billing_records(db, api_key_id=api_key_id, limit=limit)

    @staticmethod
    def adjust_balance(
        db: Session,
        *,
        api_key: ApiClientKey,
        payload: ApiKeyBalanceAdjustmentIn,
    ) -> ApiKeyBillingSummaryOut:
        BillingService.create_balance_adjustment(
            db,
            api_key=api_key,
            amount=payload.amount,
            remark=payload.remark,
        )
        return BillingService.list_billing_records(db, api_key_id=api_key.id, limit=50)

    @staticmethod
    def get_cost_insights(
        db: Session,
        *,
        group_by: str,
        window_days: int,
        limit: int = 20,
    ) -> ApiKeyCostInsightResponseOut:
        normalized_group_by = group_by if group_by in {"user", "model", "provider"} else "user"
        normalized_window_days = max(1, min(window_days, 365))
        since = datetime.utcnow() - timedelta(days=normalized_window_days)
        if normalized_group_by == "model":
            value_expr = func.coalesce(RequestLog.requested_model, RequestLog.model_name, "unknown")
        elif normalized_group_by == "provider":
            value_expr = func.coalesce(RequestLog.provider_name, "unknown")
        else:
            value_expr = func.coalesce(RequestLog.user_account_name, "未分配用户")
        rows = db.execute(
            select(
                value_expr.label("dimension_value"),
                func.count(RequestLog.id).label("total_requests"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost"),
                func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
            )
            .where(
                RequestLog.api_client_key_id.is_not(None),
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .group_by(value_expr)
            .order_by(func.sum(RequestLog.total_cost).desc(), func.count(RequestLog.id).desc())
            .limit(max(1, min(limit, 100)))
        ).all()
        return ApiKeyCostInsightResponseOut(
            group_by=normalized_group_by,
            window_days=normalized_window_days,
            items=[
                ApiKeyCostInsightItemOut(
                    dimension_value=str(row.dimension_value or "unknown"),
                    total_requests=int(row.total_requests or 0),
                    total_tokens=int(row.total_tokens or 0),
                    total_cost=float(row.total_cost or 0),
                    avg_latency_ms=round(float(row.avg_latency_ms), 2) if row.avg_latency_ms is not None else None,
                )
                for row in rows
            ],
        )

    @staticmethod
    def _replace_provider_bindings(db: Session, api_key: ApiClientKey, allowed_provider_ids: list[int]) -> None:
        existing_by_provider_id = {binding.provider_id: binding for binding in api_key.provider_bindings}
        keep_ids = set(allowed_provider_ids)
        for provider_id in allowed_provider_ids:
            if provider_id in existing_by_provider_id:
                continue
            db.add(ApiClientKeyProviderBinding(api_client_key=api_key, provider_id=provider_id))
        for binding in list(api_key.provider_bindings):
            if binding.provider_id not in keep_ids:
                db.delete(binding)
        db.flush()

    @staticmethod
    def _validate_provider_configuration(
        db: Session,
        *,
        route_mode: str,
        allowed_provider_ids: list[int],
        default_provider_id: int | None,
    ) -> None:
        if route_mode == "manual" and default_provider_id is None:
            raise ValueError("manual route_mode requires default_provider_id")
        if not allowed_provider_ids:
            if default_provider_id is not None:
                raise ValueError("default_provider_id must be one of allowed_provider_ids")
            return
        existing_ids = set(
            db.scalars(select(Provider.id).where(Provider.id.in_(allowed_provider_ids))).all()
        )
        missing_ids = [provider_id for provider_id in allowed_provider_ids if provider_id not in existing_ids]
        if missing_ids:
            raise ValueError(f"Provider not found: {', '.join(str(item) for item in missing_ids)}")
        if default_provider_id is not None and default_provider_id not in existing_ids:
            raise ValueError("default_provider_id must be one of allowed_provider_ids")

    @staticmethod
    def _validate_owner_user(db: Session, owner_user_id: int | None) -> None:
        if owner_user_id is None:
            return
        existing = db.scalar(select(UserAccount.id).where(UserAccount.id == owner_user_id, UserAccount.enabled.is_(True)))
        if existing is None:
            raise ValueError("owner_user_id does not exist")

    @staticmethod
    def _validate_raw_api_key_uniqueness(
        db: Session,
        raw_api_key: str,
        *,
        exclude_api_key_id: int | None = None,
    ) -> None:
        key_hash = ApiKeyService.hash_api_key(raw_api_key)
        stmt = select(ApiClientKey.id).where(ApiClientKey.key_hash == key_hash)
        if exclude_api_key_id is not None:
            stmt = stmt.where(ApiClientKey.id != exclude_api_key_id)
        existing = db.scalar(stmt)
        if existing is not None:
            raise ValueError("API 密钥已存在，请使用其他值")
