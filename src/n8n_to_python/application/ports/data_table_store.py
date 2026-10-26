"""Порт постоянного хранилища строк для адаптера n8n Data Table."""

from typing import Any, Literal, Protocol

DataTableOperation = Literal[
    "get", "insert", "update", "upsert", "deleteRows", "rowExists", "rowNotExists"
]
DataTableCondition = tuple[str, str, Any]


class DataTableStore(Protocol):
    """Операции со строками Data Table без привязки к конкретной БД."""

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
        """Выполнить одну строковую операцию и вернуть затронутые строки."""
        ...
