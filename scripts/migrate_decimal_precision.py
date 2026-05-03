from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import engine
from app.utils.decimal_utils import (
    DB_MONEY_PRECISION,
    DB_MONEY_SCALE,
    DB_MULTIPLIER_PRECISION,
    DB_MULTIPLIER_SCALE,
    DB_PRICE_PRECISION,
    DB_PRICE_SCALE,
)


MONEY_TYPE = f"NUMERIC({DB_MONEY_PRECISION}, {DB_MONEY_SCALE})"
PRICE_TYPE = f"NUMERIC({DB_PRICE_PRECISION}, {DB_PRICE_SCALE})"
MULTIPLIER_TYPE = f"NUMERIC({DB_MULTIPLIER_PRECISION}, {DB_MULTIPLIER_SCALE})"


def _alter_column_if_exists(connection, table_name: str, column_name: str, target_type: str) -> bool:
    inspector = inspect(connection)
    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in existing_columns:
        return False
    connection.execute(
        text(
            f"ALTER TABLE {table_name} "
            f"ALTER COLUMN {column_name} TYPE {target_type} "
            f"USING {column_name}::{target_type}"
        )
    )
    return True


def main() -> int:
    dialect_name = engine.dialect.name
    if dialect_name != "postgresql":
        print(f"当前数据库方言为 {dialect_name}，该脚本仅用于 PostgreSQL 精度迁移。")
        return 0

    field_groups = {
        "model_catalogs": {
            PRICE_TYPE: [
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
            ],
        },
        "provider_models": {
            MULTIPLIER_TYPE: ["price_multiplier"],
            PRICE_TYPE: [
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
            ],
        },
        "request_logs": {
            MONEY_TYPE: [
                "prompt_cost",
                "completion_cost",
                "total_cost",
                "api_client_balance_after",
                "api_client_remaining_cost_daily",
            ],
            MULTIPLIER_TYPE: ["billing_multiplier"],
            PRICE_TYPE: [
                "channel_price_input_per_1k",
                "channel_price_output_per_1k",
                "channel_price_cache_per_1k",
            ],
        },
        "api_client_keys": {
            MONEY_TYPE: [
                "cost_limit_daily",
                "cost_limit_total",
                "total_cost_used",
                "balance_amount",
                "total_recharge_amount",
            ],
        },
        "user_accounts": {
            MONEY_TYPE: [
                "balance_amount",
                "frozen_amount",
                "total_recharge_amount",
                "cost_limit_total",
                "cost_limit_daily",
                "cost_limit_monthly",
            ],
        },
        "api_client_billing_records": {
            MONEY_TYPE: ["amount", "balance_after"],
            PRICE_TYPE: ["unit_input_price_per_1k", "unit_output_price_per_1k"],
        },
        "user_account_billing_records": {
            MONEY_TYPE: ["amount", "balance_after"],
            PRICE_TYPE: ["unit_input_price_per_1k", "unit_output_price_per_1k"],
        },
        "api_key_policy_templates": {
            MONEY_TYPE: ["cost_limit_total"],
        },
    }

    altered_columns: list[str] = []
    with engine.begin() as connection:
        for table_name, type_map in field_groups.items():
            for target_type, columns in type_map.items():
                for column_name in columns:
                    if _alter_column_if_exists(connection, table_name, column_name, target_type):
                        altered_columns.append(f"{table_name}.{column_name} -> {target_type}")

    if not altered_columns:
        print("未发现需要迁移的列。")
        return 0

    print("已完成以下列的精度迁移：")
    for item in altered_columns:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
