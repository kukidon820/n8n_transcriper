"""Порт выполнения параметризованных SQL-запросов."""

from collections.abc import Mapping
from typing import Any, Protocol

from ..models.postgres_query_result import PostgresQueryResult


class PostgresClient(Protocol):
    """Драйвер-независимый контракт асинхронного SQL-клиента."""

    async def execute_query(
        self,
        query: str,
        parameters: Mapping[str, Any],
    ) -> PostgresQueryResult:
        """Выполнить SQL с bind-параметрами и вернуть строки или row count."""
        ...
