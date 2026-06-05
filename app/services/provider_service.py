import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from httpx import HTTPError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.model_catalog import ModelCatalog
from app.models.request_log import RequestLog
from app.models.api_client_key_provider_binding import ApiClientKeyProviderBinding
from app.models.api_client_key import ApiClientKey
from app.models.app_setting import AppSetting
from app.schemas.provider import (
    ProviderBatchImportRequest,
    ProviderBatchImportResponse,
    ProviderCreate,
    ProviderDiscoverModelsIn,
    ProviderDiscoverModelsResponse,
    ProviderDiscoveredModelOut,
    ProviderModelConfigInput,
    ProviderModelConfigUpdate,
    ProviderUpdate,
)
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.model_pricing_service import ModelPricingService
from app.services.provider_capacity_service import (
    ProviderCapacityService,
    ProviderCapacitySnapshot,
    ProviderCapacityUnavailableError,
)
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService
from app.utils.decimal_utils import to_multiplier_decimal
from app.utils.json_utils import dumps_json, loads_json


class ProviderService:
    """负责 provider 及其模型挂载的管理、能力推断与状态维护。"""

    QUALITY_WINDOW_MINUTES = 24 * 60
    QUALITY_CACHE_TTL_SECONDS = 15
    AVAILABILITY_CACHE_TTL_SECONDS = 30
    VISION_MODEL_HINTS = (
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "vision",
        "qwen-vl",
        "qwen2.5-vl",
        "qvq",
        "glm-4v",
        "glm-4.1v",
        "mimo-vl",
        "doubao-vision",
    )
    VISION_MODEL_REGEXES = (
        re.compile(r"(?:^|[-_/])vision(?:$|[-_/])"),
        re.compile(r"(?:^|[-_/])vl(?:$|[-_/])"),
        re.compile(r"glm-\d+(?:\.\d+)?v(?:$|[-_/])"),
    )
    TOOL_CAPABLE_MODEL_HINTS = (
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "o3",
        "o4",
        "claude",
        "qwen",
        "deepseek",
        "glm",
        "moonshot",
        "kimi",
        "doubao",
        "mimo",
    )
    IMAGE_GENERATION_MODEL_HINTS = ("gpt-4o", "gpt-4.1", "gpt-5")
    TRACE_TERMINAL_SUCCESS_RESULTS = {"success"}
    TRACE_TERMINAL_FAILURE_RESULTS = {
        "http_error",
        "upstream_auth_error",
        "model_not_found",
        "rate_limited",
        "request_rejected",
        "exception",
        "client_cancelled",
    }
    BATCH_IMPORT_TEMPLATE = """# 中转站批量导入模板

每个中转站使用一段键值内容，段落之间用空行或 --- 分隔。AI 根据用户提供的中转站信息生成时，必须保留字段名。

名称: 中文渠道名
Base URL: https://example.com/v1
API Key: sk-xxxx
类型: openai_compatible
分组: 第三方聚合
地区: hk
优先级: 100
权重: 100
超时毫秒: 30000
最大重试次数: 1
最大活跃请求: 20
最大流式请求: 10
最大 QPS: 20
每分钟最多请求: 20
最大错误率: 80
首 Token 超时秒: 60
模型: gpt-5.4, gpt-5.5, gpt-4.1-mini
模型能力: 流式 / 仅文本 / 工具调用 / 图像理解
启用: 是
备注: 可选备注

---

名称: 第二个中文渠道名
Base URL: https://another.example.com/v1
API Key: sk-yyyy
模型:
模型能力:
"""

    @staticmethod
    def _model_name_supports_vision(normalized: str) -> bool:
        if not normalized:
            return False
        if normalized.startswith("kimi-k2"):
            return True
        if normalized.startswith("moonshot-v1") and "vision" in normalized:
            return True
        if normalized in {"mimo-v2.5", "mimo-v2-omni"}:
            return True
        if normalized.startswith("mimo-v2.5-omni"):
            return True
        if any(prefix in normalized for prefix in ProviderService.VISION_MODEL_HINTS):
            return True
        return any(pattern.search(normalized) for pattern in ProviderService.VISION_MODEL_REGEXES)

    @staticmethod
    def _model_name_supports_tools(normalized: str) -> bool:
        if not normalized:
            return False
        if normalized.startswith("kimi-k2"):
            return True
        if normalized.startswith("moonshot-v1"):
            return True
        if normalized.startswith("mimo-v2"):
            return True
        if normalized.startswith("doubao"):
            return True
        return any(prefix in normalized for prefix in ProviderService.TOOL_CAPABLE_MODEL_HINTS)

    @staticmethod
    def _model_name_supports_responses(normalized: str) -> bool:
        if not normalized:
            return True
        chat_only_prefixes = (
            "deepseek",
            "glm",
            "kimi",
            "moonshot-v1",
            "mimo",
        )
        if normalized.startswith(chat_only_prefixes):
            return False
        return True

    @staticmethod
    def _infer_model_capabilities(model_name: str) -> dict[str, bool]:
        """根据模型名启发式推断视觉、工具和图像生成能力。"""
        normalized = (model_name or "").strip().lower()
        supports_vision = ProviderService._model_name_supports_vision(normalized)
        supports_tools = ProviderService._model_name_supports_tools(normalized)
        supports_image_generation = any(prefix in normalized for prefix in ProviderService.IMAGE_GENERATION_MODEL_HINTS)
        supports_responses = ProviderService._model_name_supports_responses(normalized)
        return {
            "supports_stream": True,
            "supports_vision": supports_vision,
            "supports_tools": supports_tools,
            "supports_image_generation": supports_image_generation,
            "supports_chat_completions": True,
            "supports_responses": supports_responses,
        }

    @staticmethod
    def model_name_supports_image_generation(model_name: str) -> bool:
        """判断模型名是否具备图像生成倾向。"""
        return ProviderService._infer_model_capabilities(model_name).get("supports_image_generation", False)

    @staticmethod
    def model_name_supports_tools(model_name: str) -> bool:
        """判断模型名是否具备工具调用倾向。"""
        return ProviderService._infer_model_capabilities(model_name).get("supports_tools", False)

    @staticmethod
    def provider_model_supports_tools(provider_model: ProviderModel) -> bool:
        """结合显式配置和模型名推断 provider model 的工具能力。"""
        inferred = ProviderService._infer_model_capabilities(provider_model.model_name)
        return bool(provider_model.supports_tools or inferred.get("supports_tools", False))

    @staticmethod
    def provider_model_supports_image_generation(provider_model: ProviderModel) -> bool:
        """判断 provider model 是否可用于图像生成链路。"""
        inferred = ProviderService._infer_model_capabilities(provider_model.model_name)
        return bool(
            provider_model.supports_responses
            and ProviderService.provider_model_supports_tools(provider_model)
            and (provider_model.supports_vision or inferred.get("supports_image_generation", False))
        )

    @staticmethod
    def _build_model_config_input_from_name(model_name: str) -> ProviderModelConfigInput:
        """根据模型名生成默认的模型配置输入对象。"""
        capabilities = ProviderService._infer_model_capabilities(model_name)
        return ProviderModelConfigInput(
            model_name=model_name,
            supports_stream=capabilities["supports_stream"],
            supports_vision=capabilities["supports_vision"],
            supports_tools=capabilities["supports_tools"],
            supports_chat_completions=capabilities["supports_chat_completions"],
            supports_responses=capabilities["supports_responses"],
        )

    @staticmethod
    def mask_api_key(api_key: str) -> str:
        """对 provider API key 做脱敏展示。"""
        if len(api_key) <= 8:
            return "******"
        return f"{api_key[:4]}...{api_key[-4:]}"

    @staticmethod
    def list_providers(db: Session) -> list[Provider]:
        """按优先级返回 provider 列表，并预加载模型挂载信息。"""
        return list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .order_by(Provider.priority.asc(), Provider.id.asc())
            )
        )

    @staticmethod
    def list_runtime_providers(db: Session) -> list[Provider]:
        """读取请求路由热路径使用的 provider 快照。"""
        cache_key = "providers-runtime:list"
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return cached
        providers = ProviderService.list_providers(db)
        CacheService.set(cache_key, providers, ttl_seconds=ProviderService._runtime_provider_cache_ttl_seconds())
        return providers

    @staticmethod
    def _runtime_provider_cache_ttl_seconds() -> int:
        try:
            setting = SettingService.get_cached()
            return max(2, min(int(getattr(setting, "route_candidate_cache_ttl_sec", 10) or 10), 60))
        except Exception:
            return 10

    @staticmethod
    def list_provider_dicts(db: Session) -> list[dict]:
        """返回带质量指标的 provider 序列化结果。"""
        providers = ProviderService.list_providers(db)
        metrics = ProviderService._build_quality_metrics(db, providers)
        return [ProviderService.provider_to_dict(provider, metrics=metrics) for provider in providers]

    @staticmethod
    def build_provider_page_content(db: Session) -> dict:
        """返回中转站页面首屏与异步刷新共用的数据口径。"""
        providers = ProviderService.list_provider_dicts(db)
        return ProviderService.build_provider_page_content_from_dicts(providers)

    @staticmethod
    def build_provider_page_content_from_dicts(providers: list[dict]) -> dict:
        enabled_provider_count = sum(1 for item in providers if item.get("enabled"))
        model_configs = [model for item in providers for model in item.get("model_configs", [])]
        model_summary_map: dict[str, dict] = {}
        for model_config in model_configs:
            model_name = str(model_config.get("model_name") or "").strip()
            if not model_name:
                continue
            current = model_summary_map.setdefault(
                model_name,
                {
                    "model_name": model_name,
                    "supports_stream": False,
                    "supports_vision": False,
                    "supports_image_generation": False,
                    "priced": False,
                    "stability_scores": [],
                },
            )
            current["supports_stream"] = current["supports_stream"] or bool(model_config.get("supports_stream"))
            current["supports_vision"] = current["supports_vision"] or bool(model_config.get("supports_vision"))
            current["supports_image_generation"] = current["supports_image_generation"] or bool(model_config.get("supports_image_generation"))
            current["priced"] = current["priced"] or (
                model_config.get("input_price_per_1k") is not None
                or model_config.get("output_price_per_1k") is not None
            )
            try:
                stability_score = float(model_config.get("stability_score"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(stability_score):
                current["stability_scores"].append(stability_score)

        model_summaries = list(model_summary_map.values())
        model_stability_scores = [
            sum(item["stability_scores"]) / len(item["stability_scores"])
            for item in model_summaries
            if item["stability_scores"]
        ]
        average_stability = round(sum(model_stability_scores) / len(model_stability_scores), 2) if model_stability_scores else 0
        summary = {
            "provider_count": len(providers),
            "enabled_provider_count": enabled_provider_count,
            "model_count": len(model_summaries),
            "stream_model_count": sum(1 for item in model_summaries if item["supports_stream"]),
            "vision_model_count": sum(1 for item in model_summaries if item["supports_vision"]),
            "image_generation_model_count": sum(1 for item in model_summaries if item["supports_image_generation"]),
            "priced_model_count": sum(1 for item in model_summaries if item["priced"]),
            "avg_stability_score": average_stability,
        }
        telemetry_cards = [
            {"id": "provider_count", "label": "中转站总数", "value": summary["provider_count"]},
            {"id": "enabled_provider_count", "label": "已启用中转站", "value": summary["enabled_provider_count"]},
            {"id": "model_count", "label": "挂载模型数", "value": summary["model_count"]},
            {"id": "stream_model_count", "label": "支持 Stream", "value": summary["stream_model_count"]},
            {"id": "vision_model_count", "label": "支持图像理解", "value": summary["vision_model_count"]},
            {"id": "image_generation_model_count", "label": "支持图片生成", "value": summary["image_generation_model_count"]},
            {"id": "priced_model_count", "label": "已同步价格", "value": summary["priced_model_count"]},
            {"id": "avg_stability_score", "label": "平均稳定性", "value": average_stability},
        ]
        return {"providers": providers, "summary": summary, "telemetry_cards": telemetry_cards}

    @staticmethod
    def list_provider_option_dicts(db: Session) -> list[dict]:
        """返回适合下拉框和筛选器使用的轻量 provider 列表。"""
        providers = ProviderService.list_providers(db)
        return [ProviderService.provider_to_option_dict(provider) for provider in providers]

    @staticmethod
    def list_provider_playground_dicts(db: Session) -> list[dict]:
        """返回适合调用测试等页面使用的轻量 provider 列表。"""
        providers = ProviderService.list_providers(db)
        return [ProviderService.provider_to_playground_dict(provider) for provider in providers]

    @staticmethod
    def list_provider_summary_dicts(db: Session) -> list[dict]:
        """返回适合概览页使用的轻量 provider 列表。"""
        providers = ProviderService.list_providers(db)
        return [ProviderService.provider_to_summary_dict(provider) for provider in providers]

    @staticmethod
    def list_provider_model_mounts(
        db: Session,
        *,
        page: int,
        page_size: int,
        keyword: str | None = None,
        provider_id: int | None = None,
        enabled: bool | None = None,
        health_status: str | None = None,
    ) -> dict:
        """分页返回 provider model 挂载列表。"""
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        filters = []
        normalized_keyword = (keyword or "").strip()
        if normalized_keyword:
            pattern = f"%{normalized_keyword}%"
            filters.append(
                or_(
                    ProviderModel.model_name.ilike(pattern),
                    Provider.name.ilike(pattern),
                    Provider.group_name.ilike(pattern),
                    Provider.region_tag.ilike(pattern),
                    Provider.base_url.ilike(pattern),
                )
            )
        if provider_id is not None:
            filters.append(ProviderModel.provider_id == provider_id)
        if enabled is not None:
            filters.append(ProviderModel.enabled == enabled)
        normalized_health = (health_status or "").strip()
        if normalized_health:
            filters.append(ProviderModel.health_status == normalized_health)

        base_stmt = select(ProviderModel).join(Provider)
        if filters:
            base_stmt = base_stmt.where(*filters)
        total = int(db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0)
        provider_models = list(
            db.scalars(
                base_stmt
                .options(selectinload(ProviderModel.provider))
                .order_by(Provider.priority.asc(), Provider.id.asc(), ProviderModel.priority.asc(), ProviderModel.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        providers = list({item.provider.id: item.provider for item in provider_models if item.provider is not None}.values())
        metrics = ProviderService._build_quality_metrics(db, providers) if providers else {"provider_models": {}}
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "items": [
                ProviderService.provider_model_mount_to_dict(
                    item,
                    metrics=metrics["provider_models"].get(item.id),
                )
                for item in provider_models
                if item.provider is not None
            ],
        }

    @staticmethod
    def get_provider(db: Session, provider_id: int) -> Provider | None:
        """按 ID 读取 provider 及其模型挂载。"""
        return db.scalar(
            select(Provider).options(selectinload(Provider.provider_models)).where(Provider.id == provider_id)
        )

    @staticmethod
    def create_provider(db: Session, payload: ProviderCreate) -> Provider:
        """创建 provider，并同步初始化模型挂载和模型目录。"""
        from app.services.api_key_admin_service import ApiKeyAdminService
        from app.services.model_catalog_service import ModelCatalogService

        provider = Provider(
            name=payload.name,
            base_url=payload.base_url.rstrip("/"),
            api_key=payload.api_key,
            provider_type=payload.provider_type,
            group_name=payload.group_name,
            region_tag=payload.region_tag,
            enabled=payload.enabled,
            priority=payload.priority,
            weight=payload.weight,
            timeout_ms=payload.timeout_ms,
            max_retries=payload.max_retries,
            max_active_requests=payload.max_active_requests,
            max_active_streams=payload.max_active_streams,
            max_qps=payload.max_qps,
            max_rpm=payload.max_rpm,
            max_error_rate=payload.max_error_rate,
            first_token_timeout_sec=payload.first_token_timeout_sec,
            maintenance_window=payload.maintenance_window,
            maintenance_mode_enabled=payload.maintenance_mode_enabled,
            auto_circuit_break_enabled=payload.auto_circuit_break_enabled,
            auto_recover_enabled=payload.auto_recover_enabled,
            circuit_breaker_threshold_override=payload.circuit_breaker_threshold_override,
            recovery_probe_interval_sec_override=payload.recovery_probe_interval_sec_override,
            credential_rotated_at=datetime.utcnow(),
            remark=payload.remark,
        )
        db.add(provider)
        db.flush()
        ProviderService._replace_provider_models(db, provider, ProviderService._resolve_model_configs(payload))
        ProviderService.refresh_provider_state(provider)
        db.flush()
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ApiKeyAdminService.sync_auto_provider_bindings(db)
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    def batch_import_providers(db: Session, payload: ProviderBatchImportRequest) -> ProviderBatchImportResponse:
        """按模板解析并批量创建 provider。"""
        parsed_items = ProviderService._parse_batch_import_content(payload.content)
        existing_names = set(db.scalars(select(Provider.name)))
        result_items: list[dict] = []
        created_count = 0
        skipped_count = 0
        failed_count = 0

        for index, raw_item in enumerate(parsed_items, start=1):
            errors: list[str] = []
            provider_payload = ProviderService._normalize_batch_provider_item(raw_item, errors)
            name = provider_payload.get("name") if provider_payload else raw_item.get("name")
            base_url = provider_payload.get("base_url") if provider_payload else raw_item.get("base_url")
            model_count = len(provider_payload.get("model_configs", [])) if provider_payload else 0
            skipped = False
            created = False
            provider_dict = None

            if provider_payload and provider_payload["name"] in existing_names:
                if payload.skip_duplicates:
                    skipped = True
                    skipped_count += 1
                else:
                    errors.append("中转站名称已存在")

            if provider_payload and not errors and not payload.dry_run and not skipped:
                try:
                    provider = ProviderService.create_provider(db, ProviderCreate(**provider_payload))
                    settings = SettingService.get_or_create(db)
                    if settings.default_provider_id is None:
                        settings.default_provider_id = provider.id
                        db.commit()
                    existing_names.add(provider.name)
                    provider_dict = ProviderService.provider_to_dict(
                        provider,
                        metrics=ProviderService._build_quality_metrics(db, [provider]),
                    )
                    created = True
                    created_count += 1
                except Exception as exc:
                    errors.append(str(exc))

            if errors:
                failed_count += 1

            result_items.append({
                "index": index,
                "name": name,
                "base_url": base_url,
                "model_count": model_count,
                "valid": provider_payload is not None and not errors,
                "skipped": skipped,
                "created": created,
                "errors": errors,
                "provider": provider_dict,
            })

        valid_count = sum(1 for item in result_items if item["valid"])
        return ProviderBatchImportResponse(
            total=len(result_items),
            valid_count=valid_count,
            created_count=created_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            dry_run=payload.dry_run,
            template=ProviderService.BATCH_IMPORT_TEMPLATE,
            items=result_items,
        )

    @staticmethod
    def _parse_batch_import_content(content: str) -> list[dict]:
        normalized = (content or "").strip()
        if not normalized:
            return []
        parsed_json = ProviderService._try_parse_batch_import_json(normalized)
        if parsed_json is not None:
            return parsed_json
        blocks = ProviderService._split_batch_import_blocks(normalized)
        return [item for item in (ProviderService._parse_batch_import_block(block) for block in blocks) if item]

    @staticmethod
    def _try_parse_batch_import_json(content: str) -> list[dict] | None:
        try:
            payload = json.loads(content)
        except Exception:
            return None
        if isinstance(payload, dict):
            raw_items = payload.get("providers") or payload.get("items") or payload.get("channels")
        else:
            raw_items = payload
        if not isinstance(raw_items, list):
            return None
        return [item for item in raw_items if isinstance(item, dict)]

    @staticmethod
    def _split_batch_import_blocks(content: str) -> list[str]:
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        blocks: list[list[str]] = []
        current: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line in {"---", "----"}:
                if current:
                    blocks.append(current)
                    current = []
                continue
            if line.startswith("#"):
                continue
            if not current and not re.match(r"^([^:=：]+)\s*[:=：]\s*(.*)$", line):
                continue
            current.append(raw_line)
        if current:
            blocks.append(current)
        return ["\n".join(block).strip() for block in blocks if block]

    @staticmethod
    def _parse_batch_import_block(block: str) -> dict:
        item: dict[str, str] = {}
        current_key: str | None = None
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([^:=：]+)\s*[:=：]\s*(.*)$", line)
            if match:
                current_key = ProviderService._normalize_batch_import_key(match.group(1))
                if not current_key:
                    continue
                item[current_key] = match.group(2).strip()
                continue
            if current_key:
                item[current_key] = f"{item.get(current_key, '')}\n{line}".strip()
        return item

    @staticmethod
    def _normalize_batch_import_key(raw_key: str) -> str | None:
        normalized = re.sub(r"\s+", "", (raw_key or "").strip().lower())
        aliases = {
            "名称": "name",
            "渠道名称": "name",
            "中转站名称": "name",
            "name": "name",
            "baseurl": "base_url",
            "base_url": "base_url",
            "地址": "base_url",
            "接口地址": "base_url",
            "中转站地址": "base_url",
            "api地址": "base_url",
            "apikey": "api_key",
            "api_key": "api_key",
            "key": "api_key",
            "密钥": "api_key",
            "api密钥": "api_key",
            "类型": "provider_type",
            "type": "provider_type",
            "providertype": "provider_type",
            "provider_type": "provider_type",
            "分组": "group_name",
            "渠道分组": "group_name",
            "group": "group_name",
            "groupname": "group_name",
            "group_name": "group_name",
            "地区": "region_tag",
            "区域": "region_tag",
            "region": "region_tag",
            "regiontag": "region_tag",
            "region_tag": "region_tag",
            "优先级": "priority",
            "priority": "priority",
            "权重": "weight",
            "weight": "weight",
            "超时毫秒": "timeout_ms",
            "timeout": "timeout_ms",
            "timeoutms": "timeout_ms",
            "timeout_ms": "timeout_ms",
            "最大重试次数": "max_retries",
            "重试次数": "max_retries",
            "maxretries": "max_retries",
            "max_retries": "max_retries",
            "最大活跃请求": "max_active_requests",
            "最大并发请求": "max_active_requests",
            "maxactiverequests": "max_active_requests",
            "max_active_requests": "max_active_requests",
            "最大流式请求": "max_active_streams",
            "最大流式并发": "max_active_streams",
            "maxactivestreams": "max_active_streams",
            "max_active_streams": "max_active_streams",
            "最大qps": "max_qps",
            "qps": "max_qps",
            "maxqps": "max_qps",
            "max_qps": "max_qps",
            "每分钟最多请求": "max_rpm",
            "最大rpm": "max_rpm",
            "rpm": "max_rpm",
            "maxrpm": "max_rpm",
            "max_rpm": "max_rpm",
            "最大错误率": "max_error_rate",
            "最大错误率%": "max_error_rate",
            "maxerrorrate": "max_error_rate",
            "max_error_rate": "max_error_rate",
            "首token超时秒": "first_token_timeout_sec",
            "首tok超时秒": "first_token_timeout_sec",
            "firsttokentimeoutsec": "first_token_timeout_sec",
            "first_token_timeout_sec": "first_token_timeout_sec",
            "模型": "models",
            "模型列表": "models",
            "models": "models",
            "model": "models",
            "模型能力": "model_capabilities",
            "模型能力列表": "model_capabilities",
            "能力": "model_capabilities",
            "能力列表": "model_capabilities",
            "modelcapabilities": "model_capabilities",
            "model_capabilities": "model_capabilities",
            "capabilities": "model_capabilities",
            "启用": "enabled",
            "enabled": "enabled",
            "备注": "remark",
            "remark": "remark",
        }
        return aliases.get(normalized)

    @staticmethod
    def _normalize_batch_provider_item(raw_item: dict, errors: list[str]) -> dict | None:
        normalized = {
            ProviderService._normalize_batch_import_key(str(key)): value
            for key, value in raw_item.items()
            if ProviderService._normalize_batch_import_key(str(key))
        }
        name = ProviderService._clean_optional_text(normalized.get("name"))
        base_url = ProviderService._clean_optional_text(normalized.get("base_url"))
        api_key = ProviderService._clean_optional_text(normalized.get("api_key"))
        if not name:
            errors.append("缺少名称")
        if not base_url:
            errors.append("缺少 Base URL")
        if not api_key:
            errors.append("缺少 API Key")
        model_names = ProviderService._parse_batch_model_names(normalized.get("models"))
        if errors:
            return None
        model_capabilities = ProviderService._parse_batch_model_capabilities(normalized.get("model_capabilities"))
        model_configs = [
            ProviderService._build_batch_model_config(model_name, model_capabilities).model_dump()
            for model_name in model_names
        ]
        provider_payload = {
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "provider_type": ProviderService._clean_optional_text(normalized.get("provider_type")) or "openai_compatible",
            "group_name": ProviderService._clean_optional_text(normalized.get("group_name")),
            "region_tag": ProviderService._clean_optional_text(normalized.get("region_tag")),
            "enabled": ProviderService._parse_batch_bool(normalized.get("enabled"), default=True),
            "priority": ProviderService._parse_batch_int(normalized.get("priority"), default=100, minimum=0),
            "weight": ProviderService._parse_batch_int(normalized.get("weight"), default=100, minimum=0),
            "timeout_ms": ProviderService._parse_batch_int(normalized.get("timeout_ms"), default=30000, minimum=1000),
            "max_retries": ProviderService._parse_batch_int(normalized.get("max_retries"), default=1, minimum=0),
            "max_active_requests": ProviderService._parse_batch_nullable_int(normalized.get("max_active_requests"), default=20),
            "max_active_streams": ProviderService._parse_batch_nullable_int(normalized.get("max_active_streams"), default=10),
            "max_qps": ProviderService._parse_batch_nullable_int(normalized.get("max_qps"), default=20),
            "max_rpm": ProviderService._parse_batch_nullable_int(normalized.get("max_rpm"), default=20),
            "max_error_rate": ProviderService._parse_batch_float(normalized.get("max_error_rate"), default=80.0),
            "first_token_timeout_sec": ProviderService._parse_batch_nullable_int(normalized.get("first_token_timeout_sec"), default=60),
            "models": model_names,
            "model_configs": model_configs,
            "remark": ProviderService._clean_optional_text(normalized.get("remark")),
        }
        try:
            return ProviderCreate(**provider_payload).model_dump()
        except Exception as exc:
            errors.append(str(exc))
            return None

    @staticmethod
    def _clean_optional_text(value) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_batch_model_names(value) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = re.split(r"[,，、\n;；]+", str(value or ""))
        names: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            model_name = str(raw_item or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            names.append(model_name)
        return names

    @staticmethod
    def _default_batch_model_capabilities() -> dict[str, bool]:
        return {
            "supports_stream": True,
            "supports_vision": True,
            "supports_tools": True,
            "supports_chat_completions": True,
            "supports_responses": True,
        }

    @staticmethod
    def _parse_batch_model_capabilities(value) -> dict[str, bool]:
        defaults = ProviderService._default_batch_model_capabilities()
        text = str(value or "").strip().lower()
        if not text:
            return defaults
        tokens = {item for item in re.split(r"[,，、/\s;；]+", text) if item}
        disabled_tokens = {"否", "false", "0", "no", "off", "关闭", "停用", "none", "无"}
        if tokens & disabled_tokens:
            return {
                **defaults,
                "supports_stream": False,
                "supports_vision": False,
                "supports_tools": False,
            }
        stream_tokens = {"流式", "stream", "streaming", "sse"}
        vision_tokens = {"图像理解", "图片理解", "视觉", "vision", "image", "vl", "多模态"}
        tools_tokens = {"工具调用", "工具", "tools", "tool", "function", "functioncalling", "函数调用"}
        recognized = tokens & (stream_tokens | vision_tokens | tools_tokens | {"仅文本", "文本", "text"})
        if not recognized:
            return defaults
        return {
            **defaults,
            "supports_stream": bool(tokens & stream_tokens),
            "supports_vision": bool(tokens & vision_tokens),
            "supports_tools": bool(tokens & tools_tokens),
        }

    @staticmethod
    def _build_batch_model_config(model_name: str, capabilities: dict[str, bool]) -> ProviderModelConfigInput:
        return ProviderModelConfigInput(
            model_name=model_name,
            supports_stream=capabilities["supports_stream"],
            supports_vision=capabilities["supports_vision"],
            supports_tools=capabilities["supports_tools"],
            supports_chat_completions=capabilities["supports_chat_completions"],
            supports_responses=capabilities["supports_responses"],
        )

    @staticmethod
    def _parse_batch_bool(value, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "是", "启用", "开启"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "否", "停用", "关闭"}:
            return False
        return default

    @staticmethod
    def _parse_batch_int(value, *, default: int, minimum: int | None = None) -> int:
        try:
            parsed = int(float(str(value).strip()))
        except Exception:
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    @staticmethod
    def _parse_batch_nullable_int(value, *, default: int | None) -> int | None:
        text = str(value or "").strip()
        if text == "":
            return default
        parsed = ProviderService._parse_batch_int(text, default=default or 0, minimum=0)
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_batch_float(value, *, default: float) -> float:
        text = str(value or "").strip().replace("%", "")
        if text == "":
            return default
        try:
            return float(text)
        except Exception:
            return default

    @staticmethod
    def update_provider(db: Session, provider: Provider, payload: ProviderUpdate) -> Provider:
        """更新 provider 基础信息及模型挂载。"""
        from app.services.api_key_admin_service import ApiKeyAdminService
        from app.services.model_catalog_service import ModelCatalogService

        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if field in {"models", "model_configs"}:
                continue
            if field == "base_url" and isinstance(value, str):
                value = value.rstrip("/")
            setattr(provider, field, value)

        if "models" in data or "model_configs" in data:
            ProviderService._replace_provider_models(db, provider, ProviderService._resolve_model_configs(payload, provider))

        if {"base_url", "api_key"} & set(data.keys()):
            # 上游连接信息变化后，强制重置健康状态并等待重新探测。
            provider.health_status = "unknown"
            provider.circuit_state = "closed"
            for provider_model in provider.provider_models:
                provider_model.health_status = "unknown"
                provider_model.last_error = None

        ProviderService.refresh_provider_state(provider)
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ApiKeyAdminService.sync_auto_provider_bindings(db)
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    def delete_provider(db: Session, provider: Provider) -> None:
        from app.services.api_key_admin_service import ApiKeyAdminService
        from app.services.model_catalog_service import ModelCatalogService
        from app.services.api_key_auth_cache import ApiKeyAuthCache

        provider_id = provider.id
        affected_bindings = list(
            db.scalars(
                select(ApiClientKeyProviderBinding)
                .options(selectinload(ApiClientKeyProviderBinding.api_client_key))
                .where(ApiClientKeyProviderBinding.provider_id == provider_id)
            )
        )
        affected_default_keys = list(
            db.scalars(
                select(ApiClientKey).where(ApiClientKey.default_provider_id == provider_id)
            )
        )
        affected_key_refs = [
            (binding.api_client_key.id, binding.api_client_key.key_hash, binding.api_client_key.owner_user_id)
            for binding in affected_bindings
            if binding.api_client_key is not None
        ]
        affected_key_refs.extend(
            (api_key.id, api_key.key_hash, api_key.owner_user_id)
            for api_key in affected_default_keys
        )
        db.execute(update(AppSetting).where(AppSetting.default_provider_id == provider_id).values(default_provider_id=None))
        db.execute(update(ApiClientKey).where(ApiClientKey.default_provider_id == provider_id).values(default_provider_id=None))
        db.execute(update(RequestLog).where(RequestLog.provider_id == provider_id).values(provider_id=None))
        db.execute(delete(ApiClientKeyProviderBinding).where(ApiClientKeyProviderBinding.provider_id == provider_id))
        db.delete(provider)
        db.commit()
        for api_key_id, key_hash, owner_user_id in set(affected_key_refs):
            ApiKeyAuthCache.invalidate_api_key(api_key_id, key_hash)
            ApiKeyAuthCache.invalidate_user(owner_user_id)
        ApiKeyAdminService.sync_auto_provider_bindings(db)
        ProviderService.invalidate_provider_runtime_cache()
        ModelCatalogService.sync_model_catalogs(db)

    @staticmethod
    def update_provider_model(
        db: Session,
        provider: Provider,
        provider_model_id: int,
        payload: ProviderModelConfigUpdate,
    ) -> ProviderModel:
        from app.services.model_catalog_service import ModelCatalogService

        provider_model = next((item for item in provider.provider_models if item.id == provider_model_id), None)
        if provider_model is None:
            raise ValueError("Provider model not found")

        for field, value in payload.model_dump(exclude_unset=True).items():
            if field in {
                "supports_stream",
                "supports_vision",
                "supports_tools",
                "supports_chat_completions",
                "supports_responses",
                "context_window_tokens",
                "max_input_tokens",
                "max_output_tokens",
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
            }:
                continue
            if field == "price_multiplier" and value is None:
                continue
            setattr(provider_model, field, value)
        ProviderService._sync_provider_model_price_from_catalog(db, provider_model)

        ProviderService.refresh_provider_state(provider)
        db.flush()
        ModelCatalogService.sync_model_catalogs(db)
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider_model)
        return provider_model

    @staticmethod
    def provider_to_dict(provider: Provider, *, metrics: dict | None = None) -> dict:
        metrics = metrics or {"providers": {}, "provider_models": {}}
        provider_metric = metrics["providers"].get(provider.id, {})
        try:
            capacity_snapshot = ProviderCapacityService.snapshot(provider.id)
        except ProviderCapacityUnavailableError:
            capacity_snapshot = ProviderCapacitySnapshot()
        best_input_price = min(
            (item.input_price_per_1k for item in provider.provider_models if item.input_price_per_1k is not None),
            default=None,
        )
        best_output_price = min(
            (item.output_price_per_1k for item in provider.provider_models if item.output_price_per_1k is not None),
            default=None,
        )
        return {
            "id": provider.id,
            "name": provider.name,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "api_key_masked": ProviderService.mask_api_key(provider.api_key),
            "provider_type": provider.provider_type,
            "group_name": provider.group_name,
            "region_tag": provider.region_tag,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "weight": provider.weight,
            "timeout_ms": provider.timeout_ms,
            "max_retries": provider.max_retries,
            "max_active_requests": provider.max_active_requests,
            "max_active_streams": provider.max_active_streams,
            "max_qps": provider.max_qps,
            "max_rpm": provider.max_rpm,
            "max_error_rate": provider.max_error_rate,
            "first_token_timeout_sec": provider.first_token_timeout_sec,
            "active_requests": capacity_snapshot.active_requests,
            "active_streams": capacity_snapshot.active_streams,
            "current_qps": capacity_snapshot.current_qps,
            "current_rpm": capacity_snapshot.current_rpm,
            "maintenance_window": provider.maintenance_window,
            "maintenance_mode_enabled": provider.maintenance_mode_enabled,
            "auto_circuit_break_enabled": provider.auto_circuit_break_enabled,
            "auto_recover_enabled": provider.auto_recover_enabled,
            "circuit_breaker_threshold_override": provider.circuit_breaker_threshold_override,
            "recovery_probe_interval_sec_override": provider.recovery_probe_interval_sec_override,
            "models": [item.model_name for item in provider.provider_models],
            "model_configs": [
                ProviderService.provider_model_to_dict(item, metrics=metrics["provider_models"].get(item.id))
                for item in provider.provider_models
            ],
            "health_status": provider.health_status,
            "last_check_at": provider.last_check_at,
            "last_latency_ms": provider.last_latency_ms,
            "failure_count": provider.failure_count,
            "success_count": provider.success_count,
            "circuit_state": provider.circuit_state,
            "recent_request_count": provider_metric.get("recent_request_count", 0),
            "success_rate": provider_metric.get("success_rate"),
            "avg_first_token_latency_ms": provider_metric.get("avg_first_token_latency_ms"),
            "stability_score": provider_metric.get("stability_score"),
            "best_input_price_per_1k": best_input_price,
            "best_output_price_per_1k": best_output_price,
            "credential_rotated_at": provider.credential_rotated_at,
            "credential_hint": provider.credential_hint,
            "remark": provider.remark,
            "created_at": provider.created_at,
            "updated_at": provider.updated_at,
        }

    @staticmethod
    def provider_to_option_dict(provider: Provider) -> dict:
        return {
            "id": provider.id,
            "name": provider.name,
            "group_name": provider.group_name,
            "region_tag": provider.region_tag,
            "enabled": provider.enabled,
            "health_status": provider.health_status,
            "models": [item.model_name for item in provider.provider_models],
        }

    @staticmethod
    def provider_to_playground_dict(provider: Provider) -> dict:
        return {
            **ProviderService.provider_to_option_dict(provider),
            "base_url": provider.base_url,
            "model_configs": [
                {
                    "id": item.id,
                    "model_name": item.model_name,
                    "enabled": item.enabled,
                    "supports_stream": item.supports_stream,
                    "supports_vision": item.supports_vision,
                    "supports_tools": ProviderService.provider_model_supports_tools(item),
                    "supports_image_generation": ProviderService.provider_model_supports_image_generation(item),
                    "supports_chat_completions": item.supports_chat_completions,
                    "supports_responses": item.supports_responses,
                }
                for item in provider.provider_models
            ],
        }

    @staticmethod
    def provider_to_summary_dict(provider: Provider) -> dict:
        return {
            "id": provider.id,
            "name": provider.name,
            "group_name": provider.group_name,
            "region_tag": provider.region_tag,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "weight": provider.weight,
            "health_status": provider.health_status,
            "circuit_state": provider.circuit_state,
            "last_latency_ms": provider.last_latency_ms,
            "models": [item.model_name for item in provider.provider_models],
        }

    @staticmethod
    def get_effective_circuit_breaker_threshold(db: Session, provider: Provider) -> int:
        if provider.circuit_breaker_threshold_override is not None and provider.circuit_breaker_threshold_override > 0:
            return provider.circuit_breaker_threshold_override
        return max(1, SettingService.get_or_create(db).circuit_breaker_threshold)

    @staticmethod
    def get_effective_recovery_probe_interval_sec(db: Session, provider: Provider) -> int:
        if provider.recovery_probe_interval_sec_override is not None and provider.recovery_probe_interval_sec_override > 0:
            return provider.recovery_probe_interval_sec_override
        return max(10, SettingService.get_or_create(db).recovery_probe_interval_sec)

    @staticmethod
    def rotate_provider_credential(
        db: Session,
        provider: Provider,
        *,
        api_key: str,
        credential_hint: str | None,
    ) -> Provider:
        provider.api_key = api_key.strip()
        provider.credential_hint = credential_hint
        provider.credential_rotated_at = datetime.utcnow()
        provider.health_status = "unknown"
        provider.circuit_state = "closed"
        for provider_model in provider.provider_models:
            provider_model.health_status = "unknown"
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
            provider_model.last_error = None
        db.commit()
        ProviderService.invalidate_provider_runtime_cache()
        db.refresh(provider)
        return provider

    @staticmethod
    async def discover_models(
        db: Session,
        payload: ProviderDiscoverModelsIn,
    ) -> ProviderDiscoverModelsResponse:
        provider = None
        if payload.provider_id is not None:
            provider = ProviderService.get_provider(db, payload.provider_id)
            if provider is None:
                raise ValueError("Provider not found")
        base_url = payload.base_url or (provider.base_url if provider else None)
        api_key = payload.api_key or (provider.api_key if provider else None)
        if not base_url or not api_key:
            raise ValueError("必须提供 Base URL 和 API 密钥，或指定已存在的中转站")

        normalized_base_url = base_url.rstrip("/")
        timeout_ms = payload.timeout_ms or (provider.timeout_ms if provider else 30000)
        timeout_sec = max(1, int(timeout_ms / 1000))
        response = None
        try:
            response = await UpstreamClientService.get_client().get(
                f"{normalized_base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout_sec,
            )
            response.raise_for_status()
        except HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise ValueError(f"获取可用模型失败：{detail}") from exc
        except Exception as exc:
            raise ValueError(f"获取可用模型失败：{exc}") from exc

        try:
            body = response.json()
        except Exception as exc:
            raise ValueError(f"上游 /models 返回的不是合法 JSON：{exc}") from exc

        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ValueError("上游 /models 返回格式不符合预期，缺少 data 数组")

        existing_names = set(payload.existing_model_names or [])
        discovered_names: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("id") or "").strip()
            if not model_name or model_name in discovered_names:
                continue
            discovered_names.append(model_name)

        discovered_items = [
            ProviderDiscoveredModelOut(
                model_name=model_name,
                supports_stream=ProviderService._infer_model_capabilities(model_name)["supports_stream"],
                supports_vision=ProviderService._infer_model_capabilities(model_name)["supports_vision"],
                supports_tools=ProviderService._infer_model_capabilities(model_name)["supports_tools"],
                supports_chat_completions=ProviderService._infer_model_capabilities(model_name)["supports_chat_completions"],
                supports_responses=ProviderService._infer_model_capabilities(model_name)["supports_responses"],
                already_configured=model_name in existing_names,
            )
            for model_name in discovered_names
        ]
        return ProviderDiscoverModelsResponse(
            provider_name=provider.name if provider else None,
            source_base_url=normalized_base_url,
            total_models=len(discovered_items),
            items=discovered_items,
        )

    @staticmethod
    def availability_timeseries(
        db: Session,
        *,
        provider: Provider,
        window_hours: int,
        bucket_minutes: int,
    ) -> list[dict]:
        normalized_window_hours = max(1, min(window_hours, 24 * 30))
        normalized_bucket_minutes = max(5, min(bucket_minutes, 24 * 60))
        cache_key = f"provider-availability:{provider.id}:{normalized_window_hours}:{normalized_bucket_minutes}"
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return cached
        since = datetime.utcnow() - timedelta(hours=normalized_window_hours)
        logs = db.execute(
            select(
                RequestLog.created_at,
                RequestLog.success,
                RequestLog.latency_ms,
            )
            .where(
                RequestLog.provider_id == provider.id,
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .order_by(RequestLog.created_at.asc(), RequestLog.id.asc())
        ).all()
        buckets: dict[datetime, dict] = {}
        for created_at, success, latency_ms in logs:
            if created_at is None:
                continue
            minute_floor = created_at.replace(second=0, microsecond=0)
            bucket_minute = minute_floor.minute - (minute_floor.minute % normalized_bucket_minutes)
            bucket_start = minute_floor.replace(minute=bucket_minute)
            current = buckets.setdefault(
                bucket_start,
                {
                    "bucket_start": bucket_start,
                    "total_requests": 0,
                    "success_requests": 0,
                    "failed_requests": 0,
                    "latency_values": [],
                },
            )
            current["total_requests"] += 1
            current["success_requests"] += 1 if success else 0
            current["failed_requests"] += 0 if success else 1
            if latency_ms is not None:
                current["latency_values"].append(float(latency_ms))
        results = []
        for bucket_start in sorted(buckets.keys()):
            current = buckets[bucket_start]
            latency_values = current.pop("latency_values")
            total_requests = int(current["total_requests"] or 0)
            success_requests = int(current["success_requests"] or 0)
            current["success_rate"] = round((success_requests / total_requests) * 100, 2) if total_requests else 0.0
            current["avg_latency_ms"] = round(sum(latency_values) / len(latency_values), 2) if latency_values else None
            results.append(current)
        return CacheService.set(cache_key, results, ttl_seconds=ProviderService.AVAILABILITY_CACHE_TTL_SECONDS)

    @staticmethod
    def provider_model_to_dict(provider_model: ProviderModel, *, metrics: dict | None = None) -> dict:
        metrics = metrics or {}
        return {
            "id": provider_model.id,
            "model_name": provider_model.model_name,
            "enabled": provider_model.enabled,
            "priority": provider_model.priority,
            "weight": provider_model.weight,
            "health_status": provider_model.health_status,
            "circuit_state": provider_model.circuit_state,
            "circuit_opened_at": provider_model.circuit_opened_at,
            "last_check_at": provider_model.last_check_at,
            "last_latency_ms": provider_model.last_latency_ms,
            "failure_count": provider_model.failure_count,
            "success_count": provider_model.success_count,
            "last_error": provider_model.last_error,
            "supports_stream": provider_model.supports_stream,
            "supports_vision": provider_model.supports_vision,
            "supports_tools": ProviderService.provider_model_supports_tools(provider_model),
            "supports_image_generation": ProviderService.provider_model_supports_image_generation(provider_model),
            "supports_chat_completions": provider_model.supports_chat_completions,
            "supports_responses": provider_model.supports_responses,
            "context_window_tokens": provider_model.context_window_tokens,
            "max_input_tokens": provider_model.max_input_tokens,
            "max_output_tokens": provider_model.max_output_tokens,
            "price_multiplier": provider_model.price_multiplier,
            "input_price_per_1k": provider_model.input_price_per_1k,
            "output_price_per_1k": provider_model.output_price_per_1k,
            "cache_price_per_1k": provider_model.cache_price_per_1k,
            "recent_request_count": metrics.get("recent_request_count", 0),
            "success_rate": metrics.get("success_rate"),
            "avg_first_token_latency_ms": metrics.get("avg_first_token_latency_ms"),
            "stability_score": metrics.get("stability_score"),
            "created_at": provider_model.created_at,
            "updated_at": provider_model.updated_at,
        }

    @staticmethod
    def provider_model_mount_to_dict(provider_model: ProviderModel, *, metrics: dict | None = None) -> dict:
        provider = provider_model.provider
        return {
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "group_name": provider.group_name,
                "region_tag": provider.region_tag,
                "enabled": provider.enabled,
                "health_status": provider.health_status,
            },
            "model": ProviderService.provider_model_to_dict(provider_model, metrics=metrics),
        }

    @staticmethod
    def sync_legacy_provider_models(db: Session) -> None:
        providers = list(
            db.scalars(
                select(Provider)
                .options(selectinload(Provider.provider_models))
                .order_by(Provider.id.asc())
            )
        )
        catalog_names = set(db.scalars(select(ModelCatalog.model_name)))
        changed = False
        for provider in providers:
            if provider.provider_models:
                for provider_model in provider.provider_models:
                    if provider_model.model_name in catalog_names:
                        continue
                    inferred = ProviderService._infer_model_capabilities(provider_model.model_name)
                    if inferred["supports_stream"] and not provider_model.supports_stream:
                        provider_model.supports_stream = True
                        changed = True
                    if inferred["supports_vision"] and not provider_model.supports_vision:
                        provider_model.supports_vision = True
                        changed = True
                ProviderService._sync_models_json(provider)
                continue
            legacy_models = loads_json(provider.models_json, [])
            if not legacy_models:
                continue
            ProviderService._replace_provider_models(
                db,
                provider,
                [ProviderService._build_model_config_input_from_name(model_name) for model_name in legacy_models],
            )
            ProviderService.refresh_provider_state(provider)
            changed = True
        if changed:
            db.commit()

    @staticmethod
    def refresh_provider_state(provider: Provider) -> None:
        enabled_models = [item for item in provider.provider_models if item.enabled]
        ProviderService._sync_models_json(provider)
        provider.last_check_at = max((item.last_check_at for item in enabled_models if item.last_check_at), default=None)
        provider.last_latency_ms = next(
            (item.last_latency_ms for item in enabled_models if item.last_latency_ms is not None),
            provider.last_latency_ms,
        )

        if not provider.enabled or not enabled_models:
            provider.health_status = "unknown"
            provider.circuit_state = "closed"
            return

        statuses = {item.health_status for item in enabled_models}
        if statuses == {"healthy"}:
            provider.health_status = "healthy"
            provider.circuit_state = "closed"
            return
        if statuses == {"unhealthy"}:
            provider.health_status = "unhealthy"
            provider.circuit_state = "open" if all(item.circuit_state == "open" for item in enabled_models) else "closed"
            return
        if "healthy" in statuses or "degraded" in statuses:
            provider.health_status = "degraded"
            provider.circuit_state = "closed"
            return

        provider.health_status = "unknown"
        provider.circuit_state = "closed"

    @staticmethod
    def _resolve_model_configs(payload: ProviderCreate | ProviderUpdate, provider: Provider | None = None) -> list[ProviderModelConfigInput]:
        if getattr(payload, "model_configs", None):
            return list(payload.model_configs)
        models = getattr(payload, "models", None)
        if models is not None:
            return [ProviderService._build_model_config_input_from_name(item) for item in models]
        if provider is None:
            return []
        return [
            ProviderModelConfigInput(
                model_name=item.model_name,
                enabled=item.enabled,
                priority=item.priority,
                weight=item.weight,
                supports_stream=item.supports_stream,
                supports_vision=item.supports_vision,
                supports_tools=item.supports_tools,
                supports_chat_completions=item.supports_chat_completions,
                supports_responses=item.supports_responses,
                context_window_tokens=item.context_window_tokens,
                max_input_tokens=item.max_input_tokens,
                max_output_tokens=item.max_output_tokens,
                price_multiplier=item.price_multiplier or 1.0,
                input_price_per_1k=item.input_price_per_1k,
                output_price_per_1k=item.output_price_per_1k,
                cache_price_per_1k=item.cache_price_per_1k,
            )
            for item in provider.provider_models
        ]

    @staticmethod
    def _replace_provider_models(db: Session, provider: Provider, model_configs: list[ProviderModelConfigInput]) -> None:
        existing_by_name = {item.model_name: item for item in provider.provider_models}
        catalogs_by_name = ProviderService._ensure_model_catalogs_for_configs(db, model_configs)
        keep_names: set[str] = set()

        for config in model_configs:
            keep_names.add(config.model_name)
            provider_model = existing_by_name.get(config.model_name)
            if provider_model is None:
                provider_model = ProviderModel(provider=provider, model_name=config.model_name)
                db.add(provider_model)
            provider_model.enabled = config.enabled
            provider_model.priority = config.priority
            provider_model.weight = config.weight
            provider_model.price_multiplier = to_multiplier_decimal(config.price_multiplier)
            catalog = catalogs_by_name.get(config.model_name)
            if catalog is not None:
                ProviderService._sync_provider_model_from_catalog(provider_model, catalog)
            if provider_model.health_status not in {"healthy", "degraded", "unhealthy"}:
                provider_model.health_status = "unknown"
            if not provider_model.circuit_state:
                provider_model.circuit_state = "closed"

        for provider_model in list(provider.provider_models):
            if provider_model.model_name not in keep_names:
                db.delete(provider_model)

        ProviderService._sync_models_json(provider)

    @staticmethod
    def _ensure_model_catalogs_for_configs(
        db: Session,
        model_configs: list[ProviderModelConfigInput],
    ) -> dict[str, ModelCatalog]:
        model_names = [config.model_name for config in model_configs]
        if not model_names:
            return {}
        catalogs_by_name = {
            item.model_name: item
            for item in db.scalars(select(ModelCatalog).where(ModelCatalog.model_name.in_(model_names)))
        }
        for config in model_configs:
            if config.model_name in catalogs_by_name:
                continue
            normalized_pricing = ModelPricingService.normalize_catalog_pricing(
                pricing_mode=None,
                pricing_json=None,
                input_price_per_1k=config.input_price_per_1k,
                output_price_per_1k=config.output_price_per_1k,
                cache_price_per_1k=config.cache_price_per_1k,
            )
            catalog = ModelCatalog(
                model_name=config.model_name,
                display_name=None,
                enabled=True,
                supports_stream=config.supports_stream,
                supports_vision=config.supports_vision,
                supports_tools=config.supports_tools,
                supports_chat_completions=config.supports_chat_completions,
                supports_responses=config.supports_responses,
                context_window_tokens=config.context_window_tokens,
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens,
                pricing_mode=normalized_pricing["pricing_mode"],
                pricing_json=ModelPricingService.pricing_json_to_db_value(normalized_pricing["pricing_json"]),
                input_price_per_1k=normalized_pricing["input_price_per_1k"],
                output_price_per_1k=normalized_pricing["output_price_per_1k"],
                cache_price_per_1k=normalized_pricing["cache_price_per_1k"],
            )
            db.add(catalog)
            db.flush()
            catalogs_by_name[config.model_name] = catalog
        return catalogs_by_name

    @staticmethod
    def _sync_provider_model_price_from_catalog(db: Session, provider_model: ProviderModel) -> None:
        catalog = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == provider_model.model_name))
        if catalog is None:
            return
        ProviderService._sync_provider_model_from_catalog(provider_model, catalog)

    @staticmethod
    def _sync_provider_model_from_catalog(provider_model: ProviderModel, catalog: ModelCatalog) -> None:
        provider_model.supports_stream = catalog.supports_stream
        provider_model.supports_vision = catalog.supports_vision
        provider_model.supports_tools = catalog.supports_tools
        provider_model.supports_chat_completions = catalog.supports_chat_completions
        provider_model.supports_responses = catalog.supports_responses
        provider_model.context_window_tokens = catalog.context_window_tokens
        provider_model.max_input_tokens = catalog.max_input_tokens
        provider_model.max_output_tokens = catalog.max_output_tokens
        resolved_prices = ModelPricingService.resolve_catalog_prices_for_provider(
            pricing_mode=catalog.pricing_mode,
            pricing_json=catalog.pricing_json,
            input_price_per_1k=catalog.input_price_per_1k,
            output_price_per_1k=catalog.output_price_per_1k,
            cache_price_per_1k=catalog.cache_price_per_1k,
            price_multiplier=provider_model.price_multiplier,
        )
        provider_model.input_price_per_1k = resolved_prices["input_price_per_1k"]
        provider_model.output_price_per_1k = resolved_prices["output_price_per_1k"]
        provider_model.cache_price_per_1k = resolved_prices["cache_price_per_1k"]

    @staticmethod
    def _sync_models_json(provider: Provider) -> None:
        provider.models_json = dumps_json([item.model_name for item in provider.provider_models if item.enabled])

    @staticmethod
    def invalidate_provider_runtime_cache() -> None:
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")
        CacheService.invalidate_prefix("providers-runtime")
        CacheService.invalidate_prefix("provider-quality")
        CacheService.invalidate_prefix("provider-availability")

    @staticmethod
    def _build_quality_metrics(db: Session, providers: list[Provider]) -> dict[str, dict]:
        provider_ids = {item.id for item in providers}
        provider_model_map = {
            item.id: item
            for provider in providers
            for item in provider.provider_models
        }
        if not provider_ids:
            return {"providers": {}, "provider_models": {}}
        provider_stats, model_stats = ProviderService._load_quality_accumulators(db)

        provider_metrics = {
            provider.id: ProviderService._finalize_quality_snapshot(
                provider_stats.get(provider.id, ProviderService._empty_quality_accumulator()),
                health_status=provider.health_status,
                circuit_state=provider.circuit_state,
            )
            for provider in providers
        }
        provider_model_metrics = {
            provider_model.id: ProviderService._finalize_quality_snapshot(
                model_stats.get(provider_model.id, ProviderService._empty_quality_accumulator()),
                health_status=provider_model.health_status,
                circuit_state=provider_model.circuit_state,
            )
            for provider_model in provider_model_map.values()
        }
        return {"providers": provider_metrics, "provider_models": provider_model_metrics}

    @staticmethod
    def _load_quality_accumulators(db: Session) -> tuple[dict[int, dict], dict[int, dict]]:
        cache_key = "provider-quality:accumulators"
        cached = CacheService.get(cache_key)
        if isinstance(cached, dict):
            cached_provider_stats = cached.get("providers")
            cached_model_stats = cached.get("provider_models")
            if isinstance(cached_provider_stats, dict) and isinstance(cached_model_stats, dict):
                return (
                    {int(key): value for key, value in cached_provider_stats.items()},
                    {int(key): value for key, value in cached_model_stats.items()},
                )

        since = datetime.utcnow() - timedelta(minutes=ProviderService.QUALITY_WINDOW_MINUTES)
        rows = db.execute(
            select(
                RequestLog.provider_id,
                RequestLog.resolved_provider_model_id,
                RequestLog.success,
                RequestLog.first_token_latency_ms,
                RequestLog.trace_json,
            ).where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
        ).all()

        provider_stats: dict[int, dict] = defaultdict(ProviderService._empty_quality_accumulator)
        model_stats: dict[int, dict] = defaultdict(ProviderService._empty_quality_accumulator)

        for provider_id, resolved_provider_model_id, success, first_token_latency_ms, trace_json in rows:
            trace = loads_json(trace_json, [])
            if isinstance(trace, list):
                for item in trace:
                    if not isinstance(item, dict):
                        continue
                    trace_provider_id = item.get("provider_id")
                    provider_model_id = item.get("provider_model_id")
                    result = item.get("result")
                    if not isinstance(trace_provider_id, int):
                        continue
                    if result in ProviderService.TRACE_TERMINAL_SUCCESS_RESULTS:
                        ProviderService._register_attempt(provider_stats[trace_provider_id], success=True)
                        if isinstance(provider_model_id, int):
                            ProviderService._register_attempt(model_stats[provider_model_id], success=True)
                    elif result in ProviderService.TRACE_TERMINAL_FAILURE_RESULTS:
                        ProviderService._register_attempt(provider_stats[trace_provider_id], success=False)
                        if isinstance(provider_model_id, int):
                            ProviderService._register_attempt(model_stats[provider_model_id], success=False)

            if (
                success
                and isinstance(provider_id, int)
                and isinstance(resolved_provider_model_id, int)
                and first_token_latency_ms is not None
            ):
                ProviderService._register_first_token(provider_stats[provider_id], first_token_latency_ms)
                ProviderService._register_first_token(model_stats[resolved_provider_model_id], first_token_latency_ms)

        CacheService.set(
            cache_key,
            {
                "providers": {str(key): value for key, value in provider_stats.items()},
                "provider_models": {str(key): value for key, value in model_stats.items()},
            },
            ttl_seconds=ProviderService.QUALITY_CACHE_TTL_SECONDS,
        )
        return provider_stats, model_stats

    @staticmethod
    def _empty_quality_accumulator() -> dict[str, int | float]:
        return {
            "recent_request_count": 0,
            "success_count": 0,
            "first_token_sum": 0.0,
            "first_token_count": 0,
        }

    @staticmethod
    def _register_attempt(target: dict[str, int | float], *, success: bool) -> None:
        target["recent_request_count"] += 1
        if success:
            target["success_count"] += 1

    @staticmethod
    def _register_first_token(target: dict[str, int | float], first_token_latency_ms: int) -> None:
        target["first_token_sum"] += float(first_token_latency_ms)
        target["first_token_count"] += 1

    @staticmethod
    def _finalize_quality_snapshot(
        stats: dict[str, int | float],
        *,
        health_status: str,
        circuit_state: str,
    ) -> dict[str, int | float | None]:
        request_count = int(stats.get("recent_request_count", 0) or 0)
        success_count = int(stats.get("success_count", 0) or 0)
        first_token_count = int(stats.get("first_token_count", 0) or 0)
        avg_first_token_latency_ms = (
            round(float(stats["first_token_sum"]) / first_token_count, 2)
            if first_token_count
            else None
        )
        success_rate = round((success_count / request_count) * 100, 2) if request_count else None
        stability_score = ProviderService._calculate_stability_score(
            request_count=request_count,
            success_rate=success_rate,
            avg_first_token_latency_ms=avg_first_token_latency_ms,
            health_status=health_status,
            circuit_state=circuit_state,
        )
        return {
            "recent_request_count": request_count,
            "success_rate": success_rate,
            "avg_first_token_latency_ms": avg_first_token_latency_ms,
            "stability_score": stability_score,
        }

    @staticmethod
    def _calculate_stability_score(
        *,
        request_count: int,
        success_rate: float | None,
        avg_first_token_latency_ms: float | None,
        health_status: str,
        circuit_state: str,
    ) -> float:
        if request_count <= 0:
            base = {
                "healthy": 85.0,
                "degraded": 65.0,
                "unhealthy": 30.0,
                "unknown": 50.0,
            }.get(health_status, 50.0)
            if circuit_state == "half_open":
                base -= 8.0
            elif circuit_state == "open":
                base -= 20.0
        else:
            base = success_rate if success_rate is not None else 50.0
            if avg_first_token_latency_ms is not None:
                base -= min(35.0, avg_first_token_latency_ms / 100.0)
            if health_status == "degraded":
                base -= 10.0
            elif health_status == "unhealthy":
                base -= 25.0
            elif health_status == "unknown":
                base -= 5.0
            if circuit_state == "half_open":
                base -= 8.0
            elif circuit_state == "open":
                base -= 20.0
            if request_count < 5:
                base -= 5.0
        return round(max(0.0, min(100.0, base)), 2)
