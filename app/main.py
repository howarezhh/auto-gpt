from contextlib import asynccontextmanager
import asyncio
import logging
from uuid import uuid4

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import String, Text, inspect, text

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import AppSetting
from app.middleware.ip_management_middleware import IpManagementMiddleware
from app.models.request_log import RequestLog
from app.routers.auth import router as auth_router
from app.routers.api_keys import router as api_keys_router
from app.routers.api_key_policy_templates import router as api_key_policy_templates_router
from app.routers.benchmark import router as benchmark_router
from app.routers.content_guard import router as content_guard_router
from app.routers.playground_api import router as playground_api_router
from app.routers.dashboard import router as dashboard_router
from app.routers.conversations import router as conversations_router
from app.routers.health import router as health_router
from app.routers.ip_management import router as ip_management_router
from app.routers.logs import router as logs_router
from app.routers.logging_api import router as logging_api_router
from app.routers.metrics import router as metrics_router
from app.routers.models import router as models_router
from app.routers.pages import router as pages_router
from app.routers.provider_models import router as provider_models_router
from app.routers.providers import router as providers_router
from app.routers.proxy import router as proxy_router
from app.routers.settings import router as settings_router
from app.routers.user_accounts import router as user_accounts_router
from app.routers.user_portal import router as user_portal_router
from app.scheduler import scheduler
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.api_key_service import ApiClientAuthError, ApiKeyService
from app.services.concurrency_service import IngressConcurrencyLimitExceededError, IngressConcurrencyService
from app.services.log_service import LogService
from app.logging.adapters.exception_adapter import ExceptionLogRecorder
from app.logging.queue import LoggingQueue
from app.services.error_catalog_service import ErrorCatalogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.model_mapping_service import ModelMappingService
from app.services.openai_error_service import OpenAIErrorService
from app.services.provider_service import ProviderService
from app.services.proxy_request_context import (
    clear_current_provider_candidate,
    clear_current_request_headers_json,
    get_current_provider_candidate,
)
from app.services.redis_service import RedisService
from app.services.request_header_log_service import RequestHeaderLogService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.responses_chat_adapter_service import ResponsesChatAdapterService
from app.services.runtime_state_service import RuntimeStateService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService
from app.services.upstream_client import UpstreamClientService
from app.services.user_auth_service import require_admin_api_user
from app.tasks import configure_scheduler
from app.utils.decimal_utils import (
    DB_MONEY_PRECISION,
    DB_MONEY_SCALE,
    DB_MULTIPLIER_PRECISION,
    DB_MULTIPLIER_SCALE,
    DB_PRICE_PRECISION,
    DB_PRICE_SCALE,
)
from app.utils.json_utils import dumps_json, safeJsonParse
from app.utils.request_body_structure import summarize_request_body_structure
from app.utils.request_stream import RequestBodyReadTimeout, RequestBodyTooLarge, read_limited_request_body


settings = get_settings()
settings.validate_runtime_settings()
logger = logging.getLogger(__name__)
SCHEDULER_OWNER_LOCK_KEY = "scheduler:owner:web"
SCHEDULER_OWNER_LOCK_TTL_SECONDS = 120
_RELEASE_SCHEDULER_OWNER_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW_SCHEDULER_OWNER_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


async def _try_acquire_scheduler_owner(token: str) -> bool:
    if not settings.redis_url.strip():
        logger.warning("ENABLE_SCHEDULER=true but REDIS_URL is empty; skip scheduler owner acquisition")
        return False
    try:
        acquired = await RedisService.get_client().set(
            SCHEDULER_OWNER_LOCK_KEY,
            token,
            nx=True,
            ex=SCHEDULER_OWNER_LOCK_TTL_SECONDS,
        )
        return bool(acquired)
    except Exception as exc:
        logger.warning("Skip scheduler startup because scheduler owner lock is unavailable: %s", exc)
        return False


async def _release_scheduler_owner(token: str | None) -> None:
    if not token:
        return
    try:
        await RedisService.get_client().eval(_RELEASE_SCHEDULER_OWNER_LUA, 1, SCHEDULER_OWNER_LOCK_KEY, token)
    except Exception as exc:
        logger.warning("Failed to release scheduler owner lock: %s", exc)


