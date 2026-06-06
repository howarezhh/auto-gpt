from contextlib import asynccontextmanager
import logging
from uuid import uuid4

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import inspect, text

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import AppSetting
from app.models.request_log import RequestLog
from app.routers.auth import router as auth_router
from app.routers.api_keys import router as api_keys_router
from app.routers.api_key_policy_templates import router as api_key_policy_templates_router
from app.routers.benchmark import router as benchmark_router
from app.routers.playground_api import router as playground_api_router
from app.routers.dashboard import router as dashboard_router
from app.routers.conversations import router as conversations_router
from app.routers.health import router as health_router
from app.routers.logs import router as logs_router
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
from app.services.api_key_service import ApiClientAuthError
from app.services.log_service import LogService
from app.services.error_catalog_service import ErrorCatalogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.model_mapping_service import ModelMappingService
from app.services.openai_error_service import OpenAIErrorService
from app.services.provider_service import ProviderService
from app.services.redis_service import RedisService
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


settings = get_settings()
settings.validate_runtime_settings()
logger = logging.getLogger(__name__)


def init_database(*, allow_production_ddl: bool = False) -> None:
    """初始化数据库，并补齐运行所需的迁移和基础数据。"""
    if settings.is_production() and not allow_production_ddl:
        return
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _migrate_provider_capacity_columns(db)
        _migrate_provider_metadata_columns(db)
        _migrate_app_setting_concurrency_columns(db)
        api_key_columns_changed = _migrate_api_client_key_columns(db)
        _migrate_cache_price_columns(db)
        _migrate_model_mapping_table(db)
        _migrate_responses_chat_adapter_session_table(db)
        if _is_sqlite_session(db):
            _migrate_request_log_columns(db)
        else:
            _backfill_user_shared_wallet(db)
        setting = db.get(AppSetting, 1)
        if setting is None:
            setting = AppSetting(id=1)
            db.add(setting)
            db.commit()
            db.refresh(setting)
        if api_key_columns_changed:
            ApiKeyAdminService.backfill_all_api_keys_to_all_providers(db)
        else:
            ApiKeyAdminService.sync_auto_provider_bindings(db)
        ProviderService.sync_legacy_provider_models(db)
        ResponsesChatAdapterService.sync_env_upstreams(db)
        ModelCatalogService.sync_model_catalogs(db)
    finally:
        db.close()


def _is_sqlite_session(db) -> bool:
    """判断当前数据库会话是否连接到 SQLite。"""
    return db.get_bind().dialect.name == "sqlite"


