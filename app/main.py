from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import AppSetting
from app.routers.auth import router as auth_router
from app.routers.api_keys import router as api_keys_router
from app.routers.api_key_policy_templates import router as api_key_policy_templates_router
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
from app.services.api_key_service import ApiClientAuthError
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.openai_error_service import OpenAIErrorService
from app.services.provider_service import ProviderService
from app.services.runtime_state_service import RuntimeStateService
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService
from app.services.user_auth_service import require_admin_api_user
from app.tasks import configure_scheduler
from app.utils.json_utils import dumps_json, safeJsonParse


settings = get_settings()
settings.validate_runtime_settings()


def init_database() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _migrate_request_log_columns(db)
        setting = db.get(AppSetting, 1)
        if setting is None:
            setting = AppSetting(id=1)
            db.add(setting)
            db.commit()
            db.refresh(setting)
        ProviderService.sync_legacy_provider_models(db)
        ModelCatalogService.sync_model_catalogs(db)
    finally:
        db.close()


def _migrate_request_log_columns(db) -> None:
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
        "prompt_cost": "ALTER TABLE request_logs ADD COLUMN prompt_cost NUMERIC",
        "completion_cost": "ALTER TABLE request_logs ADD COLUMN completion_cost NUMERIC",
        "total_cost": "ALTER TABLE request_logs ADD COLUMN total_cost NUMERIC",
        "billing_status": "ALTER TABLE request_logs ADD COLUMN billing_status TEXT",
        "billing_multiplier": "ALTER TABLE request_logs ADD COLUMN billing_multiplier FLOAT",
        "channel_price_input_per_1k": "ALTER TABLE request_logs ADD COLUMN channel_price_input_per_1k FLOAT",
        "channel_price_output_per_1k": "ALTER TABLE request_logs ADD COLUMN channel_price_output_per_1k FLOAT",
        "api_client_balance_after": "ALTER TABLE request_logs ADD COLUMN api_client_balance_after NUMERIC",
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
        "api_client_remaining_cost_daily": "ALTER TABLE request_logs ADD COLUMN api_client_remaining_cost_daily NUMERIC",
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
        "price_multiplier": "ALTER TABLE provider_models ADD COLUMN price_multiplier FLOAT NOT NULL DEFAULT 1.0",
        "input_price_per_1k": "ALTER TABLE provider_models ADD COLUMN input_price_per_1k FLOAT",
        "output_price_per_1k": "ALTER TABLE provider_models ADD COLUMN output_price_per_1k FLOAT",
    }
    changed_provider_models = False
    for column, ddl in provider_model_additions.items():
        if column in existing_provider_model_columns:
            continue
        db.execute(text(ddl))
        changed_provider_models = True
    if changed_provider_models:
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
        "cost_limit_daily": "ALTER TABLE api_client_keys ADD COLUMN cost_limit_daily NUMERIC",
        "qps_limit": "ALTER TABLE api_client_keys ADD COLUMN qps_limit INTEGER",
        "rpm_limit": "ALTER TABLE api_client_keys ADD COLUMN rpm_limit INTEGER",
        "tpm_limit": "ALTER TABLE api_client_keys ADD COLUMN tpm_limit INTEGER",
        "cost_limit_total": "ALTER TABLE api_client_keys ADD COLUMN cost_limit_total NUMERIC",
        "total_cost_used": "ALTER TABLE api_client_keys ADD COLUMN total_cost_used NUMERIC NOT NULL DEFAULT 0",
        "balance_amount": "ALTER TABLE api_client_keys ADD COLUMN balance_amount NUMERIC",
        "total_recharge_amount": "ALTER TABLE api_client_keys ADD COLUMN total_recharge_amount NUMERIC NOT NULL DEFAULT 0",
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

    existing_settings_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(app_settings)")).fetchall()
    }
    app_setting_additions = {
        "enable_token_logging": "ALTER TABLE app_settings ADD COLUMN enable_token_logging BOOLEAN NOT NULL DEFAULT 1",
        "enable_payload_logging": "ALTER TABLE app_settings ADD COLUMN enable_payload_logging BOOLEAN NOT NULL DEFAULT 1",
        "enable_stream_response_persist": "ALTER TABLE app_settings ADD COLUMN enable_stream_response_persist BOOLEAN NOT NULL DEFAULT 1",
        "mask_sensitive_fields": "ALTER TABLE app_settings ADD COLUMN mask_sensitive_fields BOOLEAN NOT NULL DEFAULT 1",
        "max_logged_body_bytes": "ALTER TABLE app_settings ADD COLUMN max_logged_body_bytes INTEGER NOT NULL DEFAULT 16384",
        "allow_public_user_registration": "ALTER TABLE app_settings ADD COLUMN allow_public_user_registration BOOLEAN NOT NULL DEFAULT 0",
        "request_log_retention_days": "ALTER TABLE app_settings ADD COLUMN request_log_retention_days INTEGER NOT NULL DEFAULT 90",
        "admin_audit_log_retention_days": "ALTER TABLE app_settings ADD COLUMN admin_audit_log_retention_days INTEGER NOT NULL DEFAULT 180",
        "route_candidate_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN route_candidate_cache_ttl_sec INTEGER NOT NULL DEFAULT 10",
        "model_list_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN model_list_cache_ttl_sec INTEGER NOT NULL DEFAULT 15",
        "provider_status_cache_ttl_sec": "ALTER TABLE app_settings ADD COLUMN provider_status_cache_ttl_sec INTEGER NOT NULL DEFAULT 10",
        "async_request_logging": "ALTER TABLE app_settings ADD COLUMN async_request_logging BOOLEAN NOT NULL DEFAULT 1",
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
    user_columns = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(user_accounts)")).fetchall()
    }
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
    init_database()
    UpstreamClientService.get_client()
    if not scheduler.running:
        configure_scheduler()
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await UpstreamClientService.aclose()


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
        response = await call_next(request)
    finally:
        RuntimeStateService.leave_request()
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Request-Id"] = trace_id
    response.headers["X-Active-Requests"] = str(RuntimeStateService.current_active_requests())
    return response


