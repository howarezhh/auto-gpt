from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402


DEFAULT_SOURCE_SQLITE = PROJECT_ROOT / "data" / "app.db"


def normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg2://")
    return url


def make_engine(url: str) -> Engine:
    return create_engine(url, future=True, pool_pre_ping=True)


def table_count(engine: Engine, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def backup_postgres(url: str, backup_file: Path) -> None:
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    dump_url = normalize_postgres_url(url)
    command = ["pg_dump", "-Fc", "-f", str(backup_file), dump_url]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise SystemExit("pg_dump not found. Install PostgreSQL client tools or omit --backup-target.") from exc


def read_rows(source_engine: Engine, table_name: str, batch_size: int):
    table = Base.metadata.tables[table_name]
    offset = 0
    while True:
        with source_engine.connect() as connection:
            rows = [
                dict(row._mapping)
                for row in connection.execute(select(table).limit(batch_size).offset(offset))
            ]
        if not rows:
            break
        yield rows
        offset += len(rows)


def truncate_target(target_engine: Engine) -> None:
    with target_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(delete(table))


def reset_postgres_sequences(target_engine: Engine) -> None:
    if target_engine.dialect.name != "postgresql":
        return
    with target_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            integer_pks = [column for column in table.primary_key.columns if getattr(column.type, "python_type", None) is int]
            if len(integer_pks) != 1:
                continue
            pk = integer_pks[0]
            sequence_name = connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table.name, "column_name": pk.name},
            ).scalar()
            if not sequence_name:
                continue
            sequence_sql = text(
                """
                SELECT setval(
                    :sequence_name,
                    GREATEST(COALESCE((SELECT MAX("__pk__") FROM "__table__"), 0), 1),
                    COALESCE((SELECT MAX("__pk__") FROM "__table__"), 0) > 0
                )
                """.replace('"__table__"', f'"{table.name}"').replace('"__pk__"', f'"{pk.name}"')
            )
            connection.execute(sequence_sql, {"sequence_name": sequence_name})


def migrate(
    *,
    source_engine: Engine,
    target_engine: Engine,
    execute: bool,
    truncate: bool,
    batch_size: int,
) -> dict[str, Any]:
    report: dict[str, Any] = {"execute": execute, "truncate_target": truncate, "tables": []}
    for table in Base.metadata.sorted_tables:
        source_count = table_count(source_engine, table.name)
        target_count = table_count(target_engine, table.name)
        report["tables"].append(
            {
                "name": table.name,
                "source_rows": source_count,
                "target_rows_before": target_count,
            }
        )

    if not execute:
        return report

    if truncate:
        truncate_target(target_engine)

    for table in Base.metadata.sorted_tables:
        inserted = 0
        for rows in read_rows(source_engine, table.name, batch_size):
            if not rows:
                continue
            try:
                with target_engine.begin() as connection:
                    connection.execute(insert(table), rows)
            except IntegrityError as exc:
                raise SystemExit(
                    f"Insert failed for table {table.name}. "
                    "If the target database is not empty, rerun with --truncate-target after taking a backup."
                ) from exc
            inserted += len(rows)
        for item in report["tables"]:
            if item["name"] == table.name:
                item["inserted_rows"] = inserted
                break

    reset_postgres_sequences(target_engine)
    for item in report["tables"]:
        item["target_rows_after"] = table_count(target_engine, item["name"])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate local SQLite data to PostgreSQL.")
    parser.add_argument("--source-sqlite", default=str(DEFAULT_SOURCE_SQLITE))
    parser.add_argument("--target-database-url", required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--backup-target", default="")
    parser.add_argument("--truncate-target", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_path = Path(args.source_sqlite)
    if not source_path.exists():
        raise SystemExit(f"Source SQLite database does not exist: {source_path}")
    target_url = args.target_database_url.strip()
    if not target_url.startswith("postgresql"):
        raise SystemExit("Target database must be PostgreSQL.")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be greater than 0")
    if args.truncate_target and not args.execute:
        raise SystemExit("--truncate-target requires --execute")
    if args.backup_target:
        backup_postgres(target_url, Path(args.backup_target))

    source_engine = make_engine(f"sqlite:///{source_path.as_posix()}")
    target_engine = make_engine(target_url)
    report = migrate(
        source_engine=source_engine,
        target_engine=target_engine,
        execute=args.execute,
        truncate=args.truncate_target,
        batch_size=args.batch_size,
    )

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"SQLite to PostgreSQL migration {mode} report:")
    for item in report["tables"]:
        suffix = ""
        if args.execute:
            suffix = f", inserted={item.get('inserted_rows', 0)}, target_after={item.get('target_rows_after', '-')}"
        print(
            f"  {item['name']}: source={item['source_rows']}, "
            f"target_before={item['target_rows_before']}{suffix}"
        )
    if not args.execute:
        print("\nNo data was written. Add --execute to import data.")


if __name__ == "__main__":
    main()
