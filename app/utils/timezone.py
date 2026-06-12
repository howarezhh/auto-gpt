from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo


BEIJING_TIMEZONE_NAME = "Asia/Shanghai"
BEIJING_TZ = ZoneInfo(BEIJING_TIMEZONE_NAME)


def configure_beijing_timezone() -> None:
    """让 Python 运行时、本地日志和依赖本地时区的库统一按北京时间工作。"""
    os.environ["TZ"] = BEIJING_TIMEZONE_NAME
    if hasattr(time, "tzset"):
        time.tzset()


def now_beijing_aware() -> datetime:
    return datetime.now(BEIJING_TZ)


def now_beijing() -> datetime:
    return now_beijing_aware().replace(tzinfo=None)


def timestamp_to_beijing(timestamp: float | int) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=BEIJING_TZ).replace(tzinfo=None)


def beijing_timestamp() -> int:
    return int(now_beijing_aware().timestamp())


def to_beijing(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def isoformat_beijing(value: datetime | None = None) -> str:
    resolved = to_beijing(value) if value is not None else now_beijing()
    return resolved.isoformat() if resolved is not None else ""


def beijing_date_key(value: datetime | None = None) -> str:
    resolved = to_beijing(value) if value is not None else now_beijing()
    return resolved.strftime("%Y%m%d") if resolved is not None else ""


def beijing_month_key(value: datetime | None = None) -> str:
    resolved = to_beijing(value) if value is not None else now_beijing()
    return resolved.strftime("%Y%m") if resolved is not None else ""


def beijing_minute_key(value: datetime | None = None) -> str:
    resolved = to_beijing(value) if value is not None else now_beijing()
    return resolved.strftime("%Y%m%d%H%M") if resolved is not None else ""


configure_beijing_timezone()
