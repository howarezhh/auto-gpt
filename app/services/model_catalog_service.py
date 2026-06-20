from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session, load_only, selectinload, with_loader_criteria

from app.models.api_client_key import ApiClientKey
from app.models.api_client_key_provider_binding import ApiClientKeyProviderBinding
from app.models.api_key_policy_template import ApiKeyPolicyTemplate
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.user_account import UserAccount
from app.schemas.model_catalog import (
    ModelCatalogBatchImportRequest,
    ModelCatalogBatchImportResponse,
    ModelCatalogCreate,
    ModelCatalogUpdate,
    ModelProviderBindingIn,
)
from app.services.cache_service import CacheService
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.model_pricing_service import ModelPricingService
from app.services.provider_service import ProviderService
from app.utils.decimal_utils import (
    MULTIPLIER_QUANT,
    PRICE_QUANT,
    decimal_to_float,
    decimals_equal,
    divide_price_by_multiplier,
    multiply_price_and_multiplier,
    to_multiplier_decimal,
)
from app.utils.json_utils import dumps_json, loads_json


class ModelCatalogService:
    """负责模型目录管理、provider 绑定和目录同步。"""

    MODEL_HEALTH_MAX_PARALLEL_MODELS = 12
    MODEL_HEALTH_TEST_ALL_LIMIT = 50
    INTERACTIVE_MODEL_TEST_TOTAL_TIMEOUT_SECONDS = 10.0
    MODEL_OPTIONS_LIMIT = 500
    MODEL_HEALTH_FILTER_SCAN_LIMIT = 1000
    MODEL_MAPPING_NORMALIZE_BATCH_SIZE = 500
    MODEL_CATALOG_BATCH_IMPORT_TEMPLATE = """# 模型配置批量导入模板
# 每个模型一段，空行或 --- 分隔；导入只新增模型，不覆盖现有模型。
模型ID: gpt-5.4
模型名称: GPT-5.4
模型分组: openai
启用: 是
支持 Stream: 是
支持图像理解: 是
支持工具调用: 是
支持 Chat: 是
支持 Responses: 是
上下文 token: 400000
最大输入 token: 300000
最大输出 token: 100000
价格模式: fixed
价格 JSON:
账户输入价/1M: 3
账户输出价/1M: 12
账户缓存价/1M: 0.3
账户缓存写入价/1M:
原始币种: USD
账户币种: USD
原币种输入价/1M: 3
原币种输出价/1M: 12
原币种缓存价/1M: 0.3
原币种缓存写入价/1M:
汇率:
汇率来源:
汇率时间:
汇率版本:
舍入策略: ROUND_HALF_UP
速度标签: 快
备注: 示例模型
"""

    @staticmethod
    def _backfill_default_protocol_support(db: Session) -> bool:
        """模型目录级 Chat/Responses 协议未探测时默认均可用；挂载级协议由管理员配置。"""
        changed = False
        catalog_result = db.execute(
            update(ModelCatalog)
            .where(
                or_(
                    ModelCatalog.supports_chat_completions.is_(False),
                    ModelCatalog.supports_responses.is_(False),
                )
            )
            .values(
                supports_chat_completions=True,
                supports_responses=True,
            )
        )
        changed = changed or bool(getattr(catalog_result, "rowcount", 0) or 0)
        return changed

    @staticmethod
    def _catalog_default_capabilities(catalog: ModelCatalog) -> dict[str, bool]:
        """未挂载任何提供商模型时，使用目录模型的默认能力。"""
        return {
            "supports_stream": bool(catalog.supports_stream),
            "supports_tools": bool(catalog.supports_tools),
            "supports_vision": bool(catalog.supports_vision),
            "supports_image_generation": False,
        }

    @staticmethod
    def _aggregate_capabilities_from_bindings(catalog: ModelCatalog, bindings: list[dict]) -> dict[str, bool]:
        """模型展示能力由所有已挂载提供商模型能力 OR 聚合而来。"""
        active_bindings = [item for item in bindings if item.get("bound")]
        if not active_bindings:
            return ModelCatalogService._catalog_default_capabilities(catalog)
        return {
            "supports_stream": any(bool(item.get("supports_stream")) for item in active_bindings),
            "supports_tools": any(bool(item.get("supports_tools")) for item in active_bindings),
            "supports_vision": any(bool(item.get("supports_vision")) for item in active_bindings),
            "supports_image_generation": any(bool(item.get("supports_image_generation")) for item in active_bindings),
        }

    @staticmethod
    def list_catalogs(db: Session) -> list[ModelCatalog]:
        """返回全部模型目录项。"""
        return list(db.scalars(select(ModelCatalog).order_by(ModelCatalog.model_name.asc())))

    @staticmethod
    def get_catalog(db: Session, model_name: str) -> ModelCatalog | None:
        """按模型ID读取目录项。"""
        return db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == model_name))

    @staticmethod
    def list_model_dicts(db: Session) -> list[dict]:
        """返回模型目录的序列化结果列表。"""
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        return [ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs]

    @staticmethod
    def export_models_import_text(db: Session) -> str:
        """按批量导入格式导出现有模型目录。"""
        catalogs = ModelCatalogService.list_catalogs(db)
        blocks: list[str] = []
        for catalog in catalogs:
            pricing_json = ModelPricingService.serialize_pricing_json(catalog.pricing_json)
            blocks.append(
                "\n".join(
                    [
                        f"模型ID: {ProviderService._export_text(catalog.model_name)}",
                        f"模型名称: {ProviderService._export_text(catalog.display_name)}",
                        f"模型分组: {ProviderService._export_text(catalog.model_group)}",
                        f"启用: {ProviderService._export_bool(catalog.enabled)}",
                        f"支持 Stream: {ProviderService._export_bool(catalog.supports_stream)}",
                        f"支持图像理解: {ProviderService._export_bool(catalog.supports_vision)}",
                        f"支持工具调用: {ProviderService._export_bool(catalog.supports_tools)}",
                        f"支持 Chat: {ProviderService._export_bool(catalog.supports_chat_completions)}",
                        f"支持 Responses: {ProviderService._export_bool(catalog.supports_responses)}",
                        f"上下文 token: {ProviderService._export_text(catalog.context_window_tokens)}",
                        f"最大输入 token: {ProviderService._export_text(catalog.max_input_tokens)}",
                        f"最大输出 token: {ProviderService._export_text(catalog.max_output_tokens)}",
                        f"价格模式: {ProviderService._export_text(catalog.pricing_mode)}",
                        f"价格 JSON: {dumps_json(pricing_json) if pricing_json else ''}",
                        f"账户输入价/1M: {ProviderService._export_price_per_1m(catalog.input_price_per_1k)}",
                        f"账户输出价/1M: {ProviderService._export_price_per_1m(catalog.output_price_per_1k)}",
                        f"账户缓存价/1M: {ProviderService._export_price_per_1m(catalog.cache_price_per_1k)}",
                        f"账户缓存写入价/1M: {ProviderService._export_price_per_1m(catalog.cache_write_price_per_1k)}",
                        f"原始币种: {ProviderService._export_text(catalog.source_currency)}",
                        f"账户币种: {ProviderService._export_text(catalog.billing_currency)}",
                        f"原币种输入价/1M: {ProviderService._export_price_per_1m(catalog.source_input_price_per_1k)}",
                        f"原币种输出价/1M: {ProviderService._export_price_per_1m(catalog.source_output_price_per_1k)}",
                        f"原币种缓存价/1M: {ProviderService._export_price_per_1m(catalog.source_cache_price_per_1k)}",
                        f"原币种缓存写入价/1M: {ProviderService._export_price_per_1m(catalog.source_cache_write_price_per_1k)}",
                        f"汇率: {ProviderService._export_decimal(catalog.exchange_rate_to_billing_currency)}",
                        f"汇率来源: {ProviderService._export_text(catalog.exchange_rate_source)}",
                        f"汇率时间: {ProviderService._export_text(catalog.exchange_rate_at.isoformat() if catalog.exchange_rate_at else None)}",
                        f"汇率版本: {ProviderService._export_text(catalog.exchange_rate_version)}",
                        f"舍入策略: {ProviderService._export_text(catalog.rounding_strategy)}",
                        f"速度标签: {ProviderService._export_text(catalog.speed_label)}",
                        f"备注: {ProviderService._export_text(catalog.remark)}",
                    ]
                )
            )
        return "\n\n---\n\n".join(blocks) if blocks else ModelCatalogService.MODEL_CATALOG_BATCH_IMPORT_TEMPLATE

    @staticmethod
    def batch_import_models(db: Session, payload: ModelCatalogBatchImportRequest) -> ModelCatalogBatchImportResponse:
        """批量导入模型目录；模型 ID 只允许新增，禁止覆盖现有模型。"""
        blocks = ModelCatalogService._split_model_catalog_import_blocks(payload.content)
        existing_model_names = set(db.scalars(select(ModelCatalog.model_name)))
        seen_model_names: set[str] = set()
        providers = ProviderService.list_providers(db)
        result_items: list[dict] = []
        created_count = 0

        for index, block in enumerate(blocks, start=1):
            raw_item = ModelCatalogService._parse_model_catalog_import_block(block)
            errors: list[str] = []
            normalized = ModelCatalogService._normalize_model_catalog_import_item(raw_item, errors)
            model_name = normalized.get("model_name") if normalized else ProviderService._clean_optional_text(raw_item.get("model_name"))
            display_name = normalized.get("display_name") if normalized else ProviderService._clean_optional_text(raw_item.get("display_name"))
            if model_name:
                if model_name in seen_model_names:
                    errors.append("导入文本中模型ID重复")
                if model_name in existing_model_names:
                    errors.append("模型ID已存在，批量导入只允许新增模型")
                seen_model_names.add(model_name)
            valid = normalized is not None and not errors
            item = {
                "index": index,
                "model_name": model_name,
                "display_name": display_name,
                "valid": valid,
                "created": False,
                "errors": errors,
                "model": normalized if valid else None,
            }
            if valid and not payload.dry_run:
                try:
                    catalog = ModelCatalogService.create_model(db, ModelCatalogCreate(**normalized))
                    existing_model_names.add(catalog.model_name)
                    item["created"] = True
                    item["model"] = ModelCatalogService._serialize_catalog(catalog, providers)
                    created_count += 1
                except Exception as exc:
                    item["valid"] = False
                    item["errors"] = [str(exc)]
            result_items.append(item)

        valid_count = sum(1 for item in result_items if item["valid"])
        failed_count = sum(1 for item in result_items if not item["valid"])
        return ModelCatalogBatchImportResponse(
            total=len(result_items),
            valid_count=valid_count,
            created_count=created_count,
            failed_count=failed_count,
            dry_run=payload.dry_run,
            template=ModelCatalogService.MODEL_CATALOG_BATCH_IMPORT_TEMPLATE,
            items=result_items,
        )

    @staticmethod
    def list_model_option_dicts(db: Session) -> list[dict]:
        """返回适合下拉框与引用型页面的轻量模型列表。"""
        cache_key = "model-options:v3:list"
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return cached
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db, catalog_limit=ModelCatalogService.MODEL_OPTIONS_LIMIT)
        items = [ModelCatalogService._serialize_catalog_option(catalog, providers) for catalog in catalogs]
        return CacheService.set(cache_key, items, ttl_seconds=ModelCatalogService._model_list_cache_ttl_seconds())

    @staticmethod
    def list_model_page(
        db: Session,
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        health_status: str | None = None,
        model_group: str | None = None,
        provider_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页返回模型目录列表。"""
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 10), 100)
        providers = ProviderService.list_providers(db)
        normalized_health_status = ModelCatalogService._normalize_model_health_filter(health_status)
        if normalized_health_status is not None:
            catalogs = list(
                db.scalars(
                    ModelCatalogService._model_filter_query(
                        keyword=keyword,
                        enabled=enabled,
                        model_group=model_group,
                        provider_id=provider_id,
                    ).order_by(ModelCatalog.model_name.asc())
                    .limit(ModelCatalogService.MODEL_HEALTH_FILTER_SCAN_LIMIT)
                )
            )
            providers = ModelCatalogService._load_providers_for_catalogs(db, catalogs)
            filtered_items = [
                item
                for item in (ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs)
                if item["health_status"] == normalized_health_status
            ]
            total = len(filtered_items)
            total_pages = max((total + page_size - 1) // page_size, 1)
            page = min(page, total_pages)
            items = filtered_items[(page - 1) * page_size : page * page_size]
        else:
            query = ModelCatalogService._model_filter_query(keyword=keyword, enabled=enabled, model_group=model_group, provider_id=provider_id)
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            total_pages = max((total + page_size - 1) // page_size, 1)
            page = min(page, total_pages)
            catalogs = list(
                db.scalars(
                    query.order_by(ModelCatalog.model_name.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            providers = ModelCatalogService._load_providers_for_catalogs(db, catalogs)
            items = [ModelCatalogService._serialize_catalog(catalog, providers) for catalog in catalogs]
        return {
            "items": items,
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
            "summary": ModelCatalogService.model_summary(db),
        }

    @staticmethod
    def model_summary(db: Session) -> dict:
        """汇总模型目录的数量和可用性统计。"""
        cache_key = "model-catalog-summary:v2"
        cached = CacheService.get(cache_key)
        if isinstance(cached, dict):
            return cached
        catalog_row = db.execute(
            select(
                func.count(ModelCatalog.id).label("total"),
                func.sum(case((ModelCatalog.enabled.is_(True), 1), else_=0)).label("enabled"),
            )
        ).one()
        provider_model_row = db.execute(
            select(
                func.count(ProviderModel.id).label("bound_providers"),
                func.sum(
                    case(
                        (
                            ProviderModel.enabled.is_(True)
                            & Provider.enabled.is_(True)
                            & Provider.maintenance_mode_enabled.is_(False),
                            1,
                        ),
                        else_=0,
                    )
                ).label("available_providers"),
                func.sum(
                    case(
                        (
                            ProviderModel.enabled.is_(True)
                            & Provider.enabled.is_(True)
                            & Provider.maintenance_mode_enabled.is_(False)
                            & (Provider.circuit_state != "open")
                            & (ProviderModel.circuit_state != "open")
                            & (ProviderModel.health_status != "unhealthy"),
                            1,
                        ),
                        else_=0,
                    )
                ).label("enabled_providers"),
            )
            .select_from(ProviderModel)
            .join(Provider, Provider.id == ProviderModel.provider_id)
        ).one()
        return CacheService.set(cache_key, {
            "total": int(catalog_row.total or 0),
            "enabled": int(catalog_row.enabled or 0),
            "bound_providers": int(provider_model_row.bound_providers or 0),
            "available_providers": int(provider_model_row.available_providers or 0),
            "enabled_providers": int(provider_model_row.enabled_providers or 0),
        }, ttl_seconds=ModelCatalogService._model_list_cache_ttl_seconds())

    @staticmethod
    def get_model_detail(db: Session, model_name: str) -> dict | None:
        """返回单个模型目录的详细视图。"""
        catalog = ModelCatalogService.get_catalog(db, model_name)
        if catalog is None:
            return None
        providers = ProviderService.list_providers(db)
        return ModelCatalogService._serialize_catalog(catalog, providers, include_all_providers=True)

    @staticmethod
    def create_model(db: Session, payload: ModelCatalogCreate) -> ModelCatalog:
        """创建模型目录项并同步 provider 绑定。"""
        if ModelCatalogService.get_catalog(db, payload.model_name) is not None:
            raise ValueError("模型已存在")
        pricing_json = ModelCatalogService._pricing_json_with_top_level_fields(
            payload.pricing_json,
            payload.model_dump(),
            fallback_billing_currency=payload.billing_currency,
        )
        normalized_pricing = ModelPricingService.normalize_catalog_pricing(
            pricing_mode=payload.pricing_mode,
            pricing_json=pricing_json,
            input_price_per_1k=payload.input_price_per_1k,
            output_price_per_1k=payload.output_price_per_1k,
            cache_price_per_1k=payload.cache_price_per_1k,
            cache_write_price_per_1k=payload.cache_write_price_per_1k,
        )
        exchange_snapshot = normalized_pricing["exchange_rate_snapshot"]
        catalog = ModelCatalog(
            model_name=payload.model_name,
            display_name=payload.display_name,
            model_group=ProviderService.normalize_model_group(payload.model_group or ProviderService.infer_model_group(payload.model_name)),
            enabled=payload.enabled,
            supports_stream=payload.supports_stream,
            supports_vision=payload.supports_vision,
            supports_tools=payload.supports_tools,
            supports_chat_completions=payload.supports_chat_completions,
            supports_responses=payload.supports_responses,
            context_window_tokens=payload.context_window_tokens,
            max_input_tokens=payload.max_input_tokens,
            max_output_tokens=payload.max_output_tokens,
            pricing_mode=normalized_pricing["pricing_mode"],
            pricing_json=ModelPricingService.pricing_json_to_db_value(normalized_pricing["pricing_json"]),
            input_price_per_1k=normalized_pricing["input_price_per_1k"],
            output_price_per_1k=normalized_pricing["output_price_per_1k"],
            cache_price_per_1k=normalized_pricing["cache_price_per_1k"],
            cache_write_price_per_1k=normalized_pricing["cache_write_price_per_1k"],
            source_currency=normalized_pricing["source_currency"],
            billing_currency=normalized_pricing["billing_currency"],
            source_input_price_per_1k=normalized_pricing["source_input_price_per_1k"],
            source_output_price_per_1k=normalized_pricing["source_output_price_per_1k"],
            source_cache_price_per_1k=normalized_pricing["source_cache_price_per_1k"],
            source_cache_write_price_per_1k=normalized_pricing["source_cache_write_price_per_1k"],
            exchange_rate_to_billing_currency=exchange_snapshot.exchange_rate,
            exchange_rate_source=exchange_snapshot.exchange_rate_source,
            exchange_rate_at=ModelCatalogService._parse_snapshot_datetime(exchange_snapshot.exchange_rate_at),
            exchange_rate_version=exchange_snapshot.exchange_rate_version,
            rounding_strategy=exchange_snapshot.rounding_strategy,
            speed_label=payload.speed_label,
            remark=payload.remark,
        )
        db.add(catalog)
        db.flush()
        ModelCatalogService._apply_provider_bindings(db, catalog, payload.provider_bindings)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        db.refresh(catalog)
        return catalog

    @staticmethod
    def update_model(db: Session, catalog: ModelCatalog, payload: ModelCatalogUpdate) -> ModelCatalog:
        """更新模型目录项，并联动同步价格、能力和绑定关系。"""
        data = payload.model_dump(exclude_unset=True, exclude={"provider_bindings"})
        provider_bindings = payload.provider_bindings if "provider_bindings" in payload.model_fields_set else None
        pricing_field_names = {
            "pricing_mode",
            "pricing_json",
            "input_price_per_1k",
            "output_price_per_1k",
            "cache_price_per_1k",
            "cache_write_price_per_1k",
            "source_currency",
            "billing_currency",
            "source_input_price_per_1k",
            "source_output_price_per_1k",
            "source_cache_price_per_1k",
            "source_cache_write_price_per_1k",
            "exchange_rate_to_billing_currency",
            "exchange_rate_source",
            "exchange_rate_at",
            "exchange_rate_version",
            "rounding_strategy",
        }
        if pricing_field_names & set(data):
            pricing_json = ModelCatalogService._pricing_json_with_top_level_fields(
                data.get("pricing_json", ModelPricingService.parse_pricing_json(catalog.pricing_json)),
                data,
                fallback_billing_currency=data.get("billing_currency", catalog.billing_currency),
            )
            normalized_pricing = ModelPricingService.normalize_catalog_pricing(
                pricing_mode=data.get("pricing_mode", catalog.pricing_mode),
                pricing_json=pricing_json,
                input_price_per_1k=data.get("input_price_per_1k", catalog.input_price_per_1k),
                output_price_per_1k=data.get("output_price_per_1k", catalog.output_price_per_1k),
                cache_price_per_1k=data.get("cache_price_per_1k", catalog.cache_price_per_1k),
                cache_write_price_per_1k=data.get("cache_write_price_per_1k", catalog.cache_write_price_per_1k),
            )
            exchange_snapshot = normalized_pricing["exchange_rate_snapshot"]
            data["pricing_mode"] = normalized_pricing["pricing_mode"]
            data["pricing_json"] = ModelPricingService.pricing_json_to_db_value(normalized_pricing["pricing_json"])
            data["input_price_per_1k"] = normalized_pricing["input_price_per_1k"]
            data["output_price_per_1k"] = normalized_pricing["output_price_per_1k"]
            data["cache_price_per_1k"] = normalized_pricing["cache_price_per_1k"]
            data["cache_write_price_per_1k"] = normalized_pricing["cache_write_price_per_1k"]
            data["source_currency"] = normalized_pricing["source_currency"]
            data["billing_currency"] = normalized_pricing["billing_currency"]
            data["source_input_price_per_1k"] = normalized_pricing["source_input_price_per_1k"]
            data["source_output_price_per_1k"] = normalized_pricing["source_output_price_per_1k"]
            data["source_cache_price_per_1k"] = normalized_pricing["source_cache_price_per_1k"]
            data["source_cache_write_price_per_1k"] = normalized_pricing["source_cache_write_price_per_1k"]
            data["exchange_rate_to_billing_currency"] = exchange_snapshot.exchange_rate
            data["exchange_rate_source"] = exchange_snapshot.exchange_rate_source
            data["exchange_rate_at"] = ModelCatalogService._parse_snapshot_datetime(exchange_snapshot.exchange_rate_at)
            data["exchange_rate_version"] = exchange_snapshot.exchange_rate_version
            data["rounding_strategy"] = exchange_snapshot.rounding_strategy
        if "model_group" in data:
            data["model_group"] = ProviderService.normalize_model_group(
                data.get("model_group") or ProviderService.infer_model_group(catalog.model_name)
            )
        for field, value in data.items():
            setattr(catalog, field, value)
        changed_price_fields = set(data) & pricing_field_names
        if changed_price_fields:
            ModelCatalogService._sync_provider_prices_from_catalog(db, catalog, price_fields=changed_price_fields)
        if {
            "context_window_tokens",
            "max_input_tokens",
            "max_output_tokens",
        } & set(data):
            ModelCatalogService._sync_provider_capabilities_from_catalog(db, catalog)
        if provider_bindings is not None:
            ModelCatalogService._apply_provider_bindings(db, catalog, provider_bindings)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        db.refresh(catalog)
        return catalog

    @staticmethod
    def batch_update_context_window(
        db: Session,
        *,
        model_names: list[str],
        context_window_tokens: int | None,
    ) -> list[ModelCatalog]:
        """批量更新模型的上下文窗口。"""
        normalized_names = [item.strip() for item in model_names if isinstance(item, str) and item.strip()]
        if not normalized_names:
            raise ValueError("请选择要更新的模型")
        unique_names = list(dict.fromkeys(normalized_names))
        catalogs = list(
            db.scalars(
                select(ModelCatalog)
                .where(ModelCatalog.model_name.in_(unique_names))
                .order_by(ModelCatalog.model_name.asc())
            )
        )
        found_names = {catalog.model_name for catalog in catalogs}
        missing_names = [name for name in unique_names if name not in found_names]
        if missing_names:
            raise ValueError(f"模型不存在：{'、'.join(missing_names)}")
        for catalog in catalogs:
            catalog.context_window_tokens = context_window_tokens
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        for catalog in catalogs:
            db.refresh(catalog)
        return catalogs

    @staticmethod
    async def test_model_health(db: Session, model_name: str, *, phase_keys: frozenset[str] | None = None) -> dict[str, Any]:
        """测试单个目录模型在所有绑定渠道上的可用性。"""
        catalog = ModelCatalogService.get_catalog(db, model_name)
        if catalog is None:
            raise ValueError("模型不存在")
        providers = ModelCatalogService._load_providers_for_catalogs(db, [catalog])
        raw_results = await ModelCatalogService._probe_catalogs_health(
            [catalog],
            providers,
            quick_text_only=True,
            phase_keys=phase_keys,
        )
        raw_result = raw_results[0] if raw_results else {"catalog": catalog, "channel_results": []}
        return ModelCatalogService._finalize_catalog_health_test(
            db,
            raw_result,
            request_path="/model-catalog-test",
        )

    @staticmethod
    async def test_all_model_health(db: Session, *, phase_keys: frozenset[str] | None = None) -> list[dict[str, Any]]:
        """按提供商分组错峰测试全部目录模型。"""
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(
            db,
            catalog_limit=ModelCatalogService.MODEL_HEALTH_TEST_ALL_LIMIT,
        )
        if not catalogs:
            return []
        raw_results = await ModelCatalogService._probe_catalogs_health(
            catalogs,
            providers,
            quick_text_only=True,
            phase_keys=phase_keys,
        )
        return [
            ModelCatalogService._finalize_catalog_health_test(
                db,
                raw_result,
                request_path="/model-catalog-test-all",
            )
            for raw_result in raw_results
        ]

    @staticmethod
    def delete_model(db: Session, catalog: ModelCatalog) -> None:
        """删除模型目录项，并清理 provider 绑定和授权范围。"""
        model_name = catalog.model_name
        provider_ids = list(
            db.scalars(select(ProviderModel.provider_id).where(ProviderModel.model_name == model_name).distinct())
        )
        db.execute(delete(ProviderModel).where(ProviderModel.model_name == model_name))
        providers = list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .where(Provider.id.in_(provider_ids))
            )
        )
        for provider in providers:
            ProviderService.refresh_provider_state(provider)
        ModelCatalogService._remove_model_from_authorization_scopes(db, model_name)
        db.delete(catalog)
        db.commit()
        ModelCatalogService.invalidate_model_runtime_cache()
        ProviderService.invalidate_provider_runtime_cache()

    @staticmethod
    def sync_model_catalogs(db: Session) -> None:
        """根据 provider model 挂载情况同步模型目录。"""
        changed = ModelCatalogService._backfill_default_protocol_support(db)
        catalogs = {item.model_name: item for item in ModelCatalogService.list_catalogs(db)}
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .options(selectinload(ProviderModel.provider))
                .order_by(ProviderModel.model_name.asc(), ProviderModel.provider_id.asc())
            )
        )
        grouped: dict[str, list[ProviderModel]] = defaultdict(list)
        for item in provider_models:
            grouped[item.model_name].append(item)

        for model_name, items in grouped.items():
            catalog = catalogs.get(model_name)
            catalog_created = False
            if catalog is None:
                # 新发现的模型自动进入目录，减少后台手工维护成本。
                catalog = ModelCatalog(
                    model_name=model_name,
                    display_name=None,
                    model_group=ProviderService.infer_model_group(model_name),
                    enabled=True,
                    supports_stream=any(item.supports_stream for item in items),
                    supports_vision=any(item.supports_vision for item in items),
                    supports_tools=any(item.supports_tools for item in items),
                    supports_chat_completions=any(item.supports_chat_completions for item in items),
                    supports_responses=any(item.supports_responses for item in items),
                    context_window_tokens=ModelCatalogService._pick_max_int(items, field_name="context_window_tokens"),
                    max_input_tokens=ModelCatalogService._pick_max_int(items, field_name="max_input_tokens"),
                    max_output_tokens=ModelCatalogService._pick_max_int(items, field_name="max_output_tokens"),
                    pricing_mode=ModelPricingService.PRICING_MODE_FIXED,
                    pricing_json=None,
                    input_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="input_price_per_1k"),
                    output_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="output_price_per_1k"),
                    cache_price_per_1k=ModelCatalogService._pick_base_price(items, field_name="cache_price_per_1k"),
                )
                db.add(catalog)
                db.flush()
                catalogs[model_name] = catalog
                catalog_created = True
                changed = True

            for item in items:
                model_identity = item.upstream_model_name or item.model_name
                inferred_group = ProviderService.infer_model_group(model_identity)
                if inferred_group == "unknown":
                    inferred_group = ProviderService.infer_model_group(item.model_name)
                if (not item.model_group or item.model_group == "unknown") and inferred_group != "unknown":
                    item.model_group = ProviderService.normalize_model_group(inferred_group)
                    changed = True
                if (not catalog.model_group or catalog.model_group == "unknown") and inferred_group != "unknown":
                    catalog.model_group = ProviderService.normalize_model_group(inferred_group)
                    changed = True
                for field in ("context_window_tokens", "max_input_tokens", "max_output_tokens"):
                    if getattr(item, field) != getattr(catalog, field):
                        setattr(item, field, getattr(catalog, field))
                        changed = True
                if catalog_created:
                    derived_multiplier = ModelCatalogService._derive_multiplier(
                        base_input=catalog.input_price_per_1k,
                        direct_input=item.input_price_per_1k,
                        base_output=catalog.output_price_per_1k,
                        direct_output=item.output_price_per_1k,
                        base_cache=catalog.cache_price_per_1k,
                        direct_cache=item.cache_price_per_1k,
                        fallback=item.price_multiplier,
                    )
                    if not decimals_equal(item.price_multiplier, derived_multiplier, quant=MULTIPLIER_QUANT):
                        item.price_multiplier = derived_multiplier
                        changed = True
                if ModelCatalogService._sync_provider_model_shared_fields(item, catalog):
                    changed = True

        if changed:
            db.commit()
            ModelCatalogService.invalidate_model_runtime_cache()
            ProviderService.invalidate_provider_runtime_cache()

    @staticmethod
    def list_user_models(db: Session, *, user: UserAccount) -> list[dict]:
        key_scopes = ModelCatalogService._collect_user_route_scopes(db, user=user)
        if not key_scopes:
            return []
        catalogs, providers = ModelCatalogService._load_catalogs_and_providers(db)
        payloads: list[dict] = []
        for catalog in catalogs:
            if not catalog.enabled:
                continue
            serialized = ModelCatalogService._serialize_catalog(catalog, providers)
            allowed_bindings = [
                binding
                for binding in serialized["provider_bindings"]
                if ModelCatalogService._is_binding_routable(binding)
                and ModelCatalogService._is_model_allowed_for_user_scope(
                    model_name=catalog.model_name,
                    provider_id=binding["provider_id"],
                    key_scopes=key_scopes,
                )
            ]
            filtered_names = [
                binding["provider_name"]
                for binding in allowed_bindings
            ]
            filtered_input_prices = [
                binding["effective_input_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_input_price_per_1k"] is not None
            ]
            filtered_output_prices = [
                binding["effective_output_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_output_price_per_1k"] is not None
            ]
            filtered_cache_prices = [
                binding["effective_cache_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_cache_price_per_1k"] is not None
            ]
            filtered_cache_write_prices = [
                binding["effective_cache_write_price_per_1k"]
                for binding in allowed_bindings
                if binding["effective_cache_write_price_per_1k"] is not None
            ]
            if not filtered_names:
                continue
            capability_summary = ModelCatalogService._aggregate_capabilities_from_bindings(catalog, allowed_bindings)
            payloads.append(
                {
                    "model_name": catalog.model_name,
                    "display_name": catalog.display_name,
                    "model_group": catalog.model_group or ProviderService.infer_model_group(catalog.model_name),
                    "model_group_label": ProviderService.model_group_label(catalog.model_group or ProviderService.infer_model_group(catalog.model_name)),
                    "speed_label": catalog.speed_label,
                    "remark": catalog.remark,
                    "supports_stream": capability_summary["supports_stream"],
                    "supports_vision": capability_summary["supports_vision"],
                    "supports_tools": capability_summary["supports_tools"],
                    "supports_image_generation": capability_summary["supports_image_generation"],
                    "supports_chat_completions": catalog.supports_chat_completions,
                    "supports_responses": catalog.supports_responses,
                    "context_window_tokens": catalog.context_window_tokens,
                    "max_input_tokens": catalog.max_input_tokens,
                    "max_output_tokens": catalog.max_output_tokens,
                    "pricing_mode": catalog.pricing_mode,
                    "pricing_json": ModelPricingService.serialize_pricing_json(catalog.pricing_json),
                    "input_price_per_1k": min(filtered_input_prices) if filtered_input_prices else catalog.input_price_per_1k,
                    "output_price_per_1k": min(filtered_output_prices) if filtered_output_prices else catalog.output_price_per_1k,
                    "cache_price_per_1k": (
                        min(filtered_cache_prices)
                        if filtered_cache_prices
                        else (
                            catalog.cache_price_per_1k
                            if catalog.cache_price_per_1k is not None
                            else catalog.input_price_per_1k
                        )
                    ),
                    "cache_write_price_per_1k": (
                        min(filtered_cache_write_prices)
                        if filtered_cache_write_prices
                        else catalog.cache_write_price_per_1k
                    ),
                    "source_currency": catalog.source_currency,
                    "billing_currency": catalog.billing_currency,
                    "source_input_price_per_1k": catalog.source_input_price_per_1k,
                    "source_output_price_per_1k": catalog.source_output_price_per_1k,
                    "source_cache_price_per_1k": catalog.source_cache_price_per_1k,
                    "source_cache_write_price_per_1k": catalog.source_cache_write_price_per_1k,
                    "exchange_rate_to_billing_currency": catalog.exchange_rate_to_billing_currency,
                    "exchange_rate_source": catalog.exchange_rate_source,
                    "exchange_rate_at": catalog.exchange_rate_at,
                    "exchange_rate_version": catalog.exchange_rate_version,
                    "available_provider_names": filtered_names,
                    "enabled_provider_count": len(filtered_names),
                }
            )
        return payloads

    @staticmethod
    def enabled_model_name_set(db: Session) -> set[str]:
        cache_key = "model-enabled-names"
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return {str(item) for item in cached if isinstance(item, str)}
        names = set(db.scalars(select(ModelCatalog.model_name).where(ModelCatalog.enabled.is_(True))))
        CacheService.set(cache_key, sorted(names), ttl_seconds=ModelCatalogService._model_list_cache_ttl_seconds())
        return names

    @staticmethod
    def _model_list_cache_ttl_seconds() -> int:
        try:
            from app.services.setting_service import SettingService

            setting = SettingService.get_cached()
            raw_value = getattr(setting, "model_list_cache_ttl_sec", 15)
            if raw_value is None:
                return 15
            value = int(raw_value)
            if value <= 0:
                return 0
            return min(value, 300)
        except Exception:
            return 15

    @staticmethod
    def invalidate_model_runtime_cache() -> None:
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")
        CacheService.invalidate_prefix("model-enabled-names")
        CacheService.invalidate_prefix("model-catalog-limits")
        CacheService.invalidate_prefix("model-options")
        CacheService.invalidate_prefix("model-catalog-summary")

    @staticmethod
    def _load_catalogs_and_providers(
        db: Session,
        *,
        catalog_limit: int | None = None,
    ) -> tuple[list[ModelCatalog], list[Provider]]:
        if catalog_limit is None:
            catalogs = ModelCatalogService.list_catalogs(db)
        else:
            catalogs = list(
                db.scalars(
                    select(ModelCatalog)
                    .order_by(ModelCatalog.model_name.asc())
                    .limit(max(1, int(catalog_limit)))
                )
            )
        providers = ModelCatalogService._load_providers_for_catalogs(db, catalogs)
        return catalogs, providers

    @staticmethod
    def _load_providers_for_catalogs(db: Session, catalogs: list[ModelCatalog]) -> list[Provider]:
        catalog_names = [item.model_name for item in catalogs if item.model_name]
        if not catalog_names:
            return []
        return list(
            db.scalars(
                select(Provider)
                .options(
                    selectinload(Provider.provider_models),
                    with_loader_criteria(
                        ProviderModel,
                        ProviderModel.model_name.in_(catalog_names),
                        include_aliases=True,
                    ),
                )
                .where(
                    Provider.id.in_(
                        select(ProviderModel.provider_id).where(ProviderModel.model_name.in_(catalog_names))
                    )
                )
                .order_by(Provider.priority.asc(), Provider.id.asc())
                .execution_options(populate_existing=True)
            )
        )

    @staticmethod
    def _model_filter_query(
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        model_group: str | None = None,
        provider_id: int | None = None,
    ):
        query = select(ModelCatalog)
        if keyword:
            normalized = f"%{keyword.strip()}%"
            query = query.where(
                or_(
                    ModelCatalog.model_name.ilike(normalized),
                    ModelCatalog.display_name.ilike(normalized),
                    ModelCatalog.speed_label.ilike(normalized),
                    ModelCatalog.remark.ilike(normalized),
                    ModelCatalog.model_name.in_(
                        select(ProviderModel.model_name)
                        .join(Provider)
                        .where(Provider.name.ilike(normalized))
                    ),
                )
            )
        if enabled is not None:
            query = query.where(ModelCatalog.enabled.is_(enabled))
        if model_group:
            normalized_model_group = ProviderService.normalize_model_group(model_group)
            query = query.where(ModelCatalog.model_group == normalized_model_group)
        if provider_id is not None and provider_id > 0:
            query = query.where(
                ModelCatalog.model_name.in_(
                    select(ProviderModel.model_name).where(ProviderModel.provider_id == provider_id)
                )
            )
        return query

    @staticmethod
    def _normalize_model_health_filter(value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"healthy", "unhealthy"}:
            raise ValueError("模型可用状态筛选仅支持 healthy 或 unhealthy")
        return normalized

    @staticmethod
    def _split_model_catalog_import_blocks(content: str) -> list[str]:
        return ProviderService._split_batch_import_blocks(content)

    @staticmethod
    def _parse_model_catalog_import_block(block: str) -> dict:
        item: dict[str, str] = {}
        current_key: str | None = None
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([^:=：]+)\s*[:=：]\s*(.*)$", line)
            if match:
                normalized_key = ModelCatalogService._normalize_model_catalog_import_key(match.group(1))
                if not normalized_key:
                    if current_key:
                        item[current_key] = f"{item.get(current_key, '')}\n{line}".strip()
                    continue
                current_key = normalized_key
                item[current_key] = match.group(2).strip()
                continue
            if current_key:
                item[current_key] = f"{item.get(current_key, '')}\n{line}".strip()
        return item

    @staticmethod
    def _normalize_model_catalog_import_key(raw_key: str) -> str | None:
        normalized = re.sub(r"\s+", "", (raw_key or "").strip().lower())
        aliases = {
            "模型id": "model_name",
            "modelid": "model_name",
            "model_id": "model_name",
            "modelname": "model_name",
            "model_name": "model_name",
            "模型名": "display_name",
            "模型名称": "display_name",
            "展示名称": "display_name",
            "displayname": "display_name",
            "display_name": "display_name",
            "模型分组": "model_group",
            "分组": "model_group",
            "modelgroup": "model_group",
            "model_group": "model_group",
            "启用": "enabled",
            "enabled": "enabled",
            "支持stream": "supports_stream",
            "supportsstream": "supports_stream",
            "supports_stream": "supports_stream",
            "支持图像理解": "supports_vision",
            "支持图片理解": "supports_vision",
            "supportsvision": "supports_vision",
            "supports_vision": "supports_vision",
            "支持工具调用": "supports_tools",
            "supportstools": "supports_tools",
            "supports_tools": "supports_tools",
            "支持chat": "supports_chat_completions",
            "支持chatcompletions": "supports_chat_completions",
            "supportschatcompletions": "supports_chat_completions",
            "supports_chat_completions": "supports_chat_completions",
            "支持responses": "supports_responses",
            "supportsresponses": "supports_responses",
            "supports_responses": "supports_responses",
            "上下文token": "context_window_tokens",
            "上下文tok": "context_window_tokens",
            "contextwindowtokens": "context_window_tokens",
            "context_window_tokens": "context_window_tokens",
            "最大输入token": "max_input_tokens",
            "maxinputtokens": "max_input_tokens",
            "max_input_tokens": "max_input_tokens",
            "最大输出token": "max_output_tokens",
            "maxoutputtokens": "max_output_tokens",
            "max_output_tokens": "max_output_tokens",
            "价格模式": "pricing_mode",
            "pricingmode": "pricing_mode",
            "pricing_mode": "pricing_mode",
            "价格json": "pricing_json",
            "pricingjson": "pricing_json",
            "pricing_json": "pricing_json",
            "账户输入价1m": "input_price_per_1m",
            "账户输入价/1m": "input_price_per_1m",
            "输入价1m": "input_price_per_1m",
            "input_price_per_1m": "input_price_per_1m",
            "账户输出价1m": "output_price_per_1m",
            "账户输出价/1m": "output_price_per_1m",
            "输出价1m": "output_price_per_1m",
            "output_price_per_1m": "output_price_per_1m",
            "账户缓存价1m": "cache_price_per_1m",
            "账户缓存价/1m": "cache_price_per_1m",
            "cache_price_per_1m": "cache_price_per_1m",
            "账户缓存写入价1m": "cache_write_price_per_1m",
            "账户缓存写入价/1m": "cache_write_price_per_1m",
            "账户缓存写价1m": "cache_write_price_per_1m",
            "账户缓存写价/1m": "cache_write_price_per_1m",
            "cache_write_price_per_1m": "cache_write_price_per_1m",
            "原始币种": "source_currency",
            "原币种": "source_currency",
            "sourcecurrency": "source_currency",
            "source_currency": "source_currency",
            "账户币种": "billing_currency",
            "billingcurrency": "billing_currency",
            "billing_currency": "billing_currency",
            "原币种输入价1m": "source_input_price_per_1m",
            "原币种输入价/1m": "source_input_price_per_1m",
            "source_input_price_per_1m": "source_input_price_per_1m",
            "原币种输出价1m": "source_output_price_per_1m",
            "原币种输出价/1m": "source_output_price_per_1m",
            "source_output_price_per_1m": "source_output_price_per_1m",
            "原币种缓存价1m": "source_cache_price_per_1m",
            "原币种缓存价/1m": "source_cache_price_per_1m",
            "source_cache_price_per_1m": "source_cache_price_per_1m",
            "原币种缓存写入价1m": "source_cache_write_price_per_1m",
            "原币种缓存写入价/1m": "source_cache_write_price_per_1m",
            "原币种缓存写价1m": "source_cache_write_price_per_1m",
            "原币种缓存写价/1m": "source_cache_write_price_per_1m",
            "source_cache_write_price_per_1m": "source_cache_write_price_per_1m",
            "汇率": "exchange_rate_to_billing_currency",
            "exchangerate": "exchange_rate_to_billing_currency",
            "exchange_rate": "exchange_rate_to_billing_currency",
            "exchange_rate_to_billing_currency": "exchange_rate_to_billing_currency",
            "汇率来源": "exchange_rate_source",
            "exchange_rate_source": "exchange_rate_source",
            "汇率时间": "exchange_rate_at",
            "exchange_rate_at": "exchange_rate_at",
            "汇率版本": "exchange_rate_version",
            "exchange_rate_version": "exchange_rate_version",
            "舍入策略": "rounding_strategy",
            "rounding_strategy": "rounding_strategy",
            "速度标签": "speed_label",
            "speedlabel": "speed_label",
            "speed_label": "speed_label",
            "备注": "remark",
            "remark": "remark",
        }
        return aliases.get(normalized)

    @staticmethod
    def _normalize_model_catalog_import_item(raw_item: dict, errors: list[str]) -> dict | None:
        normalized = {
            ModelCatalogService._normalize_model_catalog_import_key(str(key)): value
            for key, value in raw_item.items()
            if ModelCatalogService._normalize_model_catalog_import_key(str(key))
        }
        model_name = ProviderService._clean_optional_text(normalized.get("model_name"))
        if not model_name:
            errors.append("缺少模型ID")
            return None
        try:
            pricing_json = ModelCatalogService._parse_model_catalog_pricing_json(normalized.get("pricing_json"))
            payload = {
                "model_name": model_name,
                "display_name": ProviderService._clean_optional_text(normalized.get("display_name")),
                "model_group": ProviderService.normalize_model_group(
                    ProviderService._clean_optional_text(normalized.get("model_group"))
                    or ProviderService.infer_model_group(model_name)
                ),
                "enabled": ProviderService._parse_batch_bool(normalized.get("enabled"), default=True),
                "supports_stream": ProviderService._parse_batch_bool(normalized.get("supports_stream"), default=True),
                "supports_vision": ProviderService._parse_batch_bool(normalized.get("supports_vision"), default=True),
                "supports_tools": ProviderService._parse_batch_bool(normalized.get("supports_tools"), default=True),
                "supports_chat_completions": ProviderService._parse_batch_bool(normalized.get("supports_chat_completions"), default=True),
                "supports_responses": ProviderService._parse_batch_bool(normalized.get("supports_responses"), default=True),
                "context_window_tokens": ProviderService._parse_batch_nullable_int(normalized.get("context_window_tokens"), default=None),
                "max_input_tokens": ProviderService._parse_batch_nullable_int(normalized.get("max_input_tokens"), default=None),
                "max_output_tokens": ProviderService._parse_batch_nullable_int(normalized.get("max_output_tokens"), default=None),
                "pricing_mode": ProviderService._clean_optional_text(normalized.get("pricing_mode")) or "fixed",
                "pricing_json": pricing_json,
                "input_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("input_price_per_1m")),
                "output_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("output_price_per_1m")),
                "cache_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("cache_price_per_1m")),
                "cache_write_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("cache_write_price_per_1m")),
                "source_currency": ModelCatalogService._normalize_import_currency(normalized.get("source_currency"), default="USD"),
                "billing_currency": ModelCatalogService._normalize_import_currency(normalized.get("billing_currency"), default="USD"),
                "source_input_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("source_input_price_per_1m")),
                "source_output_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("source_output_price_per_1m")),
                "source_cache_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("source_cache_price_per_1m")),
                "source_cache_write_price_per_1k": ProviderService._parse_price_per_1m(normalized.get("source_cache_write_price_per_1m")),
                "exchange_rate_to_billing_currency": ModelCatalogService._parse_optional_decimal(normalized.get("exchange_rate_to_billing_currency")),
                "exchange_rate_source": ProviderService._clean_optional_text(normalized.get("exchange_rate_source")),
                "exchange_rate_at": ModelCatalogService._parse_optional_datetime(normalized.get("exchange_rate_at")),
                "exchange_rate_version": ProviderService._clean_optional_text(normalized.get("exchange_rate_version")),
                "rounding_strategy": ProviderService._clean_optional_text(normalized.get("rounding_strategy")) or "ROUND_HALF_UP",
                "speed_label": ProviderService._clean_optional_text(normalized.get("speed_label")),
                "remark": ProviderService._clean_optional_text(normalized.get("remark")),
                "provider_bindings": [],
            }
            return ModelCatalogCreate(**payload).model_dump()
        except Exception as exc:
            errors.append(str(exc))
            return None

    @staticmethod
    def _parse_model_catalog_pricing_json(value) -> dict | None:
        text = ProviderService._clean_optional_text(value)
        if not text:
            return None
        parsed = loads_json(text, default=None)
        if not isinstance(parsed, dict):
            raise ValueError("价格 JSON 必须是对象")
        return parsed

    @staticmethod
    def _normalize_import_currency(value, *, default: str) -> str:
        text = ProviderService._clean_optional_text(value)
        return (text or default).upper()

    @staticmethod
    def _parse_optional_decimal(value) -> Decimal | None:
        text = ProviderService._clean_optional_text(value)
        if text is None:
            return None
        try:
            decimal_value = Decimal(text)
        except Exception as exc:
            raise ValueError("汇率必须是大于或等于 0 的数字") from exc
        if decimal_value < 0:
            raise ValueError("汇率必须是大于或等于 0 的数字")
        return decimal_value

    @staticmethod
    def _parse_optional_datetime(value) -> datetime | None:
        text = ProviderService._clean_optional_text(value)
        if text is None:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except Exception as exc:
            raise ValueError("汇率时间必须是 ISO 日期时间格式") from exc

    @staticmethod
    def _serialize_catalog(catalog: ModelCatalog, providers: list[Provider], *, include_all_providers: bool = False) -> dict:
        serialized_pricing_json = ModelPricingService.serialize_pricing_json(catalog.pricing_json)
        provider_model_map = {
            provider.id: next((item for item in provider.provider_models if item.model_name == catalog.model_name), None)
            for provider in providers
        }
        bindings = []
        for provider in providers:
            provider_model = provider_model_map.get(provider.id)
            provider_model_payload = ProviderService.provider_model_to_dict(provider_model) if provider_model else {}
            if provider_model is None and not include_all_providers:
                continue
            effective_input = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=catalog.input_price_per_1k,
                direct_price_per_1k=provider_model.input_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            effective_output = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=catalog.output_price_per_1k,
                direct_price_per_1k=provider_model.output_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            effective_cache = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=(
                    catalog.cache_price_per_1k
                    if catalog.cache_price_per_1k is not None
                    else catalog.input_price_per_1k
                ),
                direct_price_per_1k=provider_model.cache_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            effective_cache_write = ModelCatalogService._effective_price_per_1k(
                base_price_per_1k=catalog.cache_write_price_per_1k,
                direct_price_per_1k=provider_model.cache_write_price_per_1k if provider_model else None,
                price_multiplier=provider_model.price_multiplier if provider_model else 1.0,
            )
            bindings.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_enabled": provider.enabled,
                    "provider_health_status": provider.health_status,
                    "provider_circuit_state": provider.circuit_state,
                    "provider_maintenance_mode_enabled": provider.maintenance_mode_enabled,
                    "bound": provider_model is not None,
                    "provider_model_id": provider_model.id if provider_model else None,
                    "enabled": provider_model.enabled if provider_model else False,
                    "priority": provider_model.priority if provider_model else 100,
                    "price_multiplier": provider_model.price_multiplier if provider_model else 1.0,
                    "model_health_status": provider_model.health_status if provider_model else None,
                    "model_circuit_state": provider_model.circuit_state if provider_model else None,
                    "supports_stream": provider_model_payload.get("supports_stream", False),
                    "supports_tools": provider_model_payload.get("supports_tools", False),
                    "supports_vision": provider_model_payload.get("supports_vision", False),
                    "supports_image_generation": provider_model_payload.get("supports_image_generation", False),
                    "effective_input_price_per_1k": effective_input,
                    "effective_output_price_per_1k": effective_output,
                    "effective_cache_price_per_1k": effective_cache,
                    "effective_cache_write_price_per_1k": effective_cache_write,
                    "direct_input_price_per_1k": provider_model.input_price_per_1k if provider_model else None,
                    "direct_output_price_per_1k": provider_model.output_price_per_1k if provider_model else None,
                    "direct_cache_price_per_1k": provider_model.cache_price_per_1k if provider_model else None,
                    "direct_cache_write_price_per_1k": provider_model.cache_write_price_per_1k if provider_model else None,
                    "trust_status": provider_model_payload.get("trust_status", "unknown"),
                    "trust_status_label": provider_model_payload.get("trust_status_label", "未检测"),
                    "trust_status_reason": provider_model_payload.get("trust_status_reason"),
                    "content_integrity_status": provider_model_payload.get("content_integrity_status", "unknown"),
                    "content_probe_last_passed_at": provider_model_payload.get("content_probe_last_passed_at"),
                    "content_probe_last_failed_at": provider_model_payload.get("content_probe_last_failed_at"),
                }
            )

        active_bindings = [item for item in bindings if item["bound"]]
        available_bindings = [
            item for item in active_bindings if ModelCatalogService._is_binding_available_for_catalog_display(item)
        ]
        enabled_bindings = [item for item in active_bindings if ModelCatalogService._is_binding_routable(item)]
        healthy_bindings = [item for item in active_bindings if ModelCatalogService._is_binding_healthy_for_catalog_status(item)]
        trusted_bindings = [item for item in active_bindings if item.get("trust_status") == "trusted"]
        health_status = "healthy" if healthy_bindings else "unhealthy"
        health_reason = ModelCatalogService._build_catalog_health_reason(catalog, active_bindings, healthy_bindings)
        input_prices = [item["effective_input_price_per_1k"] for item in enabled_bindings if item["effective_input_price_per_1k"] is not None]
        output_prices = [item["effective_output_price_per_1k"] for item in enabled_bindings if item["effective_output_price_per_1k"] is not None]
        cache_prices = [item["effective_cache_price_per_1k"] for item in enabled_bindings if item["effective_cache_price_per_1k"] is not None]
        cache_write_prices = [item["effective_cache_write_price_per_1k"] for item in enabled_bindings if item["effective_cache_write_price_per_1k"] is not None]
        bound_multipliers = [item["price_multiplier"] for item in active_bindings]
        routable_multipliers = [item["price_multiplier"] for item in enabled_bindings]
        avg_bound_price_multiplier = ModelCatalogService._average_multiplier(bound_multipliers)
        avg_routable_price_multiplier = ModelCatalogService._average_multiplier(routable_multipliers)
        capability_summary = ModelCatalogService._aggregate_capabilities_from_bindings(catalog, active_bindings)
        return {
            "id": catalog.id,
            "model_name": catalog.model_name,
            "display_name": catalog.display_name,
            "model_group": catalog.model_group or ProviderService.infer_model_group(catalog.model_name),
            "model_group_label": ProviderService.model_group_label(catalog.model_group or ProviderService.infer_model_group(catalog.model_name)),
            "enabled": catalog.enabled,
            "supports_stream": capability_summary["supports_stream"],
            "supports_vision": capability_summary["supports_vision"],
            "supports_tools": capability_summary["supports_tools"],
            "supports_image_generation": capability_summary["supports_image_generation"],
            "supports_chat_completions": catalog.supports_chat_completions,
            "supports_responses": catalog.supports_responses,
            "context_window_tokens": catalog.context_window_tokens,
            "max_input_tokens": catalog.max_input_tokens,
            "max_output_tokens": catalog.max_output_tokens,
            "pricing_mode": catalog.pricing_mode,
            "pricing_json": serialized_pricing_json,
            "input_price_per_1k": catalog.input_price_per_1k,
            "output_price_per_1k": catalog.output_price_per_1k,
            "cache_price_per_1k": catalog.cache_price_per_1k,
            "cache_write_price_per_1k": catalog.cache_write_price_per_1k,
            "source_currency": catalog.source_currency,
            "billing_currency": catalog.billing_currency,
            "source_input_price_per_1k": catalog.source_input_price_per_1k,
            "source_output_price_per_1k": catalog.source_output_price_per_1k,
            "source_cache_price_per_1k": catalog.source_cache_price_per_1k,
            "source_cache_write_price_per_1k": catalog.source_cache_write_price_per_1k,
            "exchange_rate_to_billing_currency": catalog.exchange_rate_to_billing_currency,
            "exchange_rate_source": catalog.exchange_rate_source,
            "exchange_rate_at": catalog.exchange_rate_at,
            "exchange_rate_version": catalog.exchange_rate_version,
            "rounding_strategy": catalog.rounding_strategy,
            "speed_label": catalog.speed_label,
            "remark": catalog.remark,
            "provider_count": len(active_bindings),
            "bound_provider_count": len(active_bindings),
            "available_provider_count": len(available_bindings),
            "enabled_provider_count": len(enabled_bindings),
            "trusted_provider_count": len(trusted_bindings),
            "health_status": health_status,
            "health_reason": health_reason,
            "healthy_provider_count": len(healthy_bindings),
            "unhealthy_provider_count": max(0, len(active_bindings) - len(healthy_bindings)),
            "lowest_input_price_per_1k": min(input_prices) if input_prices else catalog.input_price_per_1k,
            "lowest_output_price_per_1k": min(output_prices) if output_prices else catalog.output_price_per_1k,
            "lowest_cache_price_per_1k": (
                min(cache_prices)
                if cache_prices
                else (
                    catalog.cache_price_per_1k
                    if catalog.cache_price_per_1k is not None
                    else catalog.input_price_per_1k
                )
            ),
            "lowest_cache_write_price_per_1k": min(cache_write_prices) if cache_write_prices else catalog.cache_write_price_per_1k,
            "avg_price_multiplier": avg_bound_price_multiplier,
            "avg_bound_price_multiplier": avg_bound_price_multiplier,
            "avg_routable_price_multiplier": avg_routable_price_multiplier,
            "bound_price_multiplier_count": len(bound_multipliers),
            "routable_price_multiplier_count": len(routable_multipliers),
            "min_bound_price_multiplier": min(bound_multipliers) if bound_multipliers else None,
            "max_bound_price_multiplier": max(bound_multipliers) if bound_multipliers else None,
            "available_provider_names": [item["provider_name"] for item in available_bindings],
            "provider_bindings": bindings,
            "created_at": catalog.created_at,
            "updated_at": catalog.updated_at,
        }

    @staticmethod
    def _build_catalog_health_reason(
        catalog: ModelCatalog,
        active_bindings: list[dict[str, Any]],
        healthy_bindings: list[dict[str, Any]],
    ) -> str:
        model_label = catalog.display_name or catalog.model_name
        if healthy_bindings:
            provider_names = [
                str(item.get("provider_name") or "").strip()
                for item in healthy_bindings
                if str(item.get("provider_name") or "").strip()
            ]
            if provider_names:
                preview = "、".join(provider_names[:3])
                suffix = f" 等 {len(provider_names)} 个提供商" if len(provider_names) > 3 else " 个提供商"
                return f"{model_label} 当前有 {preview}{suffix} 可用，状态为可用。"
            return f"{model_label} 当前至少有 1 个提供商可用，状态为可用。"
        if not active_bindings:
            return f"{model_label} 尚未绑定任何提供商，因此显示不可用。"
        unavailable_reasons: list[str] = []
        for item in active_bindings:
            provider_name = str(item.get("provider_name") or "提供商").strip() or "提供商"
            if not item.get("provider_enabled"):
                unavailable_reasons.append(f"{provider_name} 已停用")
            elif item.get("provider_maintenance_mode_enabled"):
                unavailable_reasons.append(f"{provider_name} 维护中")
            elif item.get("provider_circuit_state") == "open" or item.get("model_circuit_state") == "open":
                unavailable_reasons.append(f"{provider_name} 熔断中")
            elif not item.get("enabled"):
                unavailable_reasons.append(f"{provider_name} 的挂载已停用")
            elif item.get("provider_health_status") == "unhealthy":
                unavailable_reasons.append(f"{provider_name} 自身不可用")
            elif item.get("provider_health_status") not in {"healthy", "degraded"}:
                unavailable_reasons.append(f"{provider_name} 自身未检测")
            elif item.get("model_health_status") == "unhealthy":
                unavailable_reasons.append(f"{provider_name} 的挂载不可用")
            elif item.get("model_health_status") not in {"healthy", "degraded"}:
                unavailable_reasons.append(f"{provider_name} 的挂载未检测")
            else:
                unavailable_reasons.append(f"{provider_name} 当前不可用")
        reason_text = "；".join(unavailable_reasons[:3])
        if len(unavailable_reasons) > 3:
            reason_text += f" 等 {len(unavailable_reasons)} 项原因"
        return f"{model_label} 所有已绑定提供商均不可用：{reason_text}。"

    @staticmethod
    def _serialize_catalog_option(catalog: ModelCatalog, providers: list[Provider]) -> dict:
        serialized = ModelCatalogService._serialize_catalog(catalog, providers)
        return {
            "model_name": serialized["model_name"],
            "display_name": serialized["display_name"],
            "model_group": serialized["model_group"],
            "model_group_label": serialized["model_group_label"],
            "enabled": serialized["enabled"],
            "supports_stream": serialized["supports_stream"],
            "supports_vision": serialized["supports_vision"],
            "supports_tools": serialized["supports_tools"],
            "supports_image_generation": serialized["supports_image_generation"],
            "supports_chat_completions": serialized["supports_chat_completions"],
            "supports_responses": serialized["supports_responses"],
            "context_window_tokens": serialized["context_window_tokens"],
            "max_input_tokens": serialized["max_input_tokens"],
            "max_output_tokens": serialized["max_output_tokens"],
            "bound_provider_count": serialized["bound_provider_count"],
            "available_provider_count": serialized["available_provider_count"],
            "enabled_provider_count": serialized["enabled_provider_count"],
            "provider_bindings": serialized["provider_bindings"],
        }

    @staticmethod
    def _average_multiplier(multipliers: list[Decimal | float | int]) -> float | None:
        normalized = [to_multiplier_decimal(item) for item in multipliers if item is not None]
        if not normalized:
            return None
        average = sum(normalized, Decimal("0")) / Decimal(len(normalized))
        return decimal_to_float(average.quantize(Decimal("0.0001")))

    @staticmethod
    def _apply_provider_bindings(db: Session, catalog: ModelCatalog, bindings: list[ModelProviderBindingIn]) -> None:
        providers = ProviderService.list_providers(db)
        provider_map = {item.id: item for item in providers}
        existing_map = {
            provider.id: next((item for item in provider.provider_models if item.model_name == catalog.model_name), None)
            for provider in providers
        }
        processed_provider_ids: set[int] = set()

        for binding in bindings:
            provider = provider_map.get(binding.provider_id)
            if provider is None:
                raise ValueError(f"提供商不存在: {binding.provider_id}")
            processed_provider_ids.add(provider.id)
            provider_model = existing_map.get(provider.id)
            if not binding.bound:
                if provider_model is not None:
                    provider.provider_models.remove(provider_model)
                    ProviderService.refresh_provider_state(provider)
                continue

            if provider_model is None:
                provider_model = ProviderModel(provider=provider, model_name=catalog.model_name)
                provider_model.model_group = catalog.model_group or ProviderService.infer_model_group(catalog.model_name)
                provider_model.supports_stream = catalog.supports_stream
                provider_model.supports_vision = catalog.supports_vision
                provider_model.supports_tools = catalog.supports_tools
                provider_model.supports_image_generation = False
                db.add(provider_model)
            provider_model.enabled = binding.enabled
            provider_model.context_window_tokens = catalog.context_window_tokens
            provider_model.max_input_tokens = catalog.max_input_tokens
            provider_model.max_output_tokens = catalog.max_output_tokens
            provider_model.priority = binding.priority
            provider_model.price_multiplier = to_multiplier_decimal(binding.price_multiplier)
            resolved_prices = ModelPricingService.resolve_catalog_prices_for_provider(
                pricing_mode=catalog.pricing_mode,
                pricing_json=catalog.pricing_json,
                input_price_per_1k=catalog.input_price_per_1k,
                output_price_per_1k=catalog.output_price_per_1k,
                cache_price_per_1k=catalog.cache_price_per_1k,
                cache_write_price_per_1k=catalog.cache_write_price_per_1k,
                price_multiplier=provider_model.price_multiplier,
            )
            ModelCatalogService._apply_resolved_pricing_to_provider_model(provider_model, resolved_prices)
            ProviderService.refresh_provider_state(provider)

        for provider in providers:
            if provider.id in processed_provider_ids:
                continue
            provider_model = existing_map.get(provider.id)
            if provider_model is None:
                continue
            provider.provider_models.remove(provider_model)
            ProviderService.refresh_provider_state(provider)

    @staticmethod
    def _sync_provider_prices_from_catalog(db: Session, catalog: ModelCatalog, *, price_fields: set[str] | None = None) -> None:
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .where(ProviderModel.model_name == catalog.model_name)
            )
        )
        for provider_model in provider_models:
            resolved_prices = ModelPricingService.resolve_catalog_prices_for_provider(
                pricing_mode=catalog.pricing_mode,
                pricing_json=catalog.pricing_json,
                input_price_per_1k=catalog.input_price_per_1k,
                output_price_per_1k=catalog.output_price_per_1k,
                cache_price_per_1k=catalog.cache_price_per_1k,
                cache_write_price_per_1k=catalog.cache_write_price_per_1k,
                price_multiplier=provider_model.price_multiplier,
            )
            ModelCatalogService._apply_resolved_pricing_to_provider_model(provider_model, resolved_prices)

    @staticmethod
    def _sync_provider_model_shared_fields(provider_model: ProviderModel, catalog: ModelCatalog) -> bool:
        changed = False
        for field in (
            "context_window_tokens",
            "max_input_tokens",
            "max_output_tokens",
        ):
            if getattr(provider_model, field) != getattr(catalog, field):
                setattr(provider_model, field, getattr(catalog, field))
                changed = True
        resolved_prices = ModelPricingService.resolve_catalog_prices_for_provider(
            pricing_mode=catalog.pricing_mode,
            pricing_json=catalog.pricing_json,
            input_price_per_1k=catalog.input_price_per_1k,
            output_price_per_1k=catalog.output_price_per_1k,
            cache_price_per_1k=catalog.cache_price_per_1k,
            cache_write_price_per_1k=catalog.cache_write_price_per_1k,
            price_multiplier=provider_model.price_multiplier,
        )
        for field, expected in (
            ("input_price_per_1k", resolved_prices["input_price_per_1k"]),
            ("output_price_per_1k", resolved_prices["output_price_per_1k"]),
            ("cache_price_per_1k", resolved_prices["cache_price_per_1k"]),
            ("cache_write_price_per_1k", resolved_prices["cache_write_price_per_1k"]),
            ("source_input_price_per_1k", resolved_prices["source_input_price_per_1k"]),
            ("source_output_price_per_1k", resolved_prices["source_output_price_per_1k"]),
            ("source_cache_price_per_1k", resolved_prices["source_cache_price_per_1k"]),
            ("source_cache_write_price_per_1k", resolved_prices["source_cache_write_price_per_1k"]),
        ):
            current = getattr(provider_model, field)
            if not ModelCatalogService._nullable_decimal_equal(current, expected, quant=PRICE_QUANT):
                setattr(provider_model, field, expected)
                changed = True
        snapshot = resolved_prices["exchange_rate_snapshot"]
        metadata_fields = {
            "source_currency": resolved_prices["source_currency"],
            "billing_currency": resolved_prices["billing_currency"],
            "exchange_rate_to_billing_currency": snapshot.exchange_rate,
            "exchange_rate_source": snapshot.exchange_rate_source,
            "exchange_rate_at": ModelCatalogService._parse_snapshot_datetime(snapshot.exchange_rate_at),
            "exchange_rate_version": snapshot.exchange_rate_version,
            "rounding_strategy": snapshot.rounding_strategy,
        }
        for field, expected in metadata_fields.items():
            if getattr(provider_model, field) != expected:
                setattr(provider_model, field, expected)
                changed = True
        return changed

    @staticmethod
    def _apply_resolved_pricing_to_provider_model(provider_model: ProviderModel, resolved_prices: dict[str, Any]) -> None:
        provider_model.input_price_per_1k = resolved_prices["input_price_per_1k"]
        provider_model.output_price_per_1k = resolved_prices["output_price_per_1k"]
        provider_model.cache_price_per_1k = resolved_prices["cache_price_per_1k"]
        provider_model.cache_write_price_per_1k = resolved_prices.get("cache_write_price_per_1k")
        provider_model.source_currency = resolved_prices["source_currency"]
        provider_model.billing_currency = resolved_prices["billing_currency"]
        provider_model.source_input_price_per_1k = resolved_prices["source_input_price_per_1k"]
        provider_model.source_output_price_per_1k = resolved_prices["source_output_price_per_1k"]
        provider_model.source_cache_price_per_1k = resolved_prices["source_cache_price_per_1k"]
        provider_model.source_cache_write_price_per_1k = resolved_prices["source_cache_write_price_per_1k"]
        snapshot = resolved_prices["exchange_rate_snapshot"]
        provider_model.exchange_rate_to_billing_currency = snapshot.exchange_rate
        provider_model.exchange_rate_source = snapshot.exchange_rate_source
        provider_model.exchange_rate_at = ModelCatalogService._parse_snapshot_datetime(snapshot.exchange_rate_at)
        provider_model.exchange_rate_version = snapshot.exchange_rate_version
        provider_model.rounding_strategy = snapshot.rounding_strategy

    @staticmethod
    def _pricing_json_with_top_level_fields(
        pricing_json: str | dict | None,
        data: dict[str, Any],
        *,
        fallback_billing_currency: str | None = None,
    ) -> dict[str, Any]:
        metadata = ModelPricingService.parse_pricing_json(pricing_json)
        for source_field in (
            "source_currency",
            "billing_currency",
            "source_input_price_per_1k",
            "source_output_price_per_1k",
            "source_cache_price_per_1k",
            "source_cache_write_price_per_1k",
            "exchange_rate_source",
            "exchange_rate_at",
            "exchange_rate_version",
            "rounding_strategy",
        ):
            if source_field in data:
                metadata[source_field] = data[source_field]
        if "exchange_rate_to_billing_currency" in data:
            billing_currency = str(data.get("billing_currency") or metadata.get("billing_currency") or fallback_billing_currency or "USD").lower()
            metadata[f"exchange_rate_to_{billing_currency}"] = data["exchange_rate_to_billing_currency"]
        return metadata

    @staticmethod
    def _collect_catalog_test_targets(
        catalog: ModelCatalog,
        providers: list[Provider],
        *,
        include_disabled: bool = False,
    ) -> list[tuple[Provider, ProviderModel]]:
        targets: list[tuple[Provider, ProviderModel]] = []
        for provider in providers:
            if not include_disabled and (not provider.enabled or provider.maintenance_mode_enabled):
                continue
            provider_model = next((item for item in provider.provider_models if item.model_name == catalog.model_name), None)
            if provider_model is not None and (include_disabled or provider_model.enabled):
                targets.append((provider, provider_model))
        return targets

    @staticmethod
    async def _probe_catalog_health(
        catalog: ModelCatalog,
        providers: list[Provider],
        *,
        quick_text_only: bool = False,
        phase_keys: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        raw_results = await ModelCatalogService._probe_catalogs_health(
            [catalog],
            providers,
            quick_text_only=quick_text_only,
            phase_keys=phase_keys,
        )
        return raw_results[0] if raw_results else {"catalog": catalog, "channel_results": []}

    @staticmethod
    async def _probe_catalogs_health(
        catalogs: list[ModelCatalog],
        providers: list[Provider],
        *,
        quick_text_only: bool = False,
        phase_keys: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        from app.services.health_service import HealthService

        raw_results_by_catalog = {
            catalog.model_name: {"catalog": catalog, "channel_results": []}
            for catalog in catalogs
        }
        jobs: list[tuple[ModelCatalog, Provider, ProviderModel]] = []
        for catalog in catalogs:
            for provider, provider_model in ModelCatalogService._collect_catalog_test_targets(catalog, providers):
                jobs.append((catalog, provider, provider_model))
        if not jobs:
            return list(raw_results_by_catalog.values())
        channel_semaphore = asyncio.Semaphore(max(1, min(len(jobs), int(HealthService.MAX_PARALLEL_MODEL_PROBES) * 4)))

        async def run_channel(job: tuple[ModelCatalog, Provider, ProviderModel]) -> tuple[ModelCatalog, Provider, ProviderModel, dict[str, Any]]:
            catalog, provider, provider_model = job
            async with channel_semaphore:
                try:
                    result = await asyncio.wait_for(
                        HealthService._run_provider_model_checks(
                            provider,
                            [provider_model],
                            phase_keys=(
                                phase_keys
                                if phase_keys is not None
                                else (HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS if quick_text_only else None)
                            ),
                            text_probe_max_tokens=(
                                HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS
                                if quick_text_only
                                else None
                            ),
                            capability_probe_max_tokens=HealthService.INTERACTIVE_CAPABILITY_PROBE_MAX_TOKENS,
                            interactive_mode=True,
                            parallel_phases=True,
                            single_endpoint_mode=True,
                        ),
                        timeout=ModelCatalogService.INTERACTIVE_MODEL_TEST_TOTAL_TIMEOUT_SECONDS,
                    )
                    return catalog, provider, provider_model, result[0]
                except asyncio.TimeoutError:
                    return (
                        catalog,
                        provider,
                        provider_model,
                        ModelCatalogService._build_channel_timeout_result(
                            provider_model,
                            message=f"单模型测试总耗时超过 {int(ModelCatalogService.INTERACTIVE_MODEL_TEST_TOTAL_TIMEOUT_SECONDS)} 秒，已停止等待该渠道结果",
                        ),
                    )
                except Exception as exc:
                    return (
                        catalog,
                        provider,
                        provider_model,
                        ModelCatalogService._build_channel_timeout_result(
                            provider_model,
                            message=f"即时测试执行异常：{exc}",
                        ),
                    )

        provider_order = list(dict.fromkeys(provider.id for _catalog, provider, _provider_model in jobs))
        jobs_by_provider = {
            provider_id: [job for job in jobs if job[1].id == provider_id]
            for provider_id in provider_order
        }

        async def run_provider_jobs(provider_id: int) -> list[tuple[ModelCatalog, Provider, ProviderModel, dict[str, Any]]]:
            return await HealthService._gather_staggered_by_previous_completion(
                jobs_by_provider.get(provider_id) or [],
                run_channel,
            )

        grouped_results = await asyncio.gather(*(run_provider_jobs(provider_id) for provider_id in provider_order))
        for group in grouped_results:
            for catalog, provider, provider_model, result in group:
                raw_results_by_catalog[catalog.model_name]["channel_results"].append((provider, provider_model, result))
        return list(raw_results_by_catalog.values())

    @staticmethod
    def _build_channel_timeout_result(provider_model: ProviderModel, *, message: str) -> dict[str, Any]:
        return {
            "model_name": provider_model.model_name,
            "success": False,
            "provider_success": False,
            "health_status": "unhealthy",
            "latency_ms": int(ModelCatalogService.INTERACTIVE_MODEL_TEST_TOTAL_TIMEOUT_SECONDS * 1000),
            "status_code": 504,
            "message": message,
            "endpoint_results": [
                {
                    "endpoint_path": None,
                    "endpoint_label": "即时测试",
                    "success": False,
                    "native_success": False,
                    "adapted_success": False,
                    "support_mode": "timeout",
                    "support_label": "即时测试超时",
                    "latency_ms": int(ModelCatalogService.INTERACTIVE_MODEL_TEST_TOTAL_TIMEOUT_SECONDS * 1000),
                    "status_code": 504,
                    "message": message,
                    "trace": [],
                    "retryable": True,
                }
            ],
        }

    @staticmethod
    def _finalize_catalog_health_test(
        db: Session,
        raw_result: dict[str, Any],
        *,
        request_path: str,
    ) -> dict[str, Any]:
        from app.services.health_service import HealthService

        catalog: ModelCatalog = raw_result["catalog"]
        channel_payloads: list[dict[str, Any]] = []
        for provider, provider_model, model_result in raw_result.get("channel_results") or []:
            HealthService._persist_model_health_result(
                db,
                provider,
                provider_model,
                model_result,
                request_path=request_path,
            )
            channel_payloads.append(
                ModelCatalogService._serialize_model_channel_test_result(
                    provider,
                    provider_model,
                    model_result,
                )
            )
        healthy_channel_count = sum(1 for item in channel_payloads if item["available"])
        total_channel_count = len(channel_payloads)
        health_status = "healthy" if healthy_channel_count > 0 else "unhealthy"
        latency_ms = max((int(item.get("latency_ms") or 0) for item in channel_payloads), default=0)
        return {
            "model_name": catalog.model_name,
            "display_name": catalog.display_name,
            "success": health_status == "healthy",
            "health_status": health_status,
            "healthy_channel_count": healthy_channel_count,
            "total_channel_count": total_channel_count,
            "channel_results": channel_payloads,
            "latency_ms": latency_ms,
        }

    @staticmethod
    def _serialize_model_channel_test_result(
        provider: Provider,
        provider_model: ProviderModel,
        model_result: dict[str, Any],
    ) -> dict[str, Any]:
        result_health_status = str(model_result.get("health_status") or provider_model.health_status or "unknown")
        provider_success = bool(model_result.get("provider_success", model_result.get("success")))
        available = (
            bool(provider.enabled)
            and bool(provider_model.enabled)
            and not bool(provider.maintenance_mode_enabled)
            and provider.circuit_state != "open"
            and provider_model.circuit_state != "open"
            and provider_success
            and result_health_status != "unhealthy"
        )
        message = str(model_result.get("message") or "")
        if len(message) > 180:
            message = f"{message[:177]}..."
        endpoint_results = [
            ModelCatalogService._serialize_endpoint_test_result(item)
            for item in (model_result.get("endpoint_results") or [])
            if isinstance(item, dict)
        ]
        return {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
            "provider_enabled": provider.enabled,
            "model_enabled": provider_model.enabled,
            "success": bool(model_result.get("success")),
            "provider_success": provider_success,
            "available": available,
            "health_status": result_health_status,
            "status_code": model_result.get("status_code"),
            "latency_ms": int(model_result.get("latency_ms") or 0),
            "message": message,
            "endpoint_results": endpoint_results,
            "content_guard": ContentGuardProbeService.first_content_guard_result(model_result.get("endpoint_results") or []),
        }

    @staticmethod
    def _serialize_endpoint_test_result(endpoint_result: dict[str, Any]) -> dict[str, Any]:
        message = str(endpoint_result.get("message") or endpoint_result.get("support_label") or "")
        if len(message) > 180:
            message = f"{message[:177]}..."
        payload = {
            "endpoint_path": endpoint_result.get("endpoint_path"),
            "endpoint_label": endpoint_result.get("endpoint_label"),
            "success": bool(endpoint_result.get("success")),
            "native_success": bool(endpoint_result.get("native_success")),
            "adapted_success": bool(endpoint_result.get("adapted_success")),
            "support_mode": endpoint_result.get("support_mode"),
            "support_label": endpoint_result.get("support_label"),
            "latency_ms": int(endpoint_result.get("latency_ms") or 0),
            "status_code": endpoint_result.get("status_code"),
            "message": message,
            "retryable": endpoint_result.get("retryable"),
            "trace": endpoint_result.get("trace") if isinstance(endpoint_result.get("trace"), list) else [],
            "content_guard": endpoint_result.get("content_guard") if isinstance(endpoint_result.get("content_guard"), dict) else None,
        }
        if endpoint_result.get("raw_provider_response") is not None:
            payload["raw_provider_response"] = endpoint_result.get("raw_provider_response")
        return payload

    @staticmethod
    def _sync_provider_capabilities_from_catalog(db: Session, catalog: ModelCatalog) -> None:
        provider_models = list(
            db.scalars(
                select(ProviderModel)
                .where(ProviderModel.model_name == catalog.model_name)
            )
        )
        for provider_model in provider_models:
            provider_model.context_window_tokens = catalog.context_window_tokens
            provider_model.max_input_tokens = catalog.max_input_tokens
            provider_model.max_output_tokens = catalog.max_output_tokens

    @staticmethod
    def _nullable_decimal_equal(left, right, *, quant: Decimal) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return decimals_equal(left, right, quant=quant)

    @staticmethod
    def _parse_snapshot_datetime(value: str | None) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def _pick_base_price(provider_models: list[ProviderModel], *, field_name: str):
        values = [getattr(item, field_name) for item in provider_models if getattr(item, field_name) is not None]
        positive_values = [item for item in values if item > 0]
        if positive_values:
            return min(positive_values)
        return min(values) if values else None

    @staticmethod
    def _pick_max_int(provider_models: list[ProviderModel], *, field_name: str) -> int | None:
        values = [int(getattr(item, field_name)) for item in provider_models if getattr(item, field_name) is not None]
        return max(values) if values else None

    @staticmethod
    def _effective_price_per_1k(
        *,
        base_price_per_1k,
        direct_price_per_1k,
        price_multiplier,
    ):
        if base_price_per_1k is not None:
            return multiply_price_and_multiplier(base_price_per_1k, price_multiplier)
        return direct_price_per_1k

    @staticmethod
    def _derive_multiplier(
        *,
        base_input,
        direct_input,
        base_output,
        direct_output,
        base_cache,
        direct_cache,
        fallback,
    ) -> Decimal:
        fallback_decimal = to_multiplier_decimal(fallback)
        if base_input is not None and direct_input is not None:
            return divide_price_by_multiplier(direct_input, base_input, fallback=fallback_decimal)
        if base_output is not None and direct_output is not None:
            return divide_price_by_multiplier(direct_output, base_output, fallback=fallback_decimal)
        if base_cache is not None and direct_cache is not None:
            return divide_price_by_multiplier(direct_cache, base_cache, fallback=fallback_decimal)
        return fallback_decimal

    @staticmethod
    def _collect_user_route_scopes(db: Session, *, user: UserAccount) -> list[dict]:
        owned_keys = list(
            db.scalars(
                select(ApiClientKey)
                .options(
                    load_only(ApiClientKey.id, ApiClientKey.allowed_model_names_json),
                    selectinload(ApiClientKey.provider_bindings).load_only(
                        ApiClientKeyProviderBinding.provider_id
                    ),
                )
                .where(ApiClientKey.owner_user_id == user.id, ApiClientKey.enabled.is_(True))
            )
        )
        scopes = []
        for item in owned_keys:
            provider_ids = {binding.provider_id for binding in item.provider_bindings}
            if not provider_ids:
                continue
            scopes.append(
                {
                    "provider_ids": provider_ids,
                    "allowed_model_names": set(loads_json(item.allowed_model_names_json, [])),
                }
            )
        return scopes

    @staticmethod
    def _is_model_allowed_for_user_scope(*, model_name: str, provider_id: int, key_scopes: list[dict]) -> bool:
        for scope in key_scopes:
            if provider_id not in scope["provider_ids"]:
                continue
            allowed_model_names = scope["allowed_model_names"]
            if not allowed_model_names or model_name in allowed_model_names:
                return True
        return False

    @staticmethod
    def _remove_model_from_authorization_scopes(db: Session, model_name: str) -> None:
        from app.services.api_key_auth_cache import ApiKeyAuthCache

        api_keys = list(
            db.scalars(
                select(ApiClientKey)
                .options(
                    load_only(
                        ApiClientKey.id,
                        ApiClientKey.key_hash,
                        ApiClientKey.owner_user_id,
                        ApiClientKey.allowed_model_names_json,
                    )
                )
                .where(ApiClientKey.allowed_model_names_json.contains(model_name))
            )
        )
        for api_key in api_keys:
            allowed_model_names = list(loads_json(api_key.allowed_model_names_json, []))
            if model_name not in allowed_model_names:
                continue
            api_key.allowed_model_names_json = dumps_json(
                [item for item in allowed_model_names if item != model_name]
            )
            ApiKeyAuthCache.invalidate_api_key(api_key.id, api_key.key_hash)
            ApiKeyAuthCache.invalidate_user(api_key.owner_user_id)

        templates = list(
            db.scalars(
                select(ApiKeyPolicyTemplate)
                .options(
                    load_only(ApiKeyPolicyTemplate.id, ApiKeyPolicyTemplate.allowed_model_names_json)
                )
                .where(ApiKeyPolicyTemplate.allowed_model_names_json.contains(model_name))
            )
        )
        for template in templates:
            allowed_model_names = list(loads_json(template.allowed_model_names_json, []))
            if model_name not in allowed_model_names:
                continue
            template.allowed_model_names_json = dumps_json(
                [item for item in allowed_model_names if item != model_name]
            )

    @staticmethod
    def _is_binding_routable(binding: dict) -> bool:
        return (
            binding["bound"]
            and binding["enabled"]
            and binding["provider_enabled"]
            and not binding.get("provider_maintenance_mode_enabled")
            and binding.get("provider_circuit_state") != "open"
            and binding.get("model_circuit_state") != "open"
            and binding.get("model_health_status") != "unhealthy"
        )

    @staticmethod
    def _is_binding_healthy_for_catalog_status(binding: dict) -> bool:
        return (
            binding["bound"]
            and binding["enabled"]
            and binding["provider_enabled"]
            and not binding.get("provider_maintenance_mode_enabled")
            and binding.get("provider_circuit_state") != "open"
            and binding.get("model_circuit_state") != "open"
            and binding.get("provider_health_status") in {"healthy", "degraded"}
            and binding.get("model_health_status") in {"healthy", "degraded"}
        )

    @staticmethod
    def _is_binding_available_for_catalog_display(binding: dict) -> bool:
        return (
            binding["bound"]
            and binding["enabled"]
            and binding["provider_enabled"]
            and not binding.get("provider_maintenance_mode_enabled")
        )
