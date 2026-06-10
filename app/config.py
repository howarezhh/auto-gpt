from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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
    database_url: str = "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt"
    db_pool_size: int = 0
    db_connections_per_worker: int = 10
    db_max_overflow: int = 0
    db_pool_timeout: float = 30.0
    db_pool_recycle: int = 1800
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_max_connections: int = 200
    redis_socket_connect_timeout_seconds: float = 2.0
    redis_socket_timeout_seconds: float = 2.0
    redis_health_check_interval_seconds: int = 30
    enable_startup_db_init: bool = True
    enable_startup_heavy_sync: bool = False
    startup_billing_backfill_batch_size: int = 5000
    enable_background_workers: bool = True
    async_request_log_enabled: bool = True
    request_log_queue_worker_count: int = 2
    request_log_queue_batch_size: int = 100
    request_log_ingress_queue_size: int = 20000
    logging_event_queue_worker_count: int = 1
    logging_event_queue_batch_size: int = 200
    logging_event_queue_require_local_worker: bool = False
    cache_l1_ttl_cap_seconds: float = 5.0
    cache_l1_max_entries: int = 10000
    cache_redis_ttl_jitter_ratio: float = 0.1
    api_key_auth_cache_ttl_seconds: int = 60
    api_key_auth_negative_cache_ttl_seconds: int = 30
    api_key_auth_l1_cache_ttl_seconds: float = 5.0
    api_key_auth_l1_max_entries: int = 10000
    api_key_auth_user_invalidate_scan_limit: int = 5000
    api_key_auth_usage_invalidate_interval_ms: int = 0
    concurrency_lease_ttl_seconds: int = 900
    global_qps_limit: int = 20
    global_rpm_limit: int = 20
    account_qps_limit: int = 20
    account_rpm_limit: int = 20
    global_max_active_requests: int = 20
    global_max_active_streams: int = 10
    api_key_max_active_requests: int = 20
    api_key_max_active_streams: int = 10
    account_max_active_requests: int = 20
    account_max_active_streams: int = 10
    provider_max_active_requests: int = 20
    provider_max_active_streams: int = 10
    route_capacity_prefilter_enabled: bool = False
    token_finalize_worker_count: int = 2
    token_finalize_batch_size: int = 50
    token_finalize_queue_size: int = 10000
    token_finalize_immediate_delay_ms: int = 0
    token_usage_backfill_interval_seconds: int = 15
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
    v1_request_body_idle_timeout_seconds: int = 15
    stream_connect_timeout_seconds: int = 10
    stream_first_token_timeout_seconds: int = 60
    stream_idle_timeout_seconds: int = 120
    stream_max_duration_seconds: int = 600
    upstream_pool_timeout_s: float = 10.0
    upstream_max_connections: int = 200
    upstream_max_keepalive_connections: int = 50
    upstream_keepalive_expiry_seconds: float = 30.0
    upstream_dns_cache_ttl_seconds: int = 300
    upstream_requests_pool_block: bool = True
    enable_scheduler: bool = True
    pip_index_url: str = "https://pypi.tuna.tsinghua.edu.cn/simple"
    session_secret_key: str = DEFAULT_SESSION_SECRET
    api_key_encryption_secret: str = DEFAULT_API_KEY_ENCRYPTION_SECRET
    external_base_url: str = ""
    uploads_dir: str = str(UPLOADS_DIR)
    responses_chat_adapter_enabled: bool = False
    responses_chat_adapter_storage_type: str = "database"
    responses_chat_adapter_ttl_seconds: int = 86400
    responses_chat_adapter_model_map_json: str = ""
    responses_chat_adapter_max_tool_rounds: int = 10
    responses_chat_adapter_web_search_enabled: bool = False
    responses_chat_adapter_search_proxy_url: str = ""
    responses_chat_adapter_upstream_base_url: str = ""
    responses_chat_adapter_upstream_api_key: str = ""
    responses_chat_adapter_upstreams_json: str = ""
    responses_chat_adapter_context_window_tokens: int = 128000
    responses_chat_adapter_snapshot_max_bytes: int = 1048576
    responses_chat_adapter_db_cleanup_interval_seconds: int = 21600

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def normalize_runtime_defaults(self) -> "Settings":
        """补齐依赖 worker 数的资源默认值，保留显式环境变量覆盖能力。"""
        self._validate_database_url()
        worker_count = max(1, int(self.web_concurrency or 1))
        connections_per_worker = max(1, int(self.db_connections_per_worker or 10))
        if int(self.db_pool_size or 0) <= 0:
            self.db_pool_size = worker_count * connections_per_worker
        if int(self.db_max_overflow or 0) <= 0:
            self.db_max_overflow = max(connections_per_worker, self.db_pool_size // 2)
        self.upstream_max_connections = max(10, min(int(self.upstream_max_connections or 200), 5000))
        self.upstream_max_keepalive_connections = max(
            1,
            min(int(self.upstream_max_keepalive_connections or 50), self.upstream_max_connections),
        )
        self.v1_request_body_idle_timeout_seconds = max(
            1,
            min(int(self.v1_request_body_idle_timeout_seconds or 15), 300),
        )
        self.upstream_keepalive_expiry_seconds = max(
            1.0,
            min(float(self.upstream_keepalive_expiry_seconds or 30.0), 600.0),
        )
        self.upstream_dns_cache_ttl_seconds = max(
            0,
            min(int(self.upstream_dns_cache_ttl_seconds or 0), 3600),
        )
        self.api_key_auth_l1_max_entries = max(100, min(int(self.api_key_auth_l1_max_entries or 10000), 100000))
        self.cache_redis_ttl_jitter_ratio = max(
            0.0,
            min(float(self.cache_redis_ttl_jitter_ratio or 0.0), 0.5),
        )
        self.api_key_auth_negative_cache_ttl_seconds = max(
            0,
            min(int(self.api_key_auth_negative_cache_ttl_seconds or 30), 300),
        )
        self.api_key_auth_user_invalidate_scan_limit = max(
            100,
            min(int(self.api_key_auth_user_invalidate_scan_limit or 5000), 100000),
        )
        self.redis_max_connections = max(10, min(int(self.redis_max_connections or 200), 5000))
        self.redis_socket_connect_timeout_seconds = max(
            0.1,
            min(float(self.redis_socket_connect_timeout_seconds or 2.0), 30.0),
        )
        self.redis_socket_timeout_seconds = max(
            0.1,
            min(float(self.redis_socket_timeout_seconds or 2.0), 30.0),
        )
        self.redis_health_check_interval_seconds = max(
            0,
            min(int(self.redis_health_check_interval_seconds or 30), 300),
        )
        return self

    def is_production(self) -> bool:
        """判断当前是否处于生产环境。"""
        return self.app_env.strip().lower() in PRODUCTION_ENVS

    def normalized_external_base_url(self) -> str | None:
        """规范化外部访问地址，去掉尾部斜杠。"""
        value = self.external_base_url.strip()
        return value.rstrip("/") if value else None

    def validate_runtime_settings(self) -> None:
        """在启动阶段校验关键配置。"""
        self._validate_database_url()
        if not self.is_production():
            return
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

    def _validate_database_url(self) -> None:
        """所有运行环境都必须使用 PostgreSQL。"""
        normalized = self.database_url.strip().lower()
        allowed_prefixes = ("postgresql://", "postgresql+", "postgres://")
        if not normalized.startswith(allowed_prefixes):
            raise RuntimeError("DATABASE_URL must use PostgreSQL, for example postgresql+psycopg://...")


@lru_cache
def get_settings() -> Settings:
    """缓存并返回全局配置实例。"""
    return Settings()
