from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database import SessionLocal
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.cache_service import CacheService
from app.services.provider_service import ProviderService
from app.services.redis_service import RedisService
from app.services.setting_service import SettingService


DEFAULT_MAX_QPS = 20
DEFAULT_MAX_RPM = 20
DEFAULT_MAX_ACTIVE_REQUESTS = 20
DEFAULT_MAX_ACTIVE_STREAMS = 10
AUTH_CACHE_INVALIDATE_SCAN_LIMIT = 10000
AUTH_CACHE_INVALIDATE_DELETE_BATCH = 500


def _columns(db, table_name: str) -> set[str]:
    inspector = inspect(db.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_provider_rpm_column(db) -> bool:
    existing = _columns(db, "providers")
    if existing and "max_rpm" not in existing:
        db.execute(text("ALTER TABLE providers ADD COLUMN max_rpm INTEGER DEFAULT 20"))
        return True
    return False


def _ensure_capacity_setting_columns(db) -> int:
    existing = _columns(db, "app_settings")
    if not existing:
        return 0
    additions = {
        "global_max_active_requests": "ALTER TABLE app_settings ADD COLUMN global_max_active_requests INTEGER DEFAULT 20",
        "global_max_active_streams": "ALTER TABLE app_settings ADD COLUMN global_max_active_streams INTEGER DEFAULT 10",
        "api_key_max_active_requests": "ALTER TABLE app_settings ADD COLUMN api_key_max_active_requests INTEGER DEFAULT 20",
        "api_key_max_active_streams": "ALTER TABLE app_settings ADD COLUMN api_key_max_active_streams INTEGER DEFAULT 10",
        "account_max_active_requests": "ALTER TABLE app_settings ADD COLUMN account_max_active_requests INTEGER DEFAULT 20",
        "account_max_active_streams": "ALTER TABLE app_settings ADD COLUMN account_max_active_streams INTEGER DEFAULT 10",
        "provider_max_active_requests": "ALTER TABLE app_settings ADD COLUMN provider_max_active_requests INTEGER DEFAULT 20",
        "provider_max_active_streams": "ALTER TABLE app_settings ADD COLUMN provider_max_active_streams INTEGER DEFAULT 10",
    }
    changed = 0
    for column, ddl in additions.items():
        if column in existing:
            continue
        db.execute(text(ddl))
        changed += 1
    return changed


def _invalidate_auth_cache_keys() -> None:
    try:
        client = RedisService.get_sync_client()
        for prefix in ("auth:key_hash:", "auth:api_key:", "auth:user:"):
            batch = []
            scanned = 0
            for key in client.scan_iter(match=f"{prefix}*", count=100):
                batch.append(key)
                scanned += 1
                if len(batch) >= AUTH_CACHE_INVALIDATE_DELETE_BATCH:
                    client.delete(*batch)
                    batch.clear()
                if scanned >= AUTH_CACHE_INVALIDATE_SCAN_LIMIT:
                    break
            if batch:
                client.delete(*batch)
    except Exception:
        return


def apply_defaults() -> dict[str, int | bool]:
    db = SessionLocal()
    try:
        added_provider_rpm_column = _ensure_provider_rpm_column(db)
        added_setting_columns = _ensure_capacity_setting_columns(db)
        setting = SettingService.get_or_create(db)
        setting.global_max_active_requests = DEFAULT_MAX_ACTIVE_REQUESTS
        setting.global_max_active_streams = DEFAULT_MAX_ACTIVE_STREAMS
        setting.api_key_max_active_requests = DEFAULT_MAX_ACTIVE_REQUESTS
        setting.api_key_max_active_streams = DEFAULT_MAX_ACTIVE_STREAMS
        setting.account_max_active_requests = DEFAULT_MAX_ACTIVE_REQUESTS
        setting.account_max_active_streams = DEFAULT_MAX_ACTIVE_STREAMS
        setting.provider_max_active_requests = DEFAULT_MAX_ACTIVE_REQUESTS
        setting.provider_max_active_streams = DEFAULT_MAX_ACTIVE_STREAMS

        provider_result = db.execute(
            text(
                """
                UPDATE providers
                SET max_qps = :max_qps,
                    max_rpm = :max_rpm,
                    max_active_requests = :max_active_requests,
                    max_active_streams = :max_active_streams
                """
            ),
            {
                "max_qps": DEFAULT_MAX_QPS,
                "max_rpm": DEFAULT_MAX_RPM,
                "max_active_requests": DEFAULT_MAX_ACTIVE_REQUESTS,
                "max_active_streams": DEFAULT_MAX_ACTIVE_STREAMS,
            },
        )
        api_key_result = db.execute(
            text(
                """
                UPDATE api_client_keys
                SET qps_limit = :qps_limit,
                    rpm_limit = :rpm_limit
                """
            ),
            {
                "qps_limit": DEFAULT_MAX_QPS,
                "rpm_limit": DEFAULT_MAX_RPM,
            },
        )
        db.commit()
        SettingService.invalidate_runtime_cache()
        ProviderService.invalidate_provider_runtime_cache()
        CacheService.invalidate_prefix("auth:key_hash:")
        CacheService.invalidate_prefix("auth:api_key:")
        CacheService.invalidate_prefix("auth:user:")
        _invalidate_auth_cache_keys()
        ApiKeyAuthCache._local_cache.clear()
        ApiKeyAuthCache._local_api_key_hashes.clear()
        return {
            "added_provider_rpm_column": added_provider_rpm_column,
            "added_setting_columns": added_setting_columns,
            "providers_updated": int(provider_result.rowcount or 0),
            "api_keys_updated": int(api_key_result.rowcount or 0),
            "settings_updated": 1,
        }
    finally:
        db.close()


if __name__ == "__main__":
    result = apply_defaults()
    print("capacity defaults applied:")
    for key, value in result.items():
        print(f"- {key}: {value}")