def _get_table_columns(db, table_name: str) -> set[str]:
    """读取指定表的现有列名集合。"""
    inspector = inspect(db.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _migrate_provider_capacity_columns(db) -> None:
    """为 providers 表补充容量控制相关字段。"""
    existing_columns = _get_table_columns(db, "providers")
    additions = {
        "max_active_requests": "ALTER TABLE providers ADD COLUMN max_active_requests INTEGER DEFAULT 20",
        "max_active_streams": "ALTER TABLE providers ADD COLUMN max_active_streams INTEGER DEFAULT 10",
        "max_qps": "ALTER TABLE providers ADD COLUMN max_qps INTEGER DEFAULT 20",
        "max_rpm": "ALTER TABLE providers ADD COLUMN max_rpm INTEGER DEFAULT 20",
        "max_error_rate": "ALTER TABLE providers ADD COLUMN max_error_rate FLOAT DEFAULT 80",
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


def _migrate_provider_metadata_columns(db) -> None:
    """为 providers 表补充路由、协议与运维治理字段。"""
    existing_columns = _get_table_columns(db, "providers")
    if not existing_columns:
        return
    dialect_name = db.get_bind().dialect.name
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    datetime_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
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
    additions = {
        "global_max_request_tokens": "ALTER TABLE app_settings ADD COLUMN global_max_request_tokens INTEGER DEFAULT 0",
        "route_exhausted_retry_max_wait_seconds": "ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_max_wait_seconds INTEGER DEFAULT 600",
        "route_exhausted_retry_infinite_enabled": f"ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_infinite_enabled BOOLEAN DEFAULT {false_default}",
        "max_candidate_count": "ALTER TABLE app_settings ADD COLUMN max_candidate_count INTEGER DEFAULT 10",
        "max_v1_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_request_body_bytes INTEGER DEFAULT 20971520",
        "max_v1_chat_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_chat_request_body_bytes INTEGER DEFAULT 0",
        "max_v1_responses_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_responses_request_body_bytes INTEGER DEFAULT 0",
        "long_output_stream_threshold_tokens": "ALTER TABLE app_settings ADD COLUMN long_output_stream_threshold_tokens INTEGER DEFAULT 8192",
        "max_non_stream_response_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_non_stream_response_body_bytes INTEGER DEFAULT 20971520",
        "stream_token_capture_max_bytes": "ALTER TABLE app_settings ADD COLUMN stream_token_capture_max_bytes INTEGER DEFAULT 1048576",
        "max_logged_metadata_bytes": "ALTER TABLE app_settings ADD COLUMN max_logged_metadata_bytes INTEGER DEFAULT 1024",
        "global_max_active_requests": f"ALTER TABLE app_settings ADD COLUMN global_max_active_requests INTEGER DEFAULT {runtime_settings.global_max_active_requests}",
        "global_max_active_streams": f"ALTER TABLE app_settings ADD COLUMN global_max_active_streams INTEGER DEFAULT {runtime_settings.global_max_active_streams}",
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
        "responses_chat_adapter_storage_type": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_storage_type TEXT DEFAULT 'memory'",
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


def _migrate_cache_price_columns(db) -> None:
    """为模型和日志表补充缓存计费与能力字段。"""
    dialect_name = db.get_bind().dialect.name
    price_type = f"NUMERIC({DB_PRICE_PRECISION}, {DB_PRICE_SCALE})"
    true_default = "TRUE" if dialect_name == "postgresql" else "1"
    false_default = "FALSE" if dialect_name == "postgresql" else "0"
    additions_by_table = {
        "provider_models": {
            "cache_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN cache_price_per_1k {price_type}",
            "supports_tools": f"ALTER TABLE provider_models ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT {false_default}",
            "supports_chat_completions": f"ALTER TABLE provider_models ADD COLUMN supports_chat_completions BOOLEAN NOT NULL DEFAULT {true_default}",
            "supports_responses": f"ALTER TABLE provider_models ADD COLUMN supports_responses BOOLEAN NOT NULL DEFAULT {true_default}",
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
        "route_exhausted_retry_infinite_enabled": f"ALTER TABLE api_client_keys ADD COLUMN route_exhausted_retry_infinite_enabled BOOLEAN NOT NULL DEFAULT {false_default}",
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


def _migrate_request_log_columns(db) -> None:
    """为 SQLite 下的请求日志补充金额与倍率字段。"""
    money_type = f"NUMERIC({DB_MONEY_PRECISION}, {DB_MONEY_SCALE})"
    price_type = f"NUMERIC({DB_PRICE_PRECISION}, {DB_PRICE_SCALE})"
    multiplier_type = f"NUMERIC({DB_MULTIPLIER_PRECISION}, {DB_MULTIPLIER_SCALE})"
    existing = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(request_logs)")).fetchall()
    }
    additions = {
        "trace_id": "ALTER TABLE request_logs ADD COLUMN trace_id TEXT",
        "requested_model": "ALTER TABLE request_logs ADD COLUMN requested_model TEXT",
        "tenant_name": "ALTER TABLE request_logs ADD COLUMN tenant_name TEXT",
        "project_name": "ALTER TABLE request_logs ADD COLUMN project_name TEXT",
        "app_name": "ALTER TABLE request_logs ADD COLUMN app_name TEXT",
        "environment_name": "ALTER TABLE request_logs ADD COLUMN environment_name TEXT",
        "resolved_provider_model_id": "ALTER TABLE request_logs ADD COLUMN resolved_provider_model_id INTEGER",
        "is_stream": "ALTER TABLE request_logs ADD COLUMN is_stream BOOLEAN NOT NULL DEFAULT 0",
        "has_image": "ALTER TABLE request_logs ADD COLUMN has_image BOOLEAN NOT NULL DEFAULT 0",
        "request_id": "ALTER TABLE request_logs ADD COLUMN request_id TEXT",
        "conversation_key": "ALTER TABLE request_logs ADD COLUMN conversation_key TEXT",
        "session_id": "ALTER TABLE request_logs ADD COLUMN session_id TEXT",
        "source_ip": "ALTER TABLE request_logs ADD COLUMN source_ip TEXT",
        "http_method": "ALTER TABLE request_logs ADD COLUMN http_method TEXT",
        "first_token_latency_ms": "ALTER TABLE request_logs ADD COLUMN first_token_latency_ms INTEGER",
        "ttfb_ms": "ALTER TABLE request_logs ADD COLUMN ttfb_ms INTEGER",
        "duration_ms": "ALTER TABLE request_logs ADD COLUMN duration_ms INTEGER",
        "tps": "ALTER TABLE request_logs ADD COLUMN tps FLOAT",
        "reasoning_level": "ALTER TABLE request_logs ADD COLUMN reasoning_level TEXT",
        "attempt_count": "ALTER TABLE request_logs ADD COLUMN attempt_count INTEGER",
        "prompt_cost": f"ALTER TABLE request_logs ADD COLUMN prompt_cost {money_type}",
        "completion_cost": f"ALTER TABLE request_logs ADD COLUMN completion_cost {money_type}",
        "total_cost": f"ALTER TABLE request_logs ADD COLUMN total_cost {money_type}",
        "billing_status": "ALTER TABLE request_logs ADD COLUMN billing_status TEXT",
        "billing_finalized_at": "ALTER TABLE request_logs ADD COLUMN billing_finalized_at DATETIME",
        "billing_event_id": "ALTER TABLE request_logs ADD COLUMN billing_event_id TEXT",
        "billing_attempt_count": "ALTER TABLE request_logs ADD COLUMN billing_attempt_count INTEGER NOT NULL DEFAULT 0",
        "billing_error": "ALTER TABLE request_logs ADD COLUMN billing_error TEXT",
        "pricing_tier_key": "ALTER TABLE request_logs ADD COLUMN pricing_tier_key TEXT",
        "pricing_tier_name": "ALTER TABLE request_logs ADD COLUMN pricing_tier_name TEXT",
        "token_finalize_attempt_count": "ALTER TABLE request_logs ADD COLUMN token_finalize_attempt_count INTEGER NOT NULL DEFAULT 0",
        "token_finalize_error": "ALTER TABLE request_logs ADD COLUMN token_finalize_error TEXT",
        "billing_multiplier": f"ALTER TABLE request_logs ADD COLUMN billing_multiplier {multiplier_type}",
        "channel_price_input_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_input_per_1k {price_type}",
        "channel_price_output_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_output_per_1k {price_type}",
        "channel_price_cache_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_cache_per_1k {price_type}",
        "channel_price_cache_write_per_1k": f"ALTER TABLE request_logs ADD COLUMN channel_price_cache_write_per_1k {price_type}",
        "api_client_balance_after": f"ALTER TABLE request_logs ADD COLUMN api_client_balance_after {money_type}",
        "prompt_tokens": "ALTER TABLE request_logs ADD COLUMN prompt_tokens INTEGER",
        "completion_tokens": "ALTER TABLE request_logs ADD COLUMN completion_tokens INTEGER",
        "total_tokens": "ALTER TABLE request_logs ADD COLUMN total_tokens INTEGER",
        "cache_read_tokens": "ALTER TABLE request_logs ADD COLUMN cache_read_tokens INTEGER",
        "cache_write_tokens": "ALTER TABLE request_logs ADD COLUMN cache_write_tokens INTEGER",
        "finish_reason": "ALTER TABLE request_logs ADD COLUMN finish_reason TEXT",
        "upstream_request_id": "ALTER TABLE request_logs ADD COLUMN upstream_request_id TEXT",
        "request_body_json": "ALTER TABLE request_logs ADD COLUMN request_body_json TEXT",
        "response_body_json": "ALTER TABLE request_logs ADD COLUMN response_body_json TEXT",
        "response_text": "ALTER TABLE request_logs ADD COLUMN response_text TEXT",
        "error_type": "ALTER TABLE request_logs ADD COLUMN error_type TEXT",
        "error_code": "ALTER TABLE request_logs ADD COLUMN error_code TEXT",
        "retryable": "ALTER TABLE request_logs ADD COLUMN retryable BOOLEAN",
        "api_client_key_id": "ALTER TABLE request_logs ADD COLUMN api_client_key_id INTEGER",
        "api_client_key_name": "ALTER TABLE request_logs ADD COLUMN api_client_key_name TEXT",
        "api_client_key_prefix": "ALTER TABLE request_logs ADD COLUMN api_client_key_prefix TEXT",
        "user_account_id": "ALTER TABLE request_logs ADD COLUMN user_account_id INTEGER",
        "user_account_name": "ALTER TABLE request_logs ADD COLUMN user_account_name TEXT",
        "api_client_auth_result": "ALTER TABLE request_logs ADD COLUMN api_client_auth_result TEXT",
        "api_client_remaining_tokens": "ALTER TABLE request_logs ADD COLUMN api_client_remaining_tokens INTEGER",
        "api_client_remaining_requests_daily": "ALTER TABLE request_logs ADD COLUMN api_client_remaining_requests_daily INTEGER",
        "api_client_remaining_cost_daily": f"ALTER TABLE request_logs ADD COLUMN api_client_remaining_cost_daily {money_type}",
        "api_client_policy_snapshot_json": "ALTER TABLE request_logs ADD COLUMN api_client_policy_snapshot_json TEXT",
    }
    changed = False
    for column, ddl in additions.items():
        if column in existing:
            continue
        db.execute(text(ddl))
        changed = True
    if changed:
        db.commit()

    existing_provider_model_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(provider_models)")).fetchall()
    }
    provider_model_additions = {
        "circuit_state": "ALTER TABLE provider_models ADD COLUMN circuit_state TEXT NOT NULL DEFAULT 'closed'",
        "circuit_opened_at": "ALTER TABLE provider_models ADD COLUMN circuit_opened_at DATETIME",
        "price_multiplier": f"ALTER TABLE provider_models ADD COLUMN price_multiplier {multiplier_type} NOT NULL DEFAULT 1.0",
        "input_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN input_price_per_1k {price_type}",
        "output_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN output_price_per_1k {price_type}",
        "cache_price_per_1k": f"ALTER TABLE provider_models ADD COLUMN cache_price_per_1k {price_type}",
        "supports_tools": "ALTER TABLE provider_models ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT 0",
        "supports_chat_completions": "ALTER TABLE provider_models ADD COLUMN supports_chat_completions BOOLEAN NOT NULL DEFAULT 1",
        "supports_responses": "ALTER TABLE provider_models ADD COLUMN supports_responses BOOLEAN NOT NULL DEFAULT 1",
        "context_window_tokens": "ALTER TABLE provider_models ADD COLUMN context_window_tokens INTEGER",
        "max_input_tokens": "ALTER TABLE provider_models ADD COLUMN max_input_tokens INTEGER",
        "max_output_tokens": "ALTER TABLE provider_models ADD COLUMN max_output_tokens INTEGER",
    }
    changed_provider_models = False
    for column, ddl in provider_model_additions.items():
        if column in existing_provider_model_columns:
            continue
        db.execute(text(ddl))
        changed_provider_models = True
    if changed_provider_models:
        db.commit()

    existing_model_catalog_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(model_catalogs)")).fetchall()
    }
    model_catalog_additions = {
        "cache_price_per_1k": f"ALTER TABLE model_catalogs ADD COLUMN cache_price_per_1k {price_type}",
        "context_window_tokens": "ALTER TABLE model_catalogs ADD COLUMN context_window_tokens INTEGER",
        "supports_tools": "ALTER TABLE model_catalogs ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT 0",
        "supports_chat_completions": "ALTER TABLE model_catalogs ADD COLUMN supports_chat_completions BOOLEAN NOT NULL DEFAULT 1",
        "supports_responses": "ALTER TABLE model_catalogs ADD COLUMN supports_responses BOOLEAN NOT NULL DEFAULT 1",
        "max_input_tokens": "ALTER TABLE model_catalogs ADD COLUMN max_input_tokens INTEGER",
        "max_output_tokens": "ALTER TABLE model_catalogs ADD COLUMN max_output_tokens INTEGER",
        "pricing_mode": "ALTER TABLE model_catalogs ADD COLUMN pricing_mode TEXT NOT NULL DEFAULT 'fixed'",
        "pricing_json": "ALTER TABLE model_catalogs ADD COLUMN pricing_json TEXT",
    }
    changed_model_catalogs = False
    for column, ddl in model_catalog_additions.items():
        if column in existing_model_catalog_columns:
            continue
        db.execute(text(ddl))
        changed_model_catalogs = True
    if changed_model_catalogs:
        db.commit()

    existing_api_key_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(api_client_keys)")).fetchall()
    }
    api_key_additions = {
        "tenant_name": "ALTER TABLE api_client_keys ADD COLUMN tenant_name TEXT",
        "project_name": "ALTER TABLE api_client_keys ADD COLUMN project_name TEXT",
        "app_name": "ALTER TABLE api_client_keys ADD COLUMN app_name TEXT",
        "environment_name": "ALTER TABLE api_client_keys ADD COLUMN environment_name TEXT",
        "request_limit_daily": "ALTER TABLE api_client_keys ADD COLUMN request_limit_daily INTEGER",
        "token_limit_daily": "ALTER TABLE api_client_keys ADD COLUMN token_limit_daily INTEGER",
        "cost_limit_daily": f"ALTER TABLE api_client_keys ADD COLUMN cost_limit_daily {money_type}",
        "qps_limit": "ALTER TABLE api_client_keys ADD COLUMN qps_limit INTEGER DEFAULT 20",
        "rpm_limit": "ALTER TABLE api_client_keys ADD COLUMN rpm_limit INTEGER DEFAULT 20",
        "tpm_limit": "ALTER TABLE api_client_keys ADD COLUMN tpm_limit INTEGER",
        "cost_limit_total": f"ALTER TABLE api_client_keys ADD COLUMN cost_limit_total {money_type}",
        "total_cost_used": f"ALTER TABLE api_client_keys ADD COLUMN total_cost_used {money_type} NOT NULL DEFAULT 0",
        "balance_amount": f"ALTER TABLE api_client_keys ADD COLUMN balance_amount {money_type}",
        "total_recharge_amount": f"ALTER TABLE api_client_keys ADD COLUMN total_recharge_amount {money_type} NOT NULL DEFAULT 0",
        "owner_user_id": "ALTER TABLE api_client_keys ADD COLUMN owner_user_id INTEGER",
        "raw_key_encrypted": "ALTER TABLE api_client_keys ADD COLUMN raw_key_encrypted TEXT",
        "allowed_model_names_json": "ALTER TABLE api_client_keys ADD COLUMN allowed_model_names_json TEXT NOT NULL DEFAULT '[]'",
        "allowed_endpoint_paths_json": "ALTER TABLE api_client_keys ADD COLUMN allowed_endpoint_paths_json TEXT NOT NULL DEFAULT '[]'",
        "allowed_source_ips_json": "ALTER TABLE api_client_keys ADD COLUMN allowed_source_ips_json TEXT NOT NULL DEFAULT '[]'",
        "preferred_provider_ids_json": "ALTER TABLE api_client_keys ADD COLUMN preferred_provider_ids_json TEXT NOT NULL DEFAULT '[]'",
        "preferred_region_tags_json": "ALTER TABLE api_client_keys ADD COLUMN preferred_region_tags_json TEXT NOT NULL DEFAULT '[]'",
        "max_candidate_count": "ALTER TABLE api_client_keys ADD COLUMN max_candidate_count INTEGER",
        "latency_bias": "ALTER TABLE api_client_keys ADD COLUMN latency_bias INTEGER NOT NULL DEFAULT 1",
        "success_rate_bias": "ALTER TABLE api_client_keys ADD COLUMN success_rate_bias INTEGER NOT NULL DEFAULT 1",
        "cost_bias": "ALTER TABLE api_client_keys ADD COLUMN cost_bias INTEGER NOT NULL DEFAULT 0",
        "route_exhausted_retry_infinite_enabled": "ALTER TABLE api_client_keys ADD COLUMN route_exhausted_retry_infinite_enabled BOOLEAN NOT NULL DEFAULT 0",
    }
    changed_api_keys = False
    for column, ddl in api_key_additions.items():
        if column in existing_api_key_columns:
            continue
        db.execute(text(ddl))
        changed_api_keys = True
    if changed_api_keys:
        db.commit()

    existing_policy_template_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(api_key_policy_templates)")).fetchall()
    }
    policy_template_additions = {
        "allowed_model_names_json": "ALTER TABLE api_key_policy_templates ADD COLUMN allowed_model_names_json TEXT NOT NULL DEFAULT '[]'",
    }
    changed_policy_templates = False
    for column, ddl in policy_template_additions.items():
        if not existing_policy_template_columns or column in existing_policy_template_columns:
            continue
        db.execute(text(ddl))
        changed_policy_templates = True
    if changed_policy_templates:
        db.commit()

    existing_provider_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(providers)")).fetchall()
    }
    provider_additions = {
        "group_name": "ALTER TABLE providers ADD COLUMN group_name TEXT",
        "region_tag": "ALTER TABLE providers ADD COLUMN region_tag TEXT",
        "protocol_type": "ALTER TABLE providers ADD COLUMN protocol_type TEXT NOT NULL DEFAULT 'both'",
        "maintenance_window": "ALTER TABLE providers ADD COLUMN maintenance_window TEXT",
        "maintenance_mode_enabled": "ALTER TABLE providers ADD COLUMN maintenance_mode_enabled BOOLEAN NOT NULL DEFAULT 0",
        "auto_circuit_break_enabled": "ALTER TABLE providers ADD COLUMN auto_circuit_break_enabled BOOLEAN NOT NULL DEFAULT 1",
        "auto_recover_enabled": "ALTER TABLE providers ADD COLUMN auto_recover_enabled BOOLEAN NOT NULL DEFAULT 1",
        "circuit_breaker_threshold_override": "ALTER TABLE providers ADD COLUMN circuit_breaker_threshold_override INTEGER",
        "recovery_probe_interval_sec_override": "ALTER TABLE providers ADD COLUMN recovery_probe_interval_sec_override INTEGER",
        "credential_rotated_at": "ALTER TABLE providers ADD COLUMN credential_rotated_at DATETIME",
        "credential_hint": "ALTER TABLE providers ADD COLUMN credential_hint TEXT",
    }
    changed_providers = False
    for column, ddl in provider_additions.items():
        if column in existing_provider_columns:
            continue
        db.execute(text(ddl))
        changed_providers = True
    if changed_providers:
        db.commit()
    if not existing_provider_columns or "protocol_type" in existing_provider_columns or changed_providers:
        db.execute(
            text(
                "UPDATE providers SET protocol_type = 'both' "
                "WHERE protocol_type IS NULL OR protocol_type NOT IN ('both', 'chat_completions', 'responses')"
            )
        )
        db.commit()

    existing_settings_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(app_settings)")).fetchall()
    }
    app_setting_additions = {
        "enable_token_logging": "ALTER TABLE app_settings ADD COLUMN enable_token_logging BOOLEAN NOT NULL DEFAULT 1",
        "enable_payload_logging": "ALTER TABLE app_settings ADD COLUMN enable_payload_logging BOOLEAN NOT NULL DEFAULT 0",
        "enable_stream_response_persist": "ALTER TABLE app_settings ADD COLUMN enable_stream_response_persist BOOLEAN NOT NULL DEFAULT 0",
        "mask_sensitive_fields": "ALTER TABLE app_settings ADD COLUMN mask_sensitive_fields BOOLEAN NOT NULL DEFAULT 1",
        "max_logged_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_logged_body_bytes INTEGER NOT NULL DEFAULT 16384",
        "global_max_request_tokens": "ALTER TABLE app_settings ADD COLUMN global_max_request_tokens INTEGER NOT NULL DEFAULT 0",
        "route_exhausted_retry_max_wait_seconds": "ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_max_wait_seconds INTEGER NOT NULL DEFAULT 600",
        "route_exhausted_retry_infinite_enabled": "ALTER TABLE app_settings ADD COLUMN route_exhausted_retry_infinite_enabled BOOLEAN NOT NULL DEFAULT 0",
        "max_candidate_count": "ALTER TABLE app_settings ADD COLUMN max_candidate_count INTEGER NOT NULL DEFAULT 10",
        "max_v1_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_request_body_bytes INTEGER NOT NULL DEFAULT 20971520",
        "max_v1_chat_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_chat_request_body_bytes INTEGER NOT NULL DEFAULT 0",
        "max_v1_responses_request_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_v1_responses_request_body_bytes INTEGER NOT NULL DEFAULT 0",
        "long_output_stream_threshold_tokens": "ALTER TABLE app_settings ADD COLUMN long_output_stream_threshold_tokens INTEGER NOT NULL DEFAULT 8192",
        "max_non_stream_response_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_non_stream_response_body_bytes INTEGER NOT NULL DEFAULT 20971520",
        "stream_token_capture_max_bytes": "ALTER TABLE app_settings ADD COLUMN stream_token_capture_max_bytes INTEGER NOT NULL DEFAULT 1048576",
        "max_logged_metadata_bytes": "ALTER TABLE app_settings ADD COLUMN max_logged_metadata_bytes INTEGER NOT NULL DEFAULT 1024",
        "allow_public_user_registration": "ALTER TABLE app_settings ADD COLUMN allow_public_user_registration BOOLEAN NOT NULL DEFAULT 0",
        "request_log_retention_days": "ALTER TABLE app_settings ADD COLUMN request_log_retention_days INTEGER NOT NULL DEFAULT 90",
        "admin_audit_log_retention_days": "ALTER TABLE app_settings ADD COLUMN admin_audit_log_retention_days INTEGER NOT NULL DEFAULT 180",
        "route_candidate_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN route_candidate_cache_ttl_sec INTEGER NOT NULL DEFAULT 10",
        "model_list_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN model_list_cache_ttl_sec INTEGER NOT NULL DEFAULT 15",
        "provider_status_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN provider_status_cache_ttl_sec INTEGER NOT NULL DEFAULT 10",
        "async_request_logging": "ALTER TABLE app_settings ADD COLUMN async_request_logging BOOLEAN NOT NULL DEFAULT 1",
        "stream_connect_timeout_seconds": "ALTER TABLE app_settings ADD COLUMN stream_connect_timeout_seconds INTEGER NOT NULL DEFAULT 10",
        "stream_first_token_timeout_seconds": "ALTER TABLE app_settings ADD COLUMN stream_first_token_timeout_seconds INTEGER NOT NULL DEFAULT 60",
        "stream_idle_timeout_seconds": "ALTER TABLE app_settings ADD COLUMN stream_idle_timeout_seconds INTEGER NOT NULL DEFAULT 120",
        "stream_max_duration_seconds": "ALTER TABLE app_settings ADD COLUMN stream_max_duration_seconds INTEGER NOT NULL DEFAULT 600",
        "responses_chat_adapter_enabled": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_enabled BOOLEAN NOT NULL DEFAULT 0",
        "responses_chat_adapter_storage_type": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_storage_type TEXT NOT NULL DEFAULT 'memory'",
        "responses_chat_adapter_ttl_seconds": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_ttl_seconds INTEGER NOT NULL DEFAULT 86400",
        "responses_chat_adapter_model_map_json": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_model_map_json TEXT NOT NULL DEFAULT ''",
        "responses_chat_adapter_max_tool_rounds": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_max_tool_rounds INTEGER NOT NULL DEFAULT 10",
        "responses_chat_adapter_web_search_enabled": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_web_search_enabled BOOLEAN NOT NULL DEFAULT 0",
        "responses_chat_adapter_search_proxy_url": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_search_proxy_url TEXT NOT NULL DEFAULT ''",
        "responses_chat_adapter_upstream_base_url": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstream_base_url TEXT NOT NULL DEFAULT ''",
        "responses_chat_adapter_upstream_api_key": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstream_api_key TEXT NOT NULL DEFAULT ''",
        "responses_chat_adapter_upstreams_json": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_upstreams_json TEXT NOT NULL DEFAULT ''",
        "responses_chat_adapter_context_window_tokens": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_context_window_tokens INTEGER NOT NULL DEFAULT 128000",
        "responses_chat_adapter_snapshot_max_bytes": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_snapshot_max_bytes INTEGER NOT NULL DEFAULT 1048576",
        "responses_chat_adapter_db_cleanup_interval_seconds": "ALTER TABLE app_settings ADD COLUMN responses_chat_adapter_db_cleanup_interval_seconds INTEGER NOT NULL DEFAULT 21600",
    }
    changed_settings = False
    for column, ddl in app_setting_additions.items():
        if column in existing_settings_columns:
            continue
        db.execute(text(ddl))
        changed_settings = True
    if changed_settings:
        db.commit()

    request_log_indexes = {
        "ix_request_logs_trace_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_trace_id ON request_logs (trace_id)",
        "ix_request_logs_request_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_request_id ON request_logs (request_id)",
        "ix_request_logs_conversation_key": "CREATE INDEX IF NOT EXISTS ix_request_logs_conversation_key ON request_logs (conversation_key)",
        "ix_request_logs_session_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_session_id ON request_logs (session_id)",
        "ix_request_logs_api_client_key_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_api_client_key_id ON request_logs (api_client_key_id)",
        "ix_request_logs_user_account_created_at": "CREATE INDEX IF NOT EXISTS ix_request_logs_user_account_created_at ON request_logs (user_account_id, created_at)",
        "ix_request_logs_created_at": "CREATE INDEX IF NOT EXISTS ix_request_logs_created_at ON request_logs (created_at)",
        "ix_request_logs_route_metrics": "CREATE INDEX IF NOT EXISTS ix_request_logs_route_metrics ON request_logs (log_type, created_at, provider_id, requested_model, success)",
        "ix_request_logs_api_key_created_at": "CREATE INDEX IF NOT EXISTS ix_request_logs_api_key_created_at ON request_logs (api_client_key_id, created_at)",
    }
    existing_indexes = {
        row[1]
        for row in db.execute(text("PRAGMA index_list(request_logs)")).fetchall()
    }
    changed_indexes = False
    for index_name, ddl in request_log_indexes.items():
        if index_name in existing_indexes:
            continue
        db.execute(text(ddl))
        changed_indexes = True
    if changed_indexes:
        db.commit()

    existing_user_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(user_accounts)")).fetchall()
    }
    user_additions = {
        "last_login_at": "ALTER TABLE user_accounts ADD COLUMN last_login_at DATETIME",
        "balance_amount": "ALTER TABLE user_accounts ADD COLUMN balance_amount NUMERIC NOT NULL DEFAULT 0",
        "frozen_amount": "ALTER TABLE user_accounts ADD COLUMN frozen_amount NUMERIC NOT NULL DEFAULT 0",
        "total_recharge_amount": "ALTER TABLE user_accounts ADD COLUMN total_recharge_amount NUMERIC NOT NULL DEFAULT 0",
        "request_limit_total": "ALTER TABLE user_accounts ADD COLUMN request_limit_total INTEGER",
        "request_limit_daily": "ALTER TABLE user_accounts ADD COLUMN request_limit_daily INTEGER",
        "request_limit_monthly": "ALTER TABLE user_accounts ADD COLUMN request_limit_monthly INTEGER",
        "token_limit_total": "ALTER TABLE user_accounts ADD COLUMN token_limit_total INTEGER",
        "token_limit_daily": "ALTER TABLE user_accounts ADD COLUMN token_limit_daily INTEGER",
        "token_limit_monthly": "ALTER TABLE user_accounts ADD COLUMN token_limit_monthly INTEGER",
        "cost_limit_total": "ALTER TABLE user_accounts ADD COLUMN cost_limit_total NUMERIC",
        "cost_limit_daily": "ALTER TABLE user_accounts ADD COLUMN cost_limit_daily NUMERIC",
        "cost_limit_monthly": "ALTER TABLE user_accounts ADD COLUMN cost_limit_monthly NUMERIC",
    }
    changed_users = False
    for column, ddl in user_additions.items():
        if not existing_user_columns or column in existing_user_columns:
            continue
        db.execute(text(ddl))
        changed_users = True
    if changed_users:
        db.commit()
    _backfill_user_shared_wallet(db)


