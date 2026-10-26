"""SQLAlchemy 2 async реализация PostgresClient на AsyncEngine."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..application.models.postgres_query_result import PostgresQueryResult


class SQLAlchemyPostgresClient:
    """Выполнять SQL в async-транзакции, возвращая сериализуемый результат."""

    def __init__(self, engine: AsyncEngine):
        """Получить уже настроенный engine; владение жизненным циклом остаётся у сборщика."""
        self._engine = engine

    async def close(self) -> None:
        """Освободить соединения переданного engine при завершении его жизненного цикла."""
        await self._engine.dispose()

    async def execute_query(
        self,
        query: str,
        parameters: Mapping[str, Any],
    ) -> PostgresQueryResult:
        """Выполнить текстовый SQL и нормализовать rows/row count."""
        async with self._engine.begin() as connection:
            result = await connection.execute(text(query), dict(parameters))
            if result.returns_rows:
                return PostgresQueryResult(rows=tuple(dict(row) for row in result.mappings().all()))
            return PostgresQueryResult(row_count=result.rowcount)
