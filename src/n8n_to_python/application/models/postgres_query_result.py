"""Нормализованный результат SQL-запроса для runtime-узла Postgres."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class PostgresQueryResult(BaseModel):
    """Возвращает строки SELECT/RETURNING либо число изменённых строк."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[dict[str, Any], ...] = ()
    row_count: int | None = None
