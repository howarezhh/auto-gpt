import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def loads_json(value: str | None, default: Any) -> Any:
    """安全解析 JSON 字符串，失败时返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def to_jsonable(value: Any) -> Any:
    """把日期、Decimal 等对象递归转换为可 JSON 序列化值。"""
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
    """以 UTF-8 友好的方式序列化 JSON。"""
    return json.dumps(to_jsonable(value), ensure_ascii=False, **kwargs)


def safeJsonParse(value: str) -> Any:
    """兼容旧命名风格的安全 JSON 解析函数。"""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