def _backfill_user_shared_wallet(db) -> None:
    user_columns = _get_table_columns(db, "user_accounts")
    if "balance_amount" not in user_columns or "total_recharge_amount" not in user_columns:
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = max(40, int(settings.worker_threadpool_tokens or 40))
    if settings.enable_startup_db_init:
        init_database(allow_production_ddl=False)
    await RedisService.init()
    UpstreamClientService.get_client()
    if settings.enable_background_workers:
        await RequestLogQueueService.start_background_workers()
        await TokenUsageService.start_background_workers()
    if settings.enable_scheduler and not scheduler.running:
        configure_scheduler()
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await RequestLogQueueService.stop_background_workers()
    if settings.enable_background_workers:
        await TokenUsageService.stop_background_workers()
    await UpstreamClientService.aclose()
    await ApiKeyAuthCache.aclose()
    await RedisService.aclose()


app = FastAPI(
    title="aotu-gpt",
    lifespan=lifespan,
    docs_url="/api-docs",
    redoc_url="/api-redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploaded-assets", StaticFiles(directory=settings.uploads_dir), name="uploaded-assets")


@app.middleware("http")
async def trace_and_runtime_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or request.headers.get("x-request-id") or uuid4().hex
    request.state.trace_id = trace_id
    RuntimeStateService.enter_request()
    try:
        body_limit_response = _reject_oversized_v1_request_by_content_length(request)
        if body_limit_response is not None:
            _apply_v1_cors_headers(request, body_limit_response)
            return body_limit_response
        response = await call_next(request)
    except Exception as exc:
        if _is_external_v1_path(request.url.path):
            response = _build_unhandled_v1_error_response(request, exc)
            _apply_v1_cors_headers(request, response)
            return response
        raise
    finally:
        RuntimeStateService.leave_request()
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Request-Id"] = trace_id
    response.headers["X-Active-Requests"] = str(RuntimeStateService.current_active_requests())
    _apply_v1_cors_headers(request, response)
    return response


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
    if not (_is_external_v1_path(request.url.path) or request.url.path.startswith("/api/")):
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
    if not (_is_external_v1_path(request.url.path) or request.url.path.startswith("/api/")):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    trace_id = getattr(request.state, "trace_id", None)
    detail_payload = exc.detail if isinstance(exc.detail, dict) else None
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
    db = SessionLocal()
    try:
        trace_id = getattr(request.state, "trace_id", None)
        log_context = ErrorCatalogService.build_log_context(
            status_code=status_code,
            detail=detail if detail is not None else {"message": message, "code": error_code},
            code=error_code,
            message=message,
            trace_id=trace_id,
        )
        if trace_id:
            existing = db.query(RequestLog.id).filter(RequestLog.trace_id == trace_id).first()
            if existing is not None:
                request.state.v1_rejection_logged = True
                return
        request.state.v1_rejection_logged = True
        LogService.create_log(
            db,
            log_type="api_client_auth",
            trace_id=trace_id,
            request_path=request.url.path,
            source_ip=ProxySafeHelpers.extract_source_ip(request),
            http_method=request.method.upper(),
            success=False,
            status_code=status_code,
            request_body_json=request_body_json,
            response_body_json=ProxySafeHelpers.truncate_json(
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
            message=str(log_context.get("message") or message),
            error_type=str(log_context.get("error_type") or OpenAIErrorService.classify_status_code(status_code)[0]),
            error_code=str(log_context.get("code") or error_code),
            retryable=bool(log_context.get("retryable", retryable)),
            api_client_auth_result=error_code,
            trace=[{"result": "request_rejected_before_route", "error": error_code, "latency_ms": 0}],
            attempt_count=0,
            schedule_token_fill=False,
        )
    except Exception:
        request.state.v1_rejection_logged = False
    finally:
        db.close()


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
    db = SessionLocal()
    try:
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
        LogService.create_log(
            db,
            log_type="api_client_auth",
            trace_id=getattr(request.state, "trace_id", None),
            model_name=requested_model,
            requested_model=requested_model,
            session_id=LogService.extract_session_id(parsed_body if isinstance(parsed_body, dict) else None),
            request_path=request.url.path,
            source_ip=ProxySafeHelpers.extract_source_ip(request),
            http_method=request.method.upper(),
            is_stream=bool(isinstance(parsed_body, dict) and parsed_body.get("stream") is True),
            has_image=ProxySafeHelpers.payload_has_image(parsed_body if isinstance(parsed_body, dict) else None),
            success=False,
            status_code=exc.status_code,
            reasoning_level=LogService.extract_reasoning_level(parsed_body if isinstance(parsed_body, dict) else None),
            model_reasoning_effort=LogService.extract_model_reasoning_effort(parsed_body if isinstance(parsed_body, dict) else None),
            request_body_json=request_body_json,
            message=exc.message,
            error_type="authentication_error" if exc.status_code in {401, 403} else "rate_limit_error",
            error_code=exc.code,
            retryable=exc.status_code == 429,
            api_client_key_id=exc.api_client_key_id,
            api_client_key_name=exc.api_client_key_name,
            api_client_key_prefix=exc.api_client_key_prefix,
            user_account_id=exc.user_account_id,
            user_account_name=exc.user_account_name,
            api_client_auth_result=exc.code,
            api_client_remaining_tokens=exc.remaining_tokens,
            api_client_remaining_requests_daily=exc.remaining_requests_daily,
            api_client_remaining_cost_daily=exc.remaining_cost_daily,
            api_client_policy_snapshot_json=exc.policy_snapshot_json,
            trace=[{"result": "auth_rejected", "error": exc.code, "latency_ms": 0}],
            attempt_count=1,
            token_request_payload=parsed_body if isinstance(parsed_body, dict) else None,
            schedule_token_fill=False,
        )
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
    body = bytearray()
    truncated = False
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if len(body) + len(chunk) > max_read_bytes:
                remaining = max_read_bytes - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
                truncated = True
                break
            body.extend(chunk)
    except RuntimeError:
        return None, ProxySafeHelpers.truncate_json(
            {
                "_summary": "request body structure unavailable because request stream was already consumed",
                "structure": {"type": "unavailable"},
            },
            max_logged_body_bytes,
        )
    if not body:
        return None, None
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
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            candidate = forwarded_for.split(",")[0].strip()
            if candidate:
                return candidate
        if request.client is None:
            return None
        return request.client.host

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
app.include_router(settings_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(logs_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(metrics_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(playground_api_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(benchmark_router, dependencies=[Depends(require_admin_api_user)])
app.include_router(proxy_router)
