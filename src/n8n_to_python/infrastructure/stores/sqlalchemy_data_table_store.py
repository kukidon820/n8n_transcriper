"""Реализация DataTableStore поверх SQLAlchemy AsyncEngine и JSON-строк."""

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import JSON, Column, Index, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ...application.ports.data_table_store import DataTableCondition, DataTableOperation

_metadata = MetaData()
_rows = Table(
    "n8n_workflow_data_table_rows",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("table_id", String(255), nullable=False),
    Column("payload", JSON, nullable=False),
    Index("ix_n8n_workflow_data_table_rows_table_id", "table_id"),
)


class SQLAlchemyDataTableStore:
    """Хранить строки Data Table в SQL JSON-полях и выполнять row CRUD."""

    def __init__(self, engine: AsyncEngine):
        """Сохранить engine; он должен использовать PostgreSQL в рабочем окружении."""
        self._engine = engine
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    async def execute_row_operation(
        self,
        *,
        table_id: str,
        operation: DataTableOperation,
        values: dict[str, Any],
        conditions: tuple[DataTableCondition, ...],
        match_type: Literal["allConditions", "anyCondition"],
        limit: int,
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        """Выполнить CRUD одной транзакцией и вернуть обработанные строки."""
        await self._ensure_schema()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                select(_rows.c.id, _rows.c.payload)
                .where(_rows.c.table_id == table_id)
                .order_by(_rows.c.id)
            )
            stored = [dict(row) for row in result.mappings()]
            matching = [row for row in stored if _matches(row["payload"], conditions, match_type)][
                :limit
            ]

            if operation == "get":
                return [dict(row["payload"]) for row in matching]
            if operation in {"rowExists", "rowNotExists"}:
                return [dict(row["payload"]) for row in matching]
            if operation == "insert":
                return [await self._insert(connection, table_id, values)]
            if operation in {"update", "upsert"}:
                if not matching and operation == "upsert":
                    return [await self._insert(connection, table_id, values)]
                updated_rows = []
                for row in matching:
                    before = dict(row["payload"])
                    after = _update_payload(before, values)
                    if dry_run:
                        updated_rows.append({"before": before, "after": after})
                    else:
                        await connection.execute(
                            _rows.update().where(_rows.c.id == row["id"]).values(payload=after)
                        )
                        updated_rows.append(after)
                return updated_rows
            if operation == "deleteRows":
                if dry_run:
                    return [dict(row["payload"]) for row in matching]
                if matching:
                    await connection.execute(
                        _rows.delete().where(_rows.c.id.in_([row["id"] for row in matching]))
                    )
                return [dict(row["payload"]) for row in matching]
            raise ValueError(f"Data Table operation {operation!r} is not supported.")

    async def _ensure_schema(self) -> None:
        """Создать внутреннюю таблицу один раз на экземпляр адаптера."""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if not self._schema_ready:
                async with self._engine.begin() as connection:
                    await connection.run_sync(_metadata.create_all)
                self._schema_ready = True

    async def _insert(
        self, connection: Any, table_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Добавить строку и включить системные поля id/createdAt/updatedAt."""
        now = datetime.now(UTC).isoformat()
        payload = {
            **values,
            "createdAt": now,
            "updatedAt": now,
        }
        result = await connection.execute(_rows.insert().values(table_id=table_id, payload=payload))
        row_id = result.inserted_primary_key[0]
        payload["id"] = row_id
        await connection.execute(_rows.update().where(_rows.c.id == row_id).values(payload=payload))
        return payload


def _update_payload(before: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Обновить пользовательские поля и время строки, сохранив её системный ID."""
    safe_values = {key: value for key, value in values.items() if key not in {"id", "createdAt"}}
    return {**before, **safe_values, "updatedAt": datetime.now(UTC).isoformat()}


def _matches(
    row: dict[str, Any],
    conditions: tuple[DataTableCondition, ...],
    match_type: Literal["allConditions", "anyCondition"],
) -> bool:
    """Сопоставить JSON row со всеми или любым заданным условием."""
    if not conditions:
        return True
    matches = [
        _condition_matches(row.get(column), operation, expected)
        for column, operation, expected in conditions
    ]
    return all(matches) if match_type == "allConditions" else any(matches)


def _condition_matches(value: Any, operation: str, expected: Any) -> bool:
    """Применить ограниченный набор сравнений Data Table к одному полю."""
    if operation == "eq":
        return value == expected
    if operation == "neq":
        return value != expected
    if operation == "isEmpty":
        return value is None or value == ""
    if operation == "isNotEmpty":
        return value is not None and value != ""
    if operation == "isTrue":
        return value is True
    if operation == "isFalse":
        return value is False
    if operation in {"like", "notLike"}:
        if not isinstance(value, str) or not isinstance(expected, str):
            return False
        pattern = "^" + re.escape(expected).replace("%", ".*").replace("_", ".") + "$"
        matched = re.match(pattern, value, flags=re.DOTALL) is not None
        return matched if operation == "like" else not matched
    comparisons = {
        "gt": lambda: value > expected,
        "gte": lambda: value >= expected,
        "lt": lambda: value < expected,
        "lte": lambda: value <= expected,
    }
    try:
        return comparisons[operation]()
    except (KeyError, TypeError):
        return False
