import json
from typing import Any


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def dumps_json(value: Any, **kwargs) -> str:
    return json.dumps(value, ensure_ascii=False, **kwargs)


def safeJsonParse(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
