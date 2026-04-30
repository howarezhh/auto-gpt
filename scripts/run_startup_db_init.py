from app.config import get_settings
from app.main import init_database


def main() -> None:
    settings = get_settings()
    init_database(allow_production_ddl=True)
    print(f"Database initialization completed for APP_ENV={settings.app_env}.")


if __name__ == "__main__":
    main()
