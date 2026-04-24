from app.database import SessionLocal
from app.scheduler import scheduler
from app.services.health_service import HealthService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService


async def scheduled_health_check() -> None:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).auto_health_check:
            await HealthService.check_all(db)
    finally:
        db.close()


def scheduled_token_usage_backfill() -> None:
    db = SessionLocal()
    try:
        if SettingService.get_or_create(db).enable_token_logging:
            TokenUsageService.backfill_missing_usage()
    finally:
        db.close()


def configure_scheduler() -> None:
    db = SessionLocal()
    try:
        interval = max(10, SettingService.get_or_create(db).health_check_interval_sec)
    finally:
        db.close()
    scheduler.add_job(
        scheduled_health_check,
        "interval",
        seconds=interval,
        id="provider_health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_token_usage_backfill,
        "interval",
        seconds=15,
        id="token_usage_backfill",
        replace_existing=True,
    )
