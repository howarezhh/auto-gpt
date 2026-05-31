from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
DEFAULT_SESSION_SECRET = "change-this-session-secret"
DEFAULT_API_KEY_ENCRYPTION_SECRET = "change-this-api-key-encryption-secret"
PRODUCTION_ENVS = {"prod", "production"}


class Settings(BaseSettings):
    """集中管理应用运行时配置。"""

    app_name: str = "aotu-gpt"
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "sqlite:///./data/app.db"
    db_pool_size: int = 8
    db_max_overflow: int = 4
    db_pool_timeout: float = 5.0
    db_pool_recycle: int = 1800
    redis_url: str = "redis://127.0.0.1:6379/0"
    enable_startup_db_init: bool = True
    enable_background_workers: bool = True
    async_request_log_enabled: bool = True
    request_log_queue_worker_count: int = 2
    request_log_queue_batch_size: int = 100
    request_log_ingress_queue_size: int = 20000
    cache_l1_ttl_cap_seconds: float = 1.0
    api_key_auth_cache_ttl_seconds: int = 60
    api_key_auth_l1_cache_ttl_seconds: float = 1.0
    api_key_auth_usage_invalidate_interval_ms: int = 0
    concurrency_lease_ttl_seconds: int = 900
    global_max_active_requests: int = 1000
    global_max_active_streams: int = 1000
    api_key_max_active_requests: int = 1000
    api_key_max_active_streams: int = 1000
    account_max_active_requests: int = 1000
    account_max_active_streams: int = 1000
    provider_max_active_requests: int = 1000
    provider_max_active_streams: int = 1000
    route_capacity_prefilter_enabled: bool = False
    token_finalize_worker_count: int = 2
    token_finalize_queue_size: int = 10000
    provider_success_update_interval_ms: int = 1000
    web_concurrency: int = 4
    gunicorn_timeout: int = 120
    gunicorn_keepalive: int = 75
    gunicorn_graceful_timeout: int = 30
    local_proxy_api_key: str = ""
    request_timeout_ms: int = 60000
    upstream_json_client: str = "aiohttp"
    upstream_stream_client: str = "aiohttp"
    worker_threadpool_tokens: int = 200
    stream_connect_timeout_seconds: int = 10
    stream_first_token_timeout_seconds: int = 60
    stream_idle_timeout_seconds: int = 120
    stream_max_duration_seconds: int = 600
    upstream_pool_timeout_s: float = 10.0
    upstream_max_connections: int = 1200
    upstream_max_keepalive_connections: int = 300
    enable_scheduler: bool = True
    pip_index_url: str = "https://pypi.tuna.tsinghua.edu.cn/simple"
    session_secret_key: str = DEFAULT_SESSION_SECRET
    api_key_encryption_secret: str = DEFAULT_API_KEY_ENCRYPTION_SECRET
    external_base_url: str = ""
    uploads_dir: str = str(UPLOADS_DIR)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def is_production(self) -> bool:
        """判断当前是否处于生产环境。"""
        return self.app_env.strip().lower() in PRODUCTION_ENVS

    def normalized_external_base_url(self) -> str | None:
        """规范化外部访问地址，去掉尾部斜杠。"""
        value = self.external_base_url.strip()
        return value.rstrip("/") if value else None

    def validate_runtime_settings(self) -> None:
        """在启动阶段校验生产环境关键配置。"""
        if not self.is_production():
            return
        self._validate_production_database()
        self._validate_secret(
            field_name="SESSION_SECRET_KEY",
            value=self.session_secret_key,
            placeholder=DEFAULT_SESSION_SECRET,
        )
        self._validate_secret(
            field_name="API_KEY_ENCRYPTION_SECRET",
            value=self.api_key_encryption_secret,
            placeholder=DEFAULT_API_KEY_ENCRYPTION_SECRET,
        )

    @staticmethod
    def _validate_secret(*, field_name: str, value: str, placeholder: str) -> None:
        """校验敏感配置是否仍为默认值或长度不足。"""
        normalized = value.strip()
        if not normalized or normalized == placeholder:
            raise RuntimeError(f"{field_name} must be set to a non-default secret when APP_ENV is production")
        if len(normalized) < 32:
            raise RuntimeError(f"{field_name} must be at least 32 characters when APP_ENV is production")

    def _validate_production_database(self) -> None:
        """禁止生产环境继续使用 SQLite。"""
        normalized = self.database_url.strip().lower()
        if normalized.startswith("sqlite"):
            raise RuntimeError("DATABASE_URL must use PostgreSQL or another service database when APP_ENV is production")


@lru_cache
def get_settings() -> Settings:
    """缓存并返回全局配置实例。"""
    return Settings()
