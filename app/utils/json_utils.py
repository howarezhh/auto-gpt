import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def dumps_json(value: Any, **kwargs) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, **kwargs)


def safeJsonParse(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
