"""Создание PostgreSQL AsyncEngine из переменной окружения."""

import os

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .postgres_client import SQLAlchemyPostgresClient


def create_postgres_engine_from_env(
    *,
    variable_name: str = "DATABASE_URL",
) -> AsyncEngine:
    """Создать SQLAlchemy 2 async engine, не выводя URL или пароль в логи."""
    database_url = os.environ.get(variable_name)
    if not database_url:
        raise RuntimeError(f"Не задана переменная окружения {variable_name}.")

    parsed_url = make_url(database_url)
    if parsed_url.drivername == "postgresql":
        parsed_url = parsed_url.set(drivername="postgresql+asyncpg")
    if parsed_url.drivername != "postgresql+asyncpg":
        raise ValueError(f"{variable_name} должен указывать на PostgreSQL с asyncpg.")
    return create_async_engine(parsed_url, pool_pre_ping=True)


def create_postgres_client_from_env(
    *,
    variable_name: str = "DATABASE_URL",
) -> SQLAlchemyPostgresClient:
    """Создать PostgreSQL client с отдельным SQLAlchemy async engine."""
    return SQLAlchemyPostgresClient(create_postgres_engine_from_env(variable_name=variable_name))