async def _renew_scheduler_owner(token: str) -> None:
    interval = max(10, SCHEDULER_OWNER_LOCK_TTL_SECONDS // 3)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await RedisService.get_client().eval(
                _RENEW_SCHEDULER_OWNER_LUA,
                1,
                SCHEDULER_OWNER_LOCK_KEY,
                token,
                SCHEDULER_OWNER_LOCK_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Scheduler owner lock renewal failed; shutdown local scheduler: %s", exc)
            if scheduler.running:
                scheduler.shutdown(wait=False)
            return
        if int(renewed or 0) != 1:
            logger.warning("Scheduler owner lock lost; shutdown local scheduler")
            if scheduler.running:
                scheduler.shutdown(wait=False)
            return


def init_database(*, allow_production_ddl: bool = False) -> None:
    """开发或独立初始化入口使用的数据库 DDL/兼容迁移。"""
    if settings.is_production() and not allow_production_ddl:
        logger.warning("生产环境已跳过数据库 DDL/兼容迁移，请通过独立迁移或初始化步骤处理数据库结构")
        return
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _migrate_provider_capacity_columns(db)
        _migrate_provider_model_capacity_columns(db)
        _migrate_provider_metadata_columns(db)
        _drop_legacy_provider_weight_columns(db)
        _migrate_app_setting_concurrency_columns(db)
        _migrate_admin_audit_log_columns(db)
        api_key_columns_changed = _migrate_api_client_key_columns(db)
        _migrate_cache_price_columns(db)
        _migrate_typed_logging_event_columns(db)
        _migrate_model_mapping_table(db)
        _migrate_responses_chat_adapter_session_table(db)
        _migrate_ip_management_tables(db)
        _backfill_user_shared_wallet(db)
        _backfill_api_key_owner_users(db)
        _backfill_missing_user_billing_records(db)
        setting = db.get(AppSetting, 1)
        if setting is None:
            setting = AppSetting(id=1)
            db.add(setting)
            db.commit()
            db.refresh(setting)
        _backfill_provider_terminology(db)
        _backfill_provider_max_retries(db)
        if settings.enable_startup_heavy_sync:
            if api_key_columns_changed:
                ApiKeyAdminService.backfill_all_api_keys_to_all_providers(db)
            else:
                ApiKeyAdminService.sync_auto_provider_bindings(db)
            ProviderService.sync_legacy_provider_models(db)
        ResponsesChatAdapterService.sync_env_upstreams(db)
        if settings.enable_startup_heavy_sync:
            ModelCatalogService.sync_model_catalogs(db)
        ModelCatalogService.invalidate_model_runtime_cache()
        ProviderService.invalidate_provider_runtime_cache()
    finally:
        db.close()


def _should_run_startup_database_init() -> bool:
    """判断 Web worker 启动期是否允许执行内置数据库初始化。"""
    if not settings.enable_startup_db_init:
        return False
    if settings.is_production():
        logger.warning("生产环境 Web worker 启动阶段禁止执行 create_all 或自动 DDL，已忽略 ENABLE_STARTUP_DB_INIT")
        return False
    return True


def _get_table_columns(db, table_name: str) -> set[str]:
    """读取指定表的现有列名集合。"""
    inspector = inspect(db.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def CONTENT_GUARD_COMPAT_COLUMNS(
    *,
    true_default: str,
    false_default: str,
    datetime_type: str,
) -> dict[str, dict[str, str]]:
    """启动期开发兼容 DDL 的内容防护字段集中口径；正式环境仍以 SQL 迁移为准。"""
    return {
        "providers": {
            "trust_level": "ALTER TABLE providers ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'standard'",
            "content_integrity_status": "ALTER TABLE providers ADD COLUMN content_integrity_status TEXT NOT NULL DEFAULT 'unknown'",
            "content_integrity_score": "ALTER TABLE providers ADD COLUMN content_integrity_score INTEGER NOT NULL DEFAULT 80",
            "content_violation_count": "ALTER TABLE providers ADD COLUMN content_violation_count INTEGER NOT NULL DEFAULT 0",
            "last_content_violation_at": f"ALTER TABLE providers ADD COLUMN last_content_violation_at {datetime_type}",
            "content_guard_enabled": f"ALTER TABLE providers ADD COLUMN content_guard_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "buffer_stream_for_guard": f"ALTER TABLE providers ADD COLUMN buffer_stream_for_guard BOOLEAN NOT NULL DEFAULT {true_default}",
            "circuit_state": "ALTER TABLE providers ADD COLUMN circuit_state TEXT NOT NULL DEFAULT 'closed'",
            "circuit_opened_at": f"ALTER TABLE providers ADD COLUMN circuit_opened_at {datetime_type}",
        },
        "provider_models": {
            "circuit_state": "ALTER TABLE provider_models ADD COLUMN circuit_state TEXT NOT NULL DEFAULT 'closed'",
            "circuit_opened_at": f"ALTER TABLE provider_models ADD COLUMN circuit_opened_at {datetime_type}",
            "content_integrity_status": "ALTER TABLE provider_models ADD COLUMN content_integrity_status TEXT NOT NULL DEFAULT 'unknown'",
            "content_probe_last_passed_at": f"ALTER TABLE provider_models ADD COLUMN content_probe_last_passed_at {datetime_type}",
            "content_probe_last_failed_at": f"ALTER TABLE provider_models ADD COLUMN content_probe_last_failed_at {datetime_type}",
            "content_probe_failure_count": "ALTER TABLE provider_models ADD COLUMN content_probe_failure_count INTEGER NOT NULL DEFAULT 0",
            "content_probe_results_json": "ALTER TABLE provider_models ADD COLUMN content_probe_results_json TEXT",
        },
        "request_logs": {
            "content_guard_result": "ALTER TABLE request_logs ADD COLUMN content_guard_result TEXT",
            "content_guard_risk_level": "ALTER TABLE request_logs ADD COLUMN content_guard_risk_level TEXT",
            "content_guard_categories_json": "ALTER TABLE request_logs ADD COLUMN content_guard_categories_json TEXT",
            "content_guard_reason": "ALTER TABLE request_logs ADD COLUMN content_guard_reason TEXT",
            "content_guard_action": "ALTER TABLE request_logs ADD COLUMN content_guard_action TEXT",
            "content_guard_excerpt": "ALTER TABLE request_logs ADD COLUMN content_guard_excerpt TEXT",
            "content_guard_latency_ms": "ALTER TABLE request_logs ADD COLUMN content_guard_latency_ms INTEGER",
            "content_guard_buffer_wait_ms": "ALTER TABLE request_logs ADD COLUMN content_guard_buffer_wait_ms INTEGER",
            "content_guard_retry_provider_count": "ALTER TABLE request_logs ADD COLUMN content_guard_retry_provider_count INTEGER",
            "content_guard_final_strategy": "ALTER TABLE request_logs ADD COLUMN content_guard_final_strategy TEXT",
            "content_guard_confidence": "ALTER TABLE request_logs ADD COLUMN content_guard_confidence FLOAT",
            "content_guard_score_delta": "ALTER TABLE request_logs ADD COLUMN content_guard_score_delta INTEGER",
        },
        "app_settings": {
            "content_guard_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_precheck_auto_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_precheck_auto_enabled BOOLEAN NOT NULL DEFAULT {false_default}",
            "content_guard_block_on_high_risk": f"ALTER TABLE app_settings ADD COLUMN content_guard_block_on_high_risk BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_probe_interval_sec": "ALTER TABLE app_settings ADD COLUMN content_guard_probe_interval_sec INTEGER NOT NULL DEFAULT 3600",
            "content_guard_json_probe_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_json_probe_enabled BOOLEAN NOT NULL DEFAULT {false_default}",
            "content_guard_probe_protocol_type": "ALTER TABLE app_settings ADD COLUMN content_guard_probe_protocol_type TEXT NOT NULL DEFAULT 'chat_completions'",
            "content_guard_max_scan_bytes": "ALTER TABLE app_settings ADD COLUMN content_guard_max_scan_bytes INTEGER NOT NULL DEFAULT 16384",
            "content_guard_stream_buffer_max_bytes": "ALTER TABLE app_settings ADD COLUMN content_guard_stream_buffer_max_bytes INTEGER NOT NULL DEFAULT 16384",
            "content_guard_low_trust_requires_buffer": f"ALTER TABLE app_settings ADD COLUMN content_guard_low_trust_requires_buffer BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_rules_json": "ALTER TABLE app_settings ADD COLUMN content_guard_rules_json TEXT NOT NULL DEFAULT ''",
            "content_guard_high_risk_strategy": "ALTER TABLE app_settings ADD COLUMN content_guard_high_risk_strategy TEXT NOT NULL DEFAULT 'switch_provider'",
            "content_guard_max_detection_delay_ms": "ALTER TABLE app_settings ADD COLUMN content_guard_max_detection_delay_ms INTEGER NOT NULL DEFAULT 300",
            "content_guard_stream_mode": "ALTER TABLE app_settings ADD COLUMN content_guard_stream_mode TEXT NOT NULL DEFAULT 'buffer_300ms'",
            "content_guard_url_check_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_url_check_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_url_allowlist_json": "ALTER TABLE app_settings ADD COLUMN content_guard_url_allowlist_json TEXT NOT NULL DEFAULT ''",
            "content_guard_async_review_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_async_review_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_high_risk_confidence_threshold": "ALTER TABLE app_settings ADD COLUMN content_guard_high_risk_confidence_threshold INTEGER NOT NULL DEFAULT 85",
            "content_guard_enhanced_detection_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_detection_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_enhanced_illegal_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_illegal_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_enhanced_ad_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_ad_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_enhanced_custom_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_custom_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_enhanced_obfuscation_enabled": f"ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_obfuscation_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
            "content_guard_enhanced_threshold": "ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_threshold INTEGER NOT NULL DEFAULT 70",
            "content_guard_enhanced_context_window_chars": "ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_context_window_chars INTEGER NOT NULL DEFAULT 96",
        },
    }


def _drop_legacy_provider_weight_columns(db) -> None:
    """删除提供商和挂载模型旧 weight 列；不支持 DROP COLUMN 的数据库保留为冗余列。"""
    for table_name in ("providers", "provider_models"):
        if "weight" not in _get_table_columns(db, table_name):
            continue
        try:
            db.execute(text(f"ALTER TABLE {table_name} DROP COLUMN weight"))
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.warning("%s 旧 weight 列删除失败，保留为数据库历史冗余列: %s", table_name, exc)


def _migrate_provider_capacity_columns(db) -> None:
    """为 providers 表补充容量控制相关字段。"""
    existing_columns = _get_table_columns(db, "providers")
    additions = {
        "max_active_requests": "ALTER TABLE providers ADD COLUMN max_active_requests INTEGER DEFAULT 20",
        "max_active_streams": "ALTER TABLE providers ADD COLUMN max_active_streams INTEGER DEFAULT 10",
        "max_qps": "ALTER TABLE providers ADD COLUMN max_qps INTEGER DEFAULT 20",
        "max_rpm": "ALTER TABLE providers ADD COLUMN max_rpm INTEGER DEFAULT 20",
        "first_token_timeout_sec": "ALTER TABLE providers ADD COLUMN first_token_timeout_sec INTEGER DEFAULT 60",
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()


def _migrate_provider_model_capacity_columns(db) -> None:
    """为 provider_models 表补充模型挂载级容量控制字段。"""
    existing_columns = _get_table_columns(db, "provider_models")
    if not existing_columns:
        return
    additions = {
        "max_active_requests": "ALTER TABLE provider_models ADD COLUMN max_active_requests INTEGER",
        "max_active_streams": "ALTER TABLE provider_models ADD COLUMN max_active_streams INTEGER",
        "max_qps": "ALTER TABLE provider_models ADD COLUMN max_qps INTEGER",
        "max_rpm": "ALTER TABLE provider_models ADD COLUMN max_rpm INTEGER",
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()


def _backfill_provider_max_retries(db) -> None:
    """仅为缺失值补齐提供商默认最大重试次数。"""
    existing_columns = _get_table_columns(db, "providers")
    if "max_retries" not in existing_columns:
        return
    target = 2
    db.execute(
        text(
            "UPDATE providers SET max_retries = :target "
            "WHERE max_retries IS NULL"
        ),
        {"target": target},
    )
    db.commit()


def _backfill_provider_terminology(db) -> None:
    """将数据库文本字段中的历史中文对象名称统一为“提供商”。"""
    inspector = inspect(db.get_bind())
    preparer = db.get_bind().dialect.identifier_preparer
    legacy_full = "\u4e2d\u8f6c\u7ad9"
    legacy_short = "\u4e2d\u8f6c"
    current_name = "提供商"
    changed = False
    for table_name in inspector.get_table_names():
        quoted_table = preparer.quote(table_name)
        for column in inspector.get_columns(table_name):
            column_type = column.get("type")
            if not isinstance(column_type, (String, Text)):
                continue
            column_name = str(column["name"])
            quoted_column = preparer.quote(column_name)
            db.execute(
                text(
                    f"UPDATE {quoted_table} "
                    f"SET {quoted_column} = replace(replace({quoted_column}, :legacy_full, :current), :legacy_short, :current) "
                    f"WHERE {quoted_column} LIKE :legacy_pattern"
                ),
                {
                    "legacy_full": legacy_full,
                    "legacy_short": legacy_short,
                    "current": current_name,
                    "legacy_pattern": f"%{legacy_short}%",
                },
            )
            changed = True
    if changed:
        db.commit()


def _migrate_provider_metadata_columns(db) -> None:
    """为 providers 表补充路由、协议与运维治理字段。"""
    existing_columns = _get_table_columns(db, "providers")
    if not existing_columns:
        return
    dialect_name = db.get_bind().dialect.name
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    datetime_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
    content_guard_compat = CONTENT_GUARD_COMPAT_COLUMNS(
        true_default=true_default,
        false_default=false_default,
        datetime_type=datetime_type,
    )
    additions = {
        "group_name": "ALTER TABLE providers ADD COLUMN group_name TEXT",
        "region_tag": "ALTER TABLE providers ADD COLUMN region_tag TEXT",
        "protocol_type": "ALTER TABLE providers ADD COLUMN protocol_type TEXT NOT NULL DEFAULT 'both'",
        "maintenance_window": "ALTER TABLE providers ADD COLUMN maintenance_window TEXT",
        "maintenance_mode_enabled": f"ALTER TABLE providers ADD COLUMN maintenance_mode_enabled BOOLEAN NOT NULL DEFAULT {false_default}",
        "auto_circuit_break_enabled": f"ALTER TABLE providers ADD COLUMN auto_circuit_break_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
        "auto_recover_enabled": f"ALTER TABLE providers ADD COLUMN auto_recover_enabled BOOLEAN NOT NULL DEFAULT {true_default}",
        "circuit_breaker_threshold_override": "ALTER TABLE providers ADD COLUMN circuit_breaker_threshold_override INTEGER",
        "recovery_probe_interval_sec_override": "ALTER TABLE providers ADD COLUMN recovery_probe_interval_sec_override INTEGER",
        "credential_rotated_at": f"ALTER TABLE providers ADD COLUMN credential_rotated_at {datetime_type}",
        "credential_hint": "ALTER TABLE providers ADD COLUMN credential_hint TEXT",
        **content_guard_compat["providers"],
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()
    if "protocol_type" in existing_columns or changed:
        db.execute(
            text(
                "UPDATE providers SET protocol_type = 'both' "
                "WHERE protocol_type IS NULL OR protocol_type NOT IN ('both', 'chat_completions', 'responses')"
            )
        )
        db.commit()


def _migrate_app_setting_concurrency_columns(db) -> None:
    """为 app_settings 表补充并发、超时与限额字段。"""
    existing_columns = _get_table_columns(db, "app_settings")
    runtime_settings = get_settings()
    dialect_name = db.get_bind().dialect.name
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    content_guard_compat = CONTENT_GUARD_COMPAT_COLUMNS(
        true_default=true_default,
        false_default=false_default,
        datetime_type="TIMESTAMP" if dialect_name == "postgresql" else "DATETIME",
    )
    additions = {
        "global_max_request_tokens": "ALTER TABLE app_settings ADD COLUMN global_max_request_tokens INTEGER DEFAULT 0",
        "route_exhausted_retry_max_wait_seconds": "ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_max_wait_seconds INTEGER DEFAULT 600",
        "route_exhausted_retry_infinite_enabled": f"ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_infinite_enabled BOOLEAN DEFAULT {false_default}",
        "trusted_providers_only": f"ALTER TABLE app_settings ADD COLUMN trusted_providers_only BOOLEAN DEFAULT {false_default}",
        "max_candidate_count": "ALTER TABLE app_settings ADD COLUMN max_candidate_count INTEGER DEFAULT 10",
        "route_candidate_expand_count": "ALTER TABLE app_settings ADD COLUMN route_candidate_expand_count INTEGER DEFAULT 5",
        "max_v1_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_request_body_bytes INTEGER DEFAULT 20971520",
        "max_v1_chat_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_chat_request_body_bytes INTEGER DEFAULT 0",
        "max_v1_responses_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_responses_request_body_bytes INTEGER DEFAULT 0",
        "long_output_stream_threshold_tokens": "ALTER TABLE app_settings ADD COLUMN long_output_stream_threshold_tokens INTEGER DEFAULT 8192",
        "max_non_stream_response_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_non_stream_response_body_bytes INTEGER DEFAULT 20971520",
        "stream_token_capture_max_bytes": "ALTER TABLE app_settings ADD COLUMN stream_token_capture_max_bytes INTEGER DEFAULT 1048576",
        "max_logged_metadata_bytes": "ALTER TABLE app_settings ADD COLUMN max_logged_metadata_bytes INTEGER DEFAULT 1024",
        **content_guard_compat["app_settings"],
        "global_max_active_requests": f"ALTER TABLE app_settings ADD COLUMN global_max_active_requests INTEGER DEFAULT {runtime_settings.global_max_active_requests}",
        "global_max_active_streams": f"ALTER TABLE app_settings ADD COLUMN global_max_active_streams INTEGER DEFAULT {runtime_settings.global_max_active_streams}",
        "global_qps_limit": f"ALTER TABLE app_settings ADD COLUMN global_qps_limit INTEGER DEFAULT {runtime_settings.global_qps_limit}",
        "global_rpm_limit": f"ALTER TABLE app_settings ADD COLUMN global_rpm_limit INTEGER DEFAULT {runtime_settings.global_rpm_limit}",
        "account_qps_limit": f"ALTER TABLE app_settings ADD COLUMN account_qps_limit INTEGER DEFAULT {runtime_settings.account_qps_limit}",
        "account_rpm_limit": f"ALTER TABLE app_settings ADD COLUMN account_rpm_limit INTEGER DEFAULT {runtime_settings.account_rpm_limit}",
        "api_key_max_active_requests": f"ALTER TABLE app_settings ADD COLUMN api_key_max_active_requests INTEGER DEFAULT {runtime_settings.api_key_max_active_requests}",
        "api_key_max_active_streams": f"ALTER TABLE app_settings ADD COLUMN api_key_max_active_streams INTEGER DEFAULT {runtime_settings.api_key_max_active_streams}",
        "account_max_active_requests": f"ALTER TABLE app_settings ADD COLUMN account_max_active_requests INTEGER DEFAULT {runtime_settings.account_max_active_requests}",
        "account_max_active_streams": f"ALTER TABLE app_settings ADD COLUMN account_max_active_streams INTEGER DEFAULT {runtime_settings.account_max_active_streams}",
        "provider_max_active_requests": f"ALTER TABLE app_settings ADD COLUMN provider_max_active_requests INTEGER DEFAULT {runtime_settings.provider_max_active_requests}",
        "provider_max_active_streams": f"ALTER TABLE app_settings ADD COLUMN provider_max_active_streams INTEGER DEFAULT {runtime_settings.provider_max_active_streams}",
        "concurrency_lease_ttl_seconds": f"ALTER TABLE app_settings ADD COLUMN concurrency_lease_ttl_seconds INTEGER DEFAULT {runtime_settings.concurrency_lease_ttl_seconds}",
        "stream_connect_timeout_seconds": f"ALTER TABLE app_settings ADD COLUMN stream_connect_timeout_seconds INTEGER DEFAULT {runtime_settings.stream_connect_timeout_seconds}",
        "stream_first_token_timeout_seconds": f"ALTER TABLE app_settings ADD COLUMN stream_first_token_timeout_seconds INTEGER DEFAULT {runtime_settings.stream_first_token_timeout_seconds}",
        "stream_idle_timeout_seconds": f"ALTER TABLE app_settings ADD COLUMN stream_idle_timeout_seconds INTEGER DEFAULT {runtime_settings.stream_idle_timeout_seconds}",
        "stream_max_duration_seconds": f"ALTER TABLE app_settings ADD COLUMN stream_max_duration_seconds INTEGER DEFAULT {runtime_settings.stream_max_duration_seconds}",
        "responses_chat_adapter_enabled": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_enabled BOOLEAN DEFAULT {true_default if runtime_settings.responses_chat_adapter_enabled else false_default}",
        "responses_chat_adapter_storage_type": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_storage_type TEXT DEFAULT 'database'",
        "responses_chat_adapter_ttl_seconds": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_ttl_seconds INTEGER DEFAULT {runtime_settings.responses_chat_adapter_ttl_seconds}",
        "responses_chat_adapter_model_map_json": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_model_map_json TEXT DEFAULT ''",
        "responses_chat_adapter_max_tool_rounds": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_max_tool_rounds INTEGER DEFAULT {runtime_settings.responses_chat_adapter_max_tool_rounds}",
        "responses_chat_adapter_web_search_enabled": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_web_search_enabled BOOLEAN DEFAULT {true_default if runtime_settings.responses_chat_adapter_web_search_enabled else false_default}",
        "responses_chat_adapter_search_proxy_url": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_search_proxy_url TEXT DEFAULT ''",
        "responses_chat_adapter_upstream_base_url": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstream_base_url TEXT DEFAULT ''",
        "responses_chat_adapter_upstream_api_key": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstream_api_key TEXT DEFAULT ''",
        "responses_chat_adapter_upstreams_json": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstreams_json TEXT DEFAULT ''",
        "responses_chat_adapter_context_window_tokens": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_context_window_tokens INTEGER DEFAULT {runtime_settings.responses_chat_adapter_context_window_tokens}",
        "responses_chat_adapter_snapshot_max_bytes": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_snapshot_max_bytes INTEGER DEFAULT {runtime_settings.responses_chat_adapter_snapshot_max_bytes}",
        "responses_chat_adapter_db_cleanup_interval_seconds": f"ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_db_cleanup_interval_seconds INTEGER DEFAULT {runtime_settings.responses_chat_adapter_db_cleanup_interval_seconds}",
        "request_log_retention_days": "ALTER TABLE app_settings ADD COLUMN request_log_retention_days INTEGER DEFAULT 90",
        "admin_audit_log_retention_days": "ALTER TABLE app_settings ADD COLUMN admin_audit_log_retention_days INTEGER DEFAULT 180",
        "request_child_log_retention_days": "ALTER TABLE app_settings ADD COLUMN request_child_log_retention_days INTEGER DEFAULT 90",
        "exception_log_retention_days": "ALTER TABLE app_settings ADD COLUMN exception_log_retention_days INTEGER DEFAULT 180",
        "health_log_retention_days": "ALTER TABLE app_settings ADD COLUMN health_log_retention_days INTEGER DEFAULT 7",
        "billing_log_retention_days": "ALTER TABLE app_settings ADD COLUMN billing_log_retention_days INTEGER DEFAULT 365",
        "background_job_log_retention_days": "ALTER TABLE app_settings ADD COLUMN background_job_log_retention_days INTEGER DEFAULT 90",
        "user_operation_log_retention_days": "ALTER TABLE app_settings ADD COLUMN user_operation_log_retention_days INTEGER DEFAULT 180",
        "asset_log_retention_days": "ALTER TABLE app_settings ADD COLUMN asset_log_retention_days INTEGER DEFAULT 180",
        "alert_event_retention_days": "ALTER TABLE app_settings ADD COLUMN alert_event_retention_days INTEGER DEFAULT 180",
    }
    changed = False
    added_columns: set[str] = set()
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        added_columns.add(column)
        changed = True
    adapter_text_defaults = {
        "responses_chat_adapter_storage_type": runtime_settings.responses_chat_adapter_storage_type,
        "responses_chat_adapter_model_map_json": runtime_settings.responses_chat_adapter_model_map_json,
        "responses_chat_adapter_search_proxy_url": runtime_settings.responses_chat_adapter_search_proxy_url,
        "responses_chat_adapter_upstream_base_url": runtime_settings.responses_chat_adapter_upstream_base_url,
        "responses_chat_adapter_upstream_api_key": runtime_settings.responses_chat_adapter_upstream_api_key,
        "responses_chat_adapter_upstreams_json": runtime_settings.responses_chat_adapter_upstreams_json,
    }
    for column, value in adapter_text_defaults.items():
        if column not in added_columns or not str(value or "").strip():
            continue
        db.execute(
            text(f"UPDATE app_settings SET {column} = :value WHERE {column} IS NULL OR {column} = ''"),
            {"value": str(value)},
        )
    if changed:
        db.commit()


def _migrate_admin_audit_log_columns(db) -> None:
    """为后台审计表补齐统一日志体系需要的上下文字段。"""
    existing_columns = _get_table_columns(db, "admin_audit_logs")
    if not existing_columns:
        return
    additions = {
        "request_trace_id": "ALTER TABLE admin_audit_logs ADD COLUMN request_trace_id TEXT",
        "source_ip": "ALTER TABLE admin_audit_logs ADD COLUMN source_ip TEXT",
        "before_json": "ALTER TABLE admin_audit_logs ADD COLUMN before_json TEXT",
        "after_json": "ALTER TABLE admin_audit_logs ADD COLUMN after_json TEXT",
        "changed_fields_json": "ALTER TABLE admin_audit_logs ADD COLUMN changed_fields_json TEXT",
        "risk_level": "ALTER TABLE admin_audit_logs ADD COLUMN risk_level TEXT DEFAULT 'low'",
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()


def _migrate_typed_logging_event_columns(db) -> None:
    """为类型化日志事件表补齐详情页查询依赖字段。"""
    dialect_name = db.get_bind().dialect.name
    float_type = "DOUBLE PRECISION" if dialect_name == "postgresql" else "FLOAT"
    additions_by_table = {
        "request_content_guard_events": {
            "provider_id": "ALTER TABLE request_content_guard_events ADD COLUMN provider_id INTEGER",
            "provider_name": "ALTER TABLE request_content_guard_events ADD COLUMN provider_name TEXT",
            "provider_model_id": "ALTER TABLE request_content_guard_events ADD COLUMN provider_model_id INTEGER",
            "model_name": "ALTER TABLE request_content_guard_events ADD COLUMN model_name TEXT",
            "requested_model": "ALTER TABLE request_content_guard_events ADD COLUMN requested_model TEXT",
            "request_path": "ALTER TABLE request_content_guard_events ADD COLUMN request_path TEXT",
            "is_stream": "ALTER TABLE request_content_guard_events ADD COLUMN is_stream BOOLEAN",
            "confidence": f"ALTER TABLE request_content_guard_events ADD COLUMN confidence {float_type}",
            "score_delta": "ALTER TABLE request_content_guard_events ADD COLUMN score_delta INTEGER",
            "diagnostics_json": "ALTER TABLE request_content_guard_events ADD COLUMN diagnostics_json TEXT",
        },
        "asset_events": {
            "sha256_hex": "ALTER TABLE asset_events ADD COLUMN sha256_hex TEXT",
        },
    }
    changed = False
    for table_name, additions in additions_by_table.items():
        existing_columns = _get_table_columns(db, table_name)
        if not existing_columns:
            continue
        for column, ddl in additions.items():
            if column in existing_columns:
                continue
            db.execute(text(ddl))
            changed = True
    if changed:
        db.commit()


def _migrate_cache_price_columns(db) -> None:
    """为模型和日志表补充缓存计费与能力字段。"""
    dialect_name = db.get_bind().dialect.name
    price_type = f"NUMERIC({DB_PRICE_PRECISION}, {DB_PRICE_SCALE})"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    datetime_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
    content_guard_compat = CONTENT_GUARD_COMPAT_COLUMNS(
        true_default=true_default,
        false_default=false_default,
        datetime_type=datetime_type,
    )
    additions_by_table = {
        "provider_models": {
            "cache_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN cache_price_per_1k {price_type}",
            "cache_write_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN cache_write_price_per_1k {price_type}",
            "supports_tools": f"ALTER TABLE provider_models ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT {false_default}",
            "supports_image_generation": f"ALTER TABLE provider_models ADD COLUMN supports_image_generation BOOLEAN NOT NULL DEFAULT {false_default}",
            "supports_chat_completions": f"ALTER TABLE provider_models ADD COLUMN supports_chat_completions BOOLEAN NOT NULL DEFAULT {true_default}",
            "supports_responses": f"ALTER TABLE provider_models ADD COLUMN supports_responses BOOLEAN NOT NULL DEFAULT {true_default}",
            **content_guard_compat["provider_models"],
            "context_window_tokens": "ALTER TABLE provider_models ADD COLUMN context_window_tokens INTEGER",
            "max_input_tokens": "ALTER TABLE provider_models ADD COLUMN max_input_tokens INTEGER",
            "max_output_tokens": "ALTER TABLE provider_models ADD COLUMN max_output_tokens INTEGER",
        },
        "model_catalogs": {
            "cache_price_per_1k": f"ALTER TABLE model_catalogs ADD COLUMN cache_price_per_1k {price_type}",
            "supports_stream": f"ALTER TABLE model_catalogs ADD COLUMN supports_stream BOOLEAN NOT NULL DEFAULT {true_default}",
            "supports_vision": f"ALTER TABLE model_catalogs ADD COLUMN supports_vision BOOLEAN NOT NULL DEFAULT {false_default}",
            "supports_tools": f"ALTER TABLE model_catalogs ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT {false_default}",
            "supports_chat_completions": f"ALTER TABLE model_catalogs ADD COLUMN supports_chat_completions BOOLEAN NOT NULL DEFAULT {true_default}",
            "supports_responses": f"ALTER TABLE model_catalogs ADD COLUMN supports_responses BOOLEAN NOT NULL DEFAULT {true_default}",
            "context_window_tokens": "ALTER TABLE model_catalogs ADD COLUMN context_window_tokens INTEGER",
            "max_input_tokens": "ALTER TABLE model_catalogs ADD COLUMN max_input_tokens INTEGER",
            "max_output_tokens": "ALTER TABLE model_catalogs ADD COLUMN max_output_tokens INTEGER",
            "pricing_mode": "ALTER TABLE model_catalogs ADD COLUMN pricing_mode TEXT NOT NULL DEFAULT 'fixed'",
            "pricing_json": "ALTER TABLE model_catalogs ADD COLUMN pricing_json TEXT",
        },
        "request_logs": {
            "channel_price_cache_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_cache_per_1k {price_type}",
            "channel_price_cache_write_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_cache_write_per_1k {price_type}",
            "model_reasoning_effort": "ALTER TABLE request_logs ADD COLUMN model_reasoning_effort TEXT",
            "pricing_tier_key": "ALTER TABLE request_logs ADD COLUMN pricing_tier_key TEXT",
            "pricing_tier_name": "ALTER TABLE request_logs ADD COLUMN pricing_tier_name TEXT",
            "reasoning_tokens": "ALTER TABLE request_logs ADD COLUMN reasoning_tokens INTEGER",
            "prompt_audio_tokens": "ALTER TABLE request_logs ADD COLUMN prompt_audio_tokens INTEGER",
            "completion_audio_tokens": "ALTER TABLE request_logs ADD COLUMN completion_audio_tokens INTEGER",
            "accepted_prediction_tokens": "ALTER TABLE request_logs ADD COLUMN accepted_prediction_tokens INTEGER",
            "rejected_prediction_tokens": "ALTER TABLE request_logs ADD COLUMN rejected_prediction_tokens INTEGER",
            "token_source": "ALTER TABLE request_logs ADD COLUMN token_source TEXT",
            "upstream_usage_missing": "ALTER TABLE request_logs ADD COLUMN upstream_usage_missing BOOLEAN",
            "usage_details_json": "ALTER TABLE request_logs ADD COLUMN usage_details_json TEXT",
            "request_headers_json": "ALTER TABLE request_logs ADD COLUMN request_headers_json TEXT",
            **content_guard_compat["request_logs"],
        },
        "api_client_billing_records": {
            "cache_read_tokens": "ALTER TABLE api_client_billing_records ADD COLUMN cache_read_tokens INTEGER",
            "cache_write_tokens": "ALTER TABLE api_client_billing_records ADD COLUMN cache_write_tokens INTEGER",
            "unit_cache_read_price_per_1k": f"ALTER TABLE api_client_billing_records ADD COLUMN unit_cache_read_price_per_1k {price_type}",
            "unit_cache_write_price_per_1k": f"ALTER TABLE api_client_billing_records ADD COLUMN unit_cache_write_price_per_1k {price_type}",
        },
        "user_account_billing_records": {
            "cache_read_tokens": "ALTER TABLE user_account_billing_records ADD COLUMN cache_read_tokens INTEGER",
            "cache_write_tokens": "ALTER TABLE user_account_billing_records ADD COLUMN cache_write_tokens INTEGER",
            "unit_cache_read_price_per_1k": f"ALTER TABLE user_account_billing_records ADD COLUMN unit_cache_read_price_per_1k {price_type}",
            "unit_cache_write_price_per_1k": f"ALTER TABLE user_account_billing_records ADD COLUMN unit_cache_write_price_per_1k {price_type}",
        },
    }
    changed = False
    added_model_catalog_columns: set[str] = set()
    for table_name, additions in additions_by_table.items():
        existing_columns = _get_table_columns(db, table_name)
        if not existing_columns:
            continue
        for column, ddl in additions.items():
            if column in existing_columns:
                continue
            db.execute(text(ddl))
            if table_name == "model_catalogs":
                added_model_catalog_columns.add(column)
            changed = True
    if "supports_stream" in added_model_catalog_columns:
        db.execute(text(
            f"UPDATE model_catalogs SET supports_stream = {true_default} "
            "WHERE model_name IN (SELECT model_name FROM provider_models WHERE supports_stream = "
            f"{true_default})"
        ))
    if "supports_vision" in added_model_catalog_columns:
        db.execute(text(
            f"UPDATE model_catalogs SET supports_vision = {true_default} "
            "WHERE model_name IN (SELECT model_name FROM provider_models WHERE supports_vision = "
            f"{true_default})"
        ))
    if "supports_tools" in added_model_catalog_columns:
        db.execute(text(
            f"UPDATE model_catalogs SET supports_tools = {true_default} "
            "WHERE model_name IN (SELECT model_name FROM provider_models WHERE supports_tools = "
            f"{true_default})"
        ))
    if changed:
        db.commit()


def _migrate_api_client_key_columns(db) -> bool:
    """为 api_client_keys 表补充 API Key 管理运行字段。"""
    existing_columns = _get_table_columns(db, "api_client_keys")
    if not existing_columns:
        return False
    dialect_name = db.get_bind().dialect.name
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    additions = {
        "auto_sync_provider_bindings": f"ALTER TABLE api_client_keys ADD COLUMN auto_sync_provider_bindings BOOLEAN NOT NULL DEFAULT {true_default}",
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing_columns:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()
    return changed


def _migrate_model_mapping_table(db) -> None:
    """补齐模型映射配置表与索引。"""
    inspector = inspect(db.get_bind())
    if "model_mappings" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("model_mappings")}
        if "strategy" in columns:
            try:
                db.execute(text("ALTER TABLE model_mappings DROP COLUMN strategy"))
                db.commit()
            except Exception as exc:
                db.rollback()
                logging.warning("模型映射旧 strategy 列删除失败，保留为数据库历史冗余列: %s", exc)
        ModelMappingService.normalize_legacy_mapping_data(db)
        return
    Base.metadata.tables["model_mappings"].create(bind=db.get_bind(), checkfirst=True)
    db.commit()
    ModelMappingService.invalidate_cache()


def _migrate_responses_chat_adapter_session_table(db) -> None:
    """补齐 Responses→Chat 兼容适配层会话表。"""
    inspector = inspect(db.get_bind())
    if "responses_chat_adapter_sessions" in inspector.get_table_names():
        return
    Base.metadata.tables["responses_chat_adapter_sessions"].create(bind=db.get_bind(), checkfirst=True)
    db.commit()


def _migrate_ip_management_tables(db) -> None:
    """补齐 IP 管理模块独立表。"""
    for table_name in ("ip_management_settings", "ip_access_rules", "ip_management_events"):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.create(bind=db.get_bind(), checkfirst=True)
    db.commit()


def _backfill_user_shared_wallet(db) -> None:
    user_columns = _get_table_columns(db, "user_accounts")
    if "balance_amount" not in user_columns or "total_recharge_amount" not in user_columns:
        return
    api_key_columns = _get_table_columns(db, "api_client_keys")
    if not {"owner_user_id", "balance_amount", "total_recharge_amount"}.issubset(api_key_columns):
        return
    rows = db.execute(
        text(
            """
            SELECT owner_user_id,
                   COALESCE(SUM(COALESCE(balance_amount, 0)), 0) AS total_balance_amount,
                   COALESCE(SUM(COALESCE(total_recharge_amount, 0)), 0) AS total_recharge_amount
            FROM api_client_keys
            WHERE owner_user_id IS NOT NULL
            GROUP BY owner_user_id
            """
        )
    ).fetchall()
    changed = False
    for owner_user_id, total_balance_amount, total_recharge_amount in rows:
        result = db.execute(
            text(
                """
                UPDATE user_accounts
                SET balance_amount = CASE
                        WHEN COALESCE(balance_amount, 0) = 0 THEN :total_balance_amount
                        ELSE balance_amount
                    END,
                    total_recharge_amount = CASE
                        WHEN COALESCE(total_recharge_amount, 0) = 0 THEN :total_recharge_amount
                        ELSE total_recharge_amount
                    END
                WHERE id = :owner_user_id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "total_balance_amount": total_balance_amount,
                "total_recharge_amount": total_recharge_amount,
            },
        )
        if result.rowcount:
            changed = True
    if changed:
        db.commit()


def _backfill_api_key_owner_users(db) -> None:
    api_key_columns = _get_table_columns(db, "api_client_keys")
    user_columns = _get_table_columns(db, "user_accounts")
    if "owner_user_id" not in api_key_columns or "id" not in user_columns or "enabled" not in user_columns:
        return
    dialect_name = db.get_bind().dialect.name
    enabled_expr = "enabled IS TRUE" if dialect_name == "postgresql" else "COALESCE(enabled, 0) = 1"
    default_owner_id = db.scalar(
        text(
            f"""
            SELECT id
            FROM user_accounts
            WHERE {enabled_expr}
            ORDER BY id ASC
            LIMIT 1
            """
        )
    )
    if default_owner_id is None:
        null_count = db.scalar(text("SELECT COUNT(*) FROM api_client_keys WHERE owner_user_id IS NULL")) or 0
        if int(null_count):
            logging.warning("存在未绑定归属用户的 API Key，但当前没有可用于回填的启用用户")
        return
    result = db.execute(
        text("UPDATE api_client_keys SET owner_user_id = :owner_user_id WHERE owner_user_id IS NULL"),
        {"owner_user_id": default_owner_id},
    )
    if result.rowcount:
        db.commit()


def _backfill_missing_user_billing_records(db) -> None:
    batch_size = max(1, min(int(settings.startup_billing_backfill_batch_size or 5000), 50000))
    required_columns = {
        "api_client_billing_records": {
            "id",
            "api_client_key_id",
            "request_log_id",
            "record_type",
            "amount",
            "balance_after",
            "provider_id",
            "provider_name",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "unit_input_price_per_1k",
            "unit_output_price_per_1k",
            "unit_cache_read_price_per_1k",
            "unit_cache_write_price_per_1k",
            "remark",
            "created_at",
        },
        "api_client_keys": {"id", "owner_user_id"},
        "user_account_billing_records": {
            "user_account_id",
            "api_client_key_id",
            "request_log_id",
            "record_type",
            "amount",
            "balance_after",
            "provider_id",
            "provider_name",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "unit_input_price_per_1k",
            "unit_output_price_per_1k",
            "unit_cache_read_price_per_1k",
            "unit_cache_write_price_per_1k",
            "remark",
            "created_at",
        },
    }
    for table_name, columns in required_columns.items():
        existing_columns = _get_table_columns(db, table_name)
        if not columns.issubset(existing_columns):
            return
    result = db.execute(
        text(
            """
            INSERT INTO user_account_billing_records (
                user_account_id,
                api_client_key_id,
                request_log_id,
                record_type,
                amount,
                balance_after,
                provider_id,
                provider_name,
                model_name,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cache_read_tokens,
                cache_write_tokens,
                unit_input_price_per_1k,
                unit_output_price_per_1k,
                unit_cache_read_price_per_1k,
                unit_cache_write_price_per_1k,
                remark,
                created_at
            )
            SELECT
                api_key.owner_user_id,
                record.api_client_key_id,
                record.request_log_id,
                record.record_type,
                record.amount,
                record.balance_after,
                record.provider_id,
                record.provider_name,
                record.model_name,
                record.prompt_tokens,
                record.completion_tokens,
                record.total_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.unit_input_price_per_1k,
                record.unit_output_price_per_1k,
                record.unit_cache_read_price_per_1k,
                record.unit_cache_write_price_per_1k,
                record.remark,
                record.created_at
            FROM api_client_billing_records record
            JOIN api_client_keys api_key ON api_key.id = record.api_client_key_id
            WHERE record.record_type = 'request_charge'
              AND record.request_log_id IS NOT NULL
              AND api_key.owner_user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM user_account_billing_records existing
                  WHERE existing.request_log_id = record.request_log_id
              )
            ORDER BY record.id ASC
            LIMIT :batch_size
            """
        ),
        {"batch_size": batch_size},
    )
    if result.rowcount:
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = max(40, int(settings.worker_threadpool_tokens or 40))
    redis_started = False
    logging_started = False
    request_log_started = False
    token_usage_started = False
    scheduler_started = False
    scheduler_owner_token: str | None = None
    scheduler_owner_renew_task: asyncio.Task | None = None
    if _should_run_startup_database_init():
        init_database()
    try:
        await RedisService.init()
        redis_started = True
        await RuntimeStateService.start_event_loop_monitor()
        UpstreamClientService.get_client()
        if settings.enable_background_workers:
            await LoggingQueue.start_background_workers()
            logging_started = True
            await RequestLogQueueService.start_background_workers()
            request_log_started = True
            await TokenUsageService.start_background_workers()
            token_usage_started = True
        if settings.enable_scheduler and not scheduler.running:
            scheduler_owner_token = uuid4().hex
            if await _try_acquire_scheduler_owner(scheduler_owner_token):
                configure_scheduler()
                scheduler.start()
                scheduler_started = True
                scheduler_owner_renew_task = asyncio.create_task(
                    _renew_scheduler_owner(scheduler_owner_token),
                    name="scheduler-owner-renew",
                )
        yield
    finally:
        if scheduler_owner_renew_task is not None:
            scheduler_owner_renew_task.cancel()
            await asyncio.gather(scheduler_owner_renew_task, return_exceptions=True)
        if scheduler_started and scheduler.running:
            scheduler.shutdown(wait=False)
        await _release_scheduler_owner(scheduler_owner_token if scheduler_started else None)
        if request_log_started:
            await RequestLogQueueService.stop_background_workers()
        if token_usage_started:
            await TokenUsageService.stop_background_workers()
        if logging_started:
            await LoggingQueue.stop_background_workers()
        await RuntimeStateService.stop_event_loop_monitor()
        await UpstreamClientService.aclose()
        await ApiKeyAuthCache.aclose()
        if redis_started:
            await RedisService.aclose()


app = FastAPI(
    title="aotu-gpt",
    lifespan=lifespan,
    docs_url="/api-docs",
    redoc_url="/api-redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
app.add_middleware(IpManagementMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploaded-assets", StaticFiles(directory=settings.uploads_dir), name="uploaded-assets")


@app.middleware("http")
async def trace_and_runtime_middleware(request: Request, call_next):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("x-trace-id") or request.headers.get("x-request-id") or uuid4().hex
    request.state.trace_id = trace_id
    clear_current_provider_candidate()
    clear_current_request_headers_json()
    if _is_external_v1_path(request.url.path):
        RequestHeaderLogService.capture_request(request)
    RuntimeStateService.enter_request()
    ingress_lease = None
    try:
        body_limit_response = _reject_oversized_v1_request_by_content_length(request)
        if body_limit_response is not None:
            _apply_v1_cors_headers(request, body_limit_response)
            return body_limit_response
        ingress_lease, ingress_response = await _acquire_v1_ingress_concurrency_or_response(request)
        if ingress_response is not None:
            _apply_v1_cors_headers(request, ingress_response)
            return ingress_response
        response = await call_next(request)
    except Exception as exc:
        if _is_external_v1_path(request.url.path):
            response = _build_unhandled_v1_error_response(request, exc)
            _apply_v1_cors_headers(request, response)
            return response
        raise
    finally:
        if ingress_lease is not None:
            try:
                await IngressConcurrencyService.release(ingress_lease)
            except Exception as exc:
                logger.warning("Failed to release v1 ingress concurrency lease: %s", exc)
        RuntimeStateService.leave_request()
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Request-Id"] = trace_id
    response.headers["X-Active-Requests"] = str(RuntimeStateService.current_active_requests())
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif request.url.path.startswith("/uploaded-assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    _apply_v1_cors_headers(request, response)
    return response


async def _acquire_v1_ingress_concurrency_or_response(request: Request):
    if not _is_external_v1_path(request.url.path):
        return None, None
    app_setting = SettingService.get_cached()
    trace_id = getattr(request.state, "trace_id", None)
    try:
        lease = await IngressConcurrencyService.acquire(
            request_id=trace_id or uuid4().hex,
            ttl_seconds=int(getattr(app_setting, "concurrency_lease_ttl_seconds", 900) or 900),
            max_active_requests=int(getattr(app_setting, "global_max_active_requests", 0) or 0),
        )
        return lease, None
    except IngressConcurrencyLimitExceededError as exc:
        message = "入口并发已达到系统上限，请稍后重试"
        detail = {
            "message": message,
            "code": exc.code,
            "max_active_requests": int(getattr(app_setting, "global_max_active_requests", 0) or 0),
        }
        _log_v1_request_rejected_before_route(
            request=request,
            status_code=429,
            message=message,
            error_code=exc.code,
            retryable=True,
            detail=detail,
        )
        return None, JSONResponse(
            status_code=429,
            content=OpenAIErrorService.build_error_payload(
                message=message,
                code=exc.code,
                trace_id=trace_id,
                error_type="rate_limit_error",
                retryable=True,
                recoverable=True,
                category="capacity_limited",
                status_code=429,
                detail=detail,
            ),
            headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
        )
    except Exception as exc:
        message = "入口并发保护依赖 Redis 暂时不可用"
        detail = {"message": message, "code": "redis_unavailable", "exception_type": exc.__class__.__name__}
        _log_v1_request_rejected_before_route(
            request=request,
            status_code=503,
            message=message,
            error_code="redis_unavailable",
            retryable=True,
            detail=detail,
        )
        return None, JSONResponse(
            status_code=503,
            content=OpenAIErrorService.build_error_payload(
                message=message,
                code="redis_unavailable",
                trace_id=trace_id,
                error_type="server_error",
                retryable=True,
                recoverable=True,
                category="capacity_limited",
                status_code=503,
                detail=detail,
            ),
            headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
        )


def _reject_oversized_v1_request_by_content_length(request: Request) -> JSONResponse | None:
    if not _is_external_v1_path(request.url.path) or request.method.upper() not in {"POST", "PUT", "PATCH"}:
        return None
    app_setting = SettingService.get_cached()
    limit = _effective_v1_body_limit(app_setting, request.url.path)
    if limit <= 0:
        return None
    content_length = request.headers.get("content-length")
    try:
        request_bytes = int(content_length) if content_length is not None else None
    except ValueError:
        request_bytes = None
    if request_bytes is None or request_bytes <= limit:
        return None
    trace_id = getattr(request.state, "trace_id", None)
    _log_v1_request_rejected_before_route(
        request=request,
        status_code=413,
        message=f"请求体大小 {request_bytes} 字节超过应用层上限 {limit} 字节",
        error_code="request_body_too_large",
        retryable=False,
        request_body_json=ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure omitted because Content-Length exceeds application limit",
                "structure": {
                    "type": "bytes",
                    "content_length": request_bytes,
                    "max_v1_request_body_bytes": limit,
                },
            },
            4096,
        ),
    )
    return JSONResponse(
        status_code=413,
        content=OpenAIErrorService.build_error_payload(
            message=f"请求体大小 {request_bytes} 字节超过应用层上限 {limit} 字节",
            code="request_body_too_large",
            trace_id=trace_id,
            error_type="invalid_request_error",
            retryable=False,
            recoverable=False,
            category="invalid_request",
            status_code=413,
            detail={
                "request_body_bytes": request_bytes,
                "max_v1_request_body_bytes": limit,
            },
        ),
        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
    )


def _effective_v1_body_limit(app_setting: AppSetting, request_path: str) -> int:
    global_limit = int(getattr(app_setting, "max_v1_request_body_bytes", 0) or 0)
    endpoint_limit = 0
    if request_path in {"/v1/chat/completions", "/v1/completions"}:
        endpoint_limit = int(getattr(app_setting, "max_v1_chat_request_body_bytes", 0) or 0)
    elif request_path == "/v1/responses":
        endpoint_limit = int(getattr(app_setting, "max_v1_responses_request_body_bytes", 0) or 0)
    positive_limits = [item for item in (global_limit, endpoint_limit) if item > 0]
    return min(positive_limits) if positive_limits else 0


def _is_external_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _apply_v1_cors_headers(request: Request, response) -> None:
    if not _is_external_v1_path(request.url.path):
        return
    origin = request.headers.get("origin")
    response.headers["Access-Control-Allow-Origin"] = origin or "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        request.headers.get("access-control-request-headers")
        or "authorization,content-type,x-request-id,x-trace-id"
    )
    response.headers["Access-Control-Expose-Headers"] = (
        "x-request-id,x-trace-id,x-proxy-provider-id,x-proxy-provider-name,x-proxy-latency-ms"
    )
    response.headers["Vary"] = "Origin"


@app.exception_handler(ApiClientAuthError)
async def api_client_auth_error_handler(request: Request, exc: ApiClientAuthError):
    await _log_api_client_auth_failure(request, exc)
    trace_id = getattr(request.state, "trace_id", None)
    classified = OpenAIErrorService.classify_error(
        status_code=exc.status_code,
        detail={"message": exc.message, "code": exc.code},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=OpenAIErrorService.build_error_payload(
            message=exc.message,
            code=exc.code,
            trace_id=trace_id,
            error_type=classified["error_type"],
            retryable=bool(classified["retryable"]),
            recoverable=bool(classified["recoverable"]),
            category=str(classified["category"]),
            status_code=exc.status_code,
        ),
        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    _record_exception_event(
        request=request,
        exc=exc,
        handler_name="request_validation_exception_handler",
        status_code=422,
        error_code="request_validation_failed",
        message="Request validation failed",
        detail={"errors": _make_json_safe(exc.errors())},
        severity="warning",
    )
    if not _is_external_v1_path(request.url.path):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    trace_id = getattr(request.state, "trace_id", None)
    detail = {"errors": _make_json_safe(exc.errors())}
    _log_v1_request_rejected_before_route(
        request=request,
        status_code=422,
        message="Request validation failed",
        error_code="request_validation_failed",
        retryable=False,
        detail=detail,
        request_body_json=getattr(request.state, "v1_request_body_structure_json", None),
    )
    return JSONResponse(
        status_code=422,
        content=OpenAIErrorService.build_error_payload(
            message="Request validation failed",
            code="request_validation_failed",
            trace_id=trace_id,
            error_type="invalid_request_error",
            retryable=False,
            recoverable=False,
            category="invalid_request",
            status_code=422,
            detail=detail,
        ),
        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail_payload = exc.detail if isinstance(exc.detail, dict) else None
    classified_for_log = OpenAIErrorService.classify_error(status_code=exc.status_code, detail=detail_payload)
    if exc.status_code >= 500:
        _record_exception_event(
            request=request,
            exc=exc,
            handler_name="http_exception_handler",
            status_code=exc.status_code,
            error_code=str(classified_for_log.get("code") or getattr(exc, "status_code", "http_exception")),
            message=str(exc.detail),
            detail=detail_payload,
            severity="danger",
        )
    if not _is_external_v1_path(request.url.path):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    trace_id = getattr(request.state, "trace_id", None)
    error_object = ErrorCatalogService.build_error_object(
        status_code=exc.status_code,
        detail=detail_payload if detail_payload is not None else exc.detail,
        trace_id=trace_id,
    )
    message = str(error_object["message"])
    error_code = str(error_object["code"])
    classified = OpenAIErrorService.classify_error(status_code=exc.status_code, detail=detail_payload)
    _log_v1_request_rejected_before_route(
        request=request,
        status_code=exc.status_code,
        message=message,
        error_code=error_code,
        retryable=bool(classified["retryable"]),
        detail=detail_payload,
        request_body_json=getattr(request.state, "v1_request_body_structure_json", None),
    )
    content = OpenAIErrorService.build_error_payload(
        message=message,
        code=error_code,
        trace_id=trace_id,
        error_type=str(classified["error_type"]),
        retryable=bool(classified["retryable"]),
        recoverable=bool(classified["recoverable"]),
        category=str(classified["category"]),
        status_code=exc.status_code,
        next_action=str(error_object["next_action"]),
        detail=error_object.get("detail") if isinstance(error_object.get("detail"), dict) else detail_payload,
    )
    if detail_payload is not None:
        content["detail"] = ErrorCatalogService.normalize_detail(
            status_code=exc.status_code,
            detail=detail_payload,
            code=error_code,
            message=message,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    _record_exception_event(
        request=request,
        exc=exc,
        handler_name="unhandled_exception_handler",
        status_code=500,
        error_code="internal_server_error",
        message=str(exc),
        detail={"exception_type": exc.__class__.__name__},
        severity="critical",
    )
    if _is_external_v1_path(request.url.path):
        return _build_unhandled_v1_error_response(request, exc)
    raise exc


def _build_unhandled_v1_error_response(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled external v1 request failed: %s", exc)
    trace_id = getattr(request.state, "trace_id", None)
    message = str(exc).strip() or exc.__class__.__name__
    detail = {
        "exception_type": exc.__class__.__name__,
        "message": message,
    }
    _record_exception_event(
        request=request,
        exc=exc,
        handler_name="build_unhandled_v1_error_response",
        status_code=500,
        error_code="internal_server_error",
        message=message,
        detail=detail,
        severity="critical",
    )
    classified = OpenAIErrorService.classify_error(status_code=500, detail=detail)
    error_object = ErrorCatalogService.build_error_object(status_code=500, detail=detail, trace_id=trace_id)
    _log_v1_request_rejected_before_route(
        request=request,
        status_code=500,
        message=message,
        error_code=str(error_object["code"]),
        retryable=bool(classified["retryable"]),
        detail=detail,
        request_body_json=getattr(request.state, "v1_request_body_structure_json", None),
    )
    return JSONResponse(
        status_code=500,
        content=OpenAIErrorService.build_error_payload(
            message=str(error_object["message"]),
            code=str(error_object["code"]),
            trace_id=trace_id,
            error_type=str(classified["error_type"]),
            retryable=bool(classified["retryable"]),
            recoverable=bool(classified["recoverable"]),
            category=str(classified["category"]),
            status_code=500,
            next_action=str(error_object["next_action"]),
            detail=detail,
        ),
        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
    )


def _record_exception_event(
    *,
    request: Request,
    exc: BaseException | None,
    handler_name: str,
    status_code: int | None,
    error_code: str | None,
    message: str | None,
    detail: dict | None = None,
    severity: str = "danger",
) -> None:
    db = SessionLocal()
    try:
        ExceptionLogRecorder.record_exception(
            db,
            exc=exc,
            handler_name=handler_name,
            request_path=request.url.path,
            method=request.method.upper(),
            trace_id=getattr(request.state, "trace_id", None),
            status_code=status_code,
            error_code=error_code,
            message=message,
            source_ip=ProxySafeHelpers.extract_source_ip(request),
            is_external_v1=_is_external_v1_path(request.url.path),
            severity=severity,
            detail=detail,
        )
    except Exception as log_exc:
        logger.warning("Failed to record exception event: %s", log_exc)
    finally:
        db.close()


def _log_v1_request_rejected_before_route(
    *,
    request: Request,
    status_code: int,
    message: str,
    error_code: str,
    retryable: bool,
    detail: dict | None = None,
    request_body_json: str | None = None,
) -> None:
    if getattr(request.state, "v1_rejection_logged", False):
        return
    try:
        trace_id = getattr(request.state, "trace_id", None)
        requested_model = getattr(request.state, "v1_requested_model", None)
        requested_model = requested_model if isinstance(requested_model, str) else None
        candidate_context = get_current_provider_candidate() or {}
        provider_id = candidate_context.get("provider_id")
        provider_name = candidate_context.get("provider_name")
        resolved_provider_model_id = candidate_context.get("resolved_provider_model_id")
        candidate_model_name = candidate_context.get("model_name")
        model_name = candidate_model_name if isinstance(candidate_model_name, str) else requested_model
        log_context = ErrorCatalogService.build_log_context(
            status_code=status_code,
            detail=detail if detail is not None else {"message": message, "code": error_code},
            code=error_code,
            message=message,
            trace_id=trace_id,
        )
        request.state.v1_rejection_logged = True
        classified = OpenAIErrorService.classify_error(
            status_code=status_code,
            detail=detail if detail is not None else {"message": message, "code": error_code},
        )
        rejection_trace = [
            {
                "result": "request_rejected_before_route",
                "error": error_code,
                "latency_ms": 0,
                "requested_model": requested_model,
                "provider_id": provider_id,
                "provider_name": provider_name,
                "resolved_provider_model_id": resolved_provider_model_id,
                "model_name": model_name,
            },
            {
                "typed_event": "request_validation",
                "event_result": "failed",
                "severity": "warning",
                "module": "proxy",
                "payload": {
                    "validation_stage": "body_read" if error_code == "request_body_too_large" else "json_parse",
                    "passed": False,
                    "request_body_summary_json": request_body_json,
                    "requested_model": requested_model,
                    "last_candidate_json": dumps_json(candidate_context) if candidate_context else None,
                    "error_code": error_code,
                    "safe_detail_json": dumps_json(detail if detail is not None else {"message": message, "code": error_code}),
                },
            },
            {
                "typed_event": "request_error_response",
                "event_result": "failed",
                "severity": "warning" if status_code < 500 else "danger",
                "module": "proxy",
                "payload": {
                    "status_code": status_code,
                    "error_type": str(log_context.get("error_type") or classified["error_type"]),
                    "error_code": str(log_context.get("code") or error_code),
                    "public_message": str(log_context.get("message") or message),
                    "category": str(log_context.get("category") or classified["category"]),
                    "retryable": bool(log_context.get("retryable", retryable)),
                    "recoverable": bool(classified["recoverable"]),
                    "diagnostic_sample_json": dumps_json(detail if detail is not None else {"message": message, "code": error_code}),
                    "requested_model": requested_model,
                    "last_candidate_json": dumps_json(candidate_context) if candidate_context else None,
                },
            },
        ]
        log_kwargs = {
            "log_type": "proxy",
            "provider_id": provider_id,
            "provider_name": provider_name,
            "trace_id": trace_id,
            "model_name": model_name,
            "requested_model": requested_model,
            "resolved_provider_model_id": resolved_provider_model_id,
            "request_path": request.url.path,
            "source_ip": ProxySafeHelpers.extract_source_ip(request),
            "http_method": request.method.upper(),
            "success": False,
            "status_code": status_code,
            "request_headers_json": getattr(request.state, "v1_request_headers_json", None),
            "request_body_json": request_body_json,
            "response_body_json": ProxySafeHelpers.truncate_json(
                {
                    "detail": detail,
                    "error_context": {
                        "code": log_context.get("code"),
                        "message": log_context.get("message"),
                        "handling_strategy": log_context.get("handling_strategy"),
                        "alert_level": log_context.get("alert_level"),
                    },
                }
                if detail is not None
                else {
                    "message": message,
                    "error_context": {
                        "code": log_context.get("code"),
                        "message": log_context.get("message"),
                        "handling_strategy": log_context.get("handling_strategy"),
                        "alert_level": log_context.get("alert_level"),
                    },
                },
                16384,
            ),
            "message": str(log_context.get("message") or message),
            "error_type": str(log_context.get("error_type") or OpenAIErrorService.classify_status_code(status_code)[0]),
            "error_code": str(log_context.get("code") or error_code),
            "retryable": bool(log_context.get("retryable", retryable)),
            "api_client_auth_result": error_code,
            "trace": rejection_trace,
            "attempt_count": 0,
            "schedule_token_fill": False,
        }
        if RequestLogQueueService.enqueue(**log_kwargs):
            return
        db = SessionLocal()
        try:
            LogService.create_log(db, **log_kwargs)
        finally:
            db.close()
    except Exception:
        request.state.v1_rejection_logged = False


def _make_json_safe(value):
    if isinstance(value, dict):
        return {str(key): _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_make_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def _log_api_client_auth_failure(request: Request, exc: ApiClientAuthError) -> None:
    if not _is_external_v1_path(request.url.path):
        return
    settings = SettingService.get_cached()
    max_body_bytes = int(getattr(settings, "max_v1_request_body_bytes", 0) or 0)
    content_length_header = request.headers.get("content-length")
    try:
        content_length = int(content_length_header) if content_length_header is not None else None
    except ValueError:
        content_length = None
    parsed_body, request_body_json = await _read_v1_request_body_structure_for_log(
        request,
        max_logged_body_bytes=int(getattr(settings, "max_logged_body_bytes", 16384) or 16384),
        max_body_bytes=max_body_bytes,
        content_length=content_length,
    )
    requested_model = None
    if isinstance(parsed_body, dict):
        requested_model = parsed_body.get("model") if isinstance(parsed_body.get("model"), str) else None
    request.state.v1_requested_model = requested_model
    classified = OpenAIErrorService.classify_error(
        status_code=exc.status_code,
        detail={"message": exc.message, "code": exc.code},
    )
    trace = [
        {"result": "auth_rejected", "error": exc.code, "latency_ms": 0},
        {
            "typed_event": "request_auth",
            "event_result": "failed",
            "severity": "warning",
            "module": "proxy",
            "payload": {
                "auth_result": exc.code,
                "api_client_key_id": exc.api_client_key_id,
                "api_client_key_prefix": exc.api_client_key_prefix,
                "user_account_id": exc.user_account_id,
                "policy_snapshot_json": exc.policy_snapshot_json,
                "error_code": exc.code,
            },
        },
        {
            "typed_event": "request_validation",
            "event_result": "success" if request_body_json else "skipped",
            "severity": "info",
            "module": "proxy",
            "payload": {
                "validation_stage": "request_summary",
                "passed": True,
                "request_body_summary_json": request_body_json,
            },
        },
        {
            "typed_event": "request_error_response",
            "event_result": "failed",
            "severity": "warning",
            "module": "proxy",
            "payload": {
                "status_code": exc.status_code,
                "error_type": str(classified["error_type"]),
                "error_code": exc.code,
                "public_message": exc.message,
                "category": str(classified["category"]),
                "retryable": exc.status_code == 429,
                "recoverable": bool(classified["recoverable"]),
                "diagnostic_sample_json": dumps_json({"message": exc.message, "code": exc.code}),
            },
        },
    ]
    log_kwargs = {
        "log_type": "api_client_auth",
        "trace_id": getattr(request.state, "trace_id", None),
        "model_name": requested_model,
        "requested_model": requested_model,
        "session_id": LogService.extract_session_id(parsed_body if isinstance(parsed_body, dict) else None),
        "request_path": request.url.path,
        "source_ip": ProxySafeHelpers.extract_source_ip(request),
        "http_method": request.method.upper(),
        "is_stream": bool(isinstance(parsed_body, dict) and parsed_body.get("stream") is True),
        "has_image": ProxySafeHelpers.payload_has_image(parsed_body if isinstance(parsed_body, dict) else None),
        "success": False,
        "status_code": exc.status_code,
        "request_headers_json": getattr(request.state, "v1_request_headers_json", None),
        "reasoning_level": LogService.extract_reasoning_level(parsed_body if isinstance(parsed_body, dict) else None),
        "model_reasoning_effort": LogService.extract_model_reasoning_effort(parsed_body if isinstance(parsed_body, dict) else None),
        "request_body_json": request_body_json,
        "message": exc.message,
        "error_type": "authentication_error" if exc.status_code in {401, 403} else "rate_limit_error",
        "error_code": exc.code,
        "retryable": exc.status_code == 429,
        "api_client_key_id": exc.api_client_key_id,
        "api_client_key_name": exc.api_client_key_name,
        "api_client_key_prefix": exc.api_client_key_prefix,
        "user_account_id": exc.user_account_id,
        "user_account_name": exc.user_account_name,
        "api_client_auth_result": exc.code,
        "api_client_policy_snapshot_json": exc.policy_snapshot_json,
        "trace": trace,
        "attempt_count": 1,
        "token_request_payload": parsed_body if isinstance(parsed_body, dict) else None,
        "schedule_token_fill": False,
    }
    if RequestLogQueueService.enqueue(**log_kwargs):
        return
    db = SessionLocal()
    try:
        LogService.create_log(db, **log_kwargs)
    finally:
        db.close()


async def _read_v1_request_body_structure_for_log(
    request: Request,
    *,
    max_logged_body_bytes: int,
    max_body_bytes: int,
    content_length: int | None,
) -> tuple[dict | None, str | None]:
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None, None
    max_read_bytes = max_logged_body_bytes * 4
    if max_body_bytes > 0:
        max_read_bytes = min(max_read_bytes, max_body_bytes)
    max_read_bytes = max(1, min(max_read_bytes, 262144))
    if content_length is not None and content_length > max_read_bytes:
        return None, ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure omitted because Content-Length exceeds logging read limit",
                "structure": {
                    "type": "bytes",
                    "content_length": content_length,
                    "max_log_read_bytes": max_read_bytes,
                },
            },
            max_logged_body_bytes,
        )
    truncated = False
    try:
        body = await read_limited_request_body(
            request.stream(),
            max_bytes=max_read_bytes,
            total_timeout_seconds=max(1.0, float(settings.request_timeout_ms or 60000) / 1000.0),
            idle_timeout_seconds=float(settings.v1_request_body_idle_timeout_seconds or 15),
        )
    except RequestBodyTooLarge:
        truncated = True
        body = b""
    except RequestBodyReadTimeout as exc:
        return None, ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure unavailable because client upload timed out",
                "structure": {
                    "type": "bytes",
                    "bytes_read": exc.bytes_read,
                    "timeout_kind": exc.timeout_kind,
                    "timeout_seconds": exc.timeout_seconds,
                },
            },
            max_logged_body_bytes,
        )
    except RuntimeError:
        return None, ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure unavailable because request stream was already consumed",
                "structure": {"type": "unavailable"},
            },
            max_logged_body_bytes,
        )
    if not body and not truncated:
        return None, None
    if truncated:
        return None, ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure only; body exceeds logging read limit",
                "structure": {
                    "type": "bytes",
                    "max_log_read_bytes": max_read_bytes,
                    "truncated": True,
                },
            },
            max_logged_body_bytes,
        )
    body_text = body.decode("utf-8", errors="ignore")
    parsed_body = safeJsonParse(body_text)
    if isinstance(parsed_body, dict):
        summary = summarize_request_body_structure(parsed_body)
        if truncated:
            summary["structure"]["truncated_body_before_parse"] = True
        return parsed_body, ProxySafeHelpers.truncate_json(summary, max_logged_body_bytes)
    return None, ProxySafeHelpers.truncate_json(
        {
            "_summary": "request body structure only; body is not valid JSON or not an object",
            "structure": {
                "type": "text",
                "chars_read": len(body_text),
                "truncated": truncated,
                "parseable_json": parsed_body is not None,
            },
        },
        max_logged_body_bytes,
    )


class ProxySafeHelpers:
    @staticmethod
    def extract_source_ip(request: Request) -> str | None:
        return ApiKeyService.extract_source_ip(request)

    @staticmethod
    def truncate_json(value, limit_bytes: int) -> str:
        serialized = dumps_json(value)
        encoded = serialized.encode("utf-8", errors="ignore")
        if len(encoded) <= limit_bytes:
            return serialized
        clipped = encoded[:limit_bytes].decode("utf-8", errors="ignore")
        return f"{clipped}...[truncated]"

    @staticmethod
    def compact_request_log_payload(payload: dict, settings: AppSetting) -> dict:
        compact = {
            key: payload[key]
            for key in ("model", "stream", "user", "max_tokens", "max_completion_tokens", "max_output_tokens")
            if key in payload
        }
        if "metadata" in payload:
            compact["metadata"] = ProxySafeHelpers.compact_metadata(
                payload.get("metadata"),
                max_bytes=int(getattr(settings, "max_logged_metadata_bytes", 1024) or 0),
            )
        return compact

    @staticmethod
    def compact_metadata(value, *, max_bytes: int):
        serialized = dumps_json(value)
        encoded = serialized.encode("utf-8", errors="ignore")
        if max_bytes > 0 and len(encoded) <= max_bytes:
            return value
        summary = {
            "_summary": "metadata omitted from compact request log",
            "value_type": type(value).__name__,
            "original_bytes": len(encoded),
        }
        if isinstance(value, dict):
            keys = [str(key) for key in value.keys()]
            summary["key_count"] = len(keys)
            summary["keys"] = keys[:50]
        elif isinstance(value, list):
            summary["item_count"] = len(value)
        return summary

    @staticmethod
    def payload_has_image(payload: dict | None) -> bool:
        if not isinstance(payload, dict):
            return False
        return ProxySafeHelpers.value_has_image(payload.get("messages")) or ProxySafeHelpers.value_has_image(payload.get("input"))

    @staticmethod
    def value_has_image(value) -> bool:
        if isinstance(value, list):
            return any(ProxySafeHelpers.value_has_image(item) for item in value)
        if isinstance(value, dict):
            item_type = value.get("type")
            if isinstance(item_type, str) and item_type in {"image_url", "input_image"}:
                return True
            if isinstance(value.get("image_url"), (dict, str)):
                return True
            return any(ProxySafeHelpers.value_has_image(item) for item in value.values())
        return False

app.include_router(auth_router)
app.include_router(user_portal_router)
app.include_router(user_accounts_router)
app.include_router(health_router)
app.include_router(pages_router)
app.include_router(dashboard_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(conversations_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(api_keys_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(api_key_policy_templates_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(providers_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(provider_models_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(models_router)
app.include_router(content_guard_router)
app.include_router(ip_management_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(settings_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(logs_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(logging_api_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(metrics_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(playground_api_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(benchmark_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(proxy_router)
