from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import Base, SessionLocal, engine
from app.models import AppSetting
from app.routers.api_keys import router as api_keys_router
from app.routers.playground_api import router as playground_api_router
from app.routers.dashboard import router as dashboard_router
from app.routers.conversations import router as conversations_router
from app.routers.logs import router as logs_router
from app.routers.metrics import router as metrics_router
from app.routers.pages import router as pages_router
from app.routers.provider_models import router as provider_models_router
from app.routers.providers import router as providers_router
from app.routers.proxy import router as proxy_router
from app.routers.settings import router as settings_router
from app.scheduler import scheduler
from app.services.api_key_service import ApiClientAuthError
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.tasks import configure_scheduler
from app.utils.json_utils import dumps_json, safeJsonParse


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
    finally:
        db.close()


def _migrate_request_log_columns(db) -> None:
    existing = {
        row[1]
        for row in db.execute(text("PRAGMA table_info(request_logs)")).fetchall()
    }
    additions = {
        "requested_model": "ALTER TABLE request_logs ADD COLUMN requested_model TEXT",
        "resolved_provider_model_id": "ALTER TABLE request_logs ADD COLUMN resolved_provider_model_id INTEGER",
        "is_stream": "ALTER TABLE request_logs ADD COLUMN is_stream BOOLEAN NOT NULL DEFAULT 0",
        "has_image": "ALTER TABLE request_logs ADD COLUMN has_image BOOLEAN NOT NULL DEFAULT 0",
        "request_id": "ALTER TABLE request_logs ADD COLUMN request_id TEXT",
        "conversation_key": "ALTER TABLE request_logs ADD COLUMN conversation_key TEXT",
        "prompt_tokens": "ALTER TABLE request_logs ADD COLUMN prompt_tokens INTEGER",
        "completion_tokens": "ALTER TABLE request_logs ADD COLUMN completion_tokens INTEGER",
        "total_tokens": "ALTER TABLE request_logs ADD COLUMN total_tokens INTEGER",
        "finish_reason": "ALTER TABLE request_logs ADD COLUMN finish_reason TEXT",
        "upstream_request_id": "ALTER TABLE request_logs ADD COLUMN upstream_request_id TEXT",
        "request_body_json": "ALTER TABLE request_logs ADD COLUMN request_body_json TEXT",
        "response_body_json": "ALTER TABLE request_logs ADD COLUMN response_body_json TEXT",
        "response_text": "ALTER TABLE request_logs ADD COLUMN response_text TEXT",
        "api_client_key_id": "ALTER TABLE request_logs ADD COLUMN api_client_key_id INTEGER",
        "api_client_key_name": "ALTER TABLE request_logs ADD COLUMN api_client_key_name TEXT",
        "api_client_key_prefix": "ALTER TABLE request_logs ADD COLUMN api_client_key_prefix TEXT",
        "api_client_auth_result": "ALTER TABLE request_logs ADD COLUMN api_client_auth_result TEXT",
        "api_client_remaining_tokens": "ALTER TABLE request_logs ADD COLUMN api_client_remaining_tokens INTEGER",
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
    }
    changed_provider_models = False
    for column, ddl in provider_model_additions.items():
        if column in existing_provider_model_columns:
            continue
        db.execute(text(ddl))
        changed_provider_models = True
    if changed_provider_models:
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
        "ix_request_logs_request_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_request_id ON request_logs (request_id)",
        "ix_request_logs_conversation_key": "CREATE INDEX IF NOT EXISTS ix_request_logs_conversation_key ON request_logs (conversation_key)",
        "ix_request_logs_api_client_key_id": "CREATE INDEX IF NOT EXISTS ix_request_logs_api_client_key_id ON request_logs (api_client_key_id)",
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    if not scheduler.running:
        configure_scheduler()
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="aotu-gpt", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.exception_handler(ApiClientAuthError)
async def api_client_auth_error_handler(request: Request, exc: ApiClientAuthError):
    await _log_api_client_auth_failure(request, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": "invalid_request_error",
                "code": exc.code,
            }
        },
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
            model_name=requested_model,
            requested_model=requested_model,
            request_path=request.url.path,
            is_stream=bool(isinstance(parsed_body, dict) and parsed_body.get("stream") is True),
            has_image=ProxySafeHelpers.payload_has_image(parsed_body if isinstance(parsed_body, dict) else None),
            success=False,
            status_code=exc.status_code,
            request_body_json=request_body_json,
            message=exc.message,
            api_client_key_id=exc.api_client_key_id,
            api_client_key_name=exc.api_client_key_name,
            api_client_key_prefix=exc.api_client_key_prefix,
            api_client_auth_result=exc.code,
            api_client_remaining_tokens=exc.remaining_tokens,
            api_client_policy_snapshot_json=exc.policy_snapshot_json,
            trace=[{"result": "auth_rejected", "error": exc.code, "latency_ms": 0}],
            token_request_payload=parsed_body if isinstance(parsed_body, dict) else None,
            schedule_token_fill=False,
        )
    finally:
        db.close()


class ProxySafeHelpers:
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

app.include_router(pages_router)
app.include_router(dashboard_router)
app.include_router(conversations_router)
app.include_router(api_keys_router)
app.include_router(providers_router)
app.include_router(provider_models_router)
app.include_router(settings_router)
app.include_router(logs_router)
app.include_router(metrics_router)
app.include_router(playground_api_router)
app.include_router(proxy_router)
