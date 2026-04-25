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
    app_name: str = "aotu-gpt"
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "sqlite:///./data/app.db"
    local_proxy_api_key: str = ""
    request_timeout_ms: int = 30000
    upstream_pool_timeout_s: float = 5.0
    upstream_max_connections: int = 200
    upstream_max_keepalive_connections: int = 50
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
        return self.app_env.strip().lower() in PRODUCTION_ENVS

    def normalized_external_base_url(self) -> str | None:
        value = self.external_base_url.strip()
        return value.rstrip("/") if value else None

    def validate_runtime_settings(self) -> None:
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
        normalized = value.strip()
        if not normalized or normalized == placeholder:
            raise RuntimeError(f"{field_name} must be set to a non-default secret when APP_ENV is production")
        if len(normalized) < 32:
            raise RuntimeError(f"{field_name} must be at least 32 characters when APP_ENV is production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