@app.exception_handler(ApiClientAuthError)
async def api_client_auth_error_handler(request: Request, exc: ApiClientAuthError):
    await _log_api_client_auth_failure(request, exc)
    trace_id = getattr(request.state, "trace_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=OpenAIErrorService.build_error_payload(
            message=exc.message,
            code=exc.code,
            trace_id=trace_id,
            error_type="authentication_error" if exc.status_code in {401, 403} else "rate_limit_error",
            retryable=exc.status_code == 429,
        ),
        headers={"X-Trace-Id": trace_id or ""},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if not request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    trace_id = getattr(request.state, "trace_id", None)
    error_type, default_code, retryable = OpenAIErrorService.classify_status_code(exc.status_code)
    message = OpenAIErrorService.extract_message(exc.detail, fallback="Request failed")
    detail_payload = exc.detail if isinstance(exc.detail, dict) else None
    error_code = default_code
    if isinstance(detail_payload, dict):
        if isinstance(detail_payload.get("code"), str):
            error_code = detail_payload["code"]
        elif isinstance(detail_payload.get("error"), dict) and isinstance(detail_payload["error"].get("code"), str):
            error_code = detail_payload["error"]["code"]
    content = OpenAIErrorService.build_error_payload(
        message=message,
        code=error_code,
        trace_id=trace_id,
        error_type=error_type,
        retryable=retryable,
        detail=detail_payload if isinstance(detail_payload, dict) else None,
    )
    if detail_payload is not None:
        content["detail"] = detail_payload
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers={"X-Trace-Id": trace_id or ""},
    )


async def _log_api_client_auth_failure(request: Request, exc: ApiClientAuthError) -> None:
    if not request.url.path.startswith("/v1/"):
        return
    db = SessionLocal()
    try:
        settings = SettingService.get_or_create(db)
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="ignore") if body_bytes else None
        parsed_body = safeJsonParse(body_text) if body_text else None
        request_body_json = None
        requested_model = None
        if isinstance(parsed_body, dict):
            request_body_json = ProxySafeHelpers.truncate_json(parsed_body, settings.max_logged_body_bytes)
            requested_model = parsed_body.get("model") if isinstance(parsed_body.get("model"), str) else None
        elif body_text:
            request_body_json = body_text[: settings.max_logged_body_bytes]
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
            if item_type in {"image_url", "input_image"}:
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
app.include_router(proxy_router)
