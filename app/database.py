from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings
from app.utils.timezone import BEIJING_TIMEZONE_NAME


class Base(DeclarativeBase):
    pass


settings = get_settings()

engine_kwargs = {
    "future": True,
    "pool_pre_ping": True,
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_timeout": settings.db_pool_timeout,
    "pool_recycle": settings.db_pool_recycle,
}

engine = create_engine(settings.database_url, **engine_kwargs)


@event.listens_for(engine, "connect")
def _set_beijing_database_timezone(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET TIME ZONE '{BEIJING_TIMEZONE_NAME}'")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
