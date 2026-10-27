"""Проверки конфигурации SQLAlchemy PostgreSQL engine без сетевых подключений."""

import pytest

from n8n_to_python.infrastructure.database import (
    create_postgres_client_from_env,
    create_postgres_engine_from_env,
)
from n8n_to_python.infrastructure.postgres_client import SQLAlchemyPostgresClient

TEST_URL_VARIABLE = "N8N_TEST_DATABASE_URL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_url", "expected_driver"),
    [
        ("postgresql://test_user:test_password@127.0.0.1/test_db", "postgresql+asyncpg"),
        (
            "postgresql+asyncpg://test_user:test_password@127.0.0.1/test_db",
            "postgresql+asyncpg",
        ),
    ],
)
async def test_engine_factory_accepts_postgresql_asyncpg_urls(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    expected_driver: str,
) -> None:
    """Normalize PostgreSQL URLs to asyncpg without opening a connection."""
    monkeypatch.setenv(TEST_URL_VARIABLE, database_url)

    engine = create_postgres_engine_from_env(variable_name=TEST_URL_VARIABLE)
    try:
        assert engine.url.drivername == expected_driver
        assert "test_password" not in str(engine.url)
    finally:
        await engine.dispose()


def test_engine_factory_requires_the_configured_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain which environment variable must be set without showing secrets."""
    monkeypatch.delenv(TEST_URL_VARIABLE, raising=False)

    with pytest.raises(RuntimeError, match=TEST_URL_VARIABLE):
        create_postgres_engine_from_env(variable_name=TEST_URL_VARIABLE)


def test_engine_factory_rejects_non_postgresql_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject SQLite or other synchronous/asynchronous database URLs clearly."""
    monkeypatch.setenv(TEST_URL_VARIABLE, "sqlite+aiosqlite:///not-postgres.db")

    with pytest.raises(ValueError, match="PostgreSQL с asyncpg"):
        create_postgres_engine_from_env(variable_name=TEST_URL_VARIABLE)


def test_engine_factory_sanitizes_malformed_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not echo a malformed URL that could contain credentials in its error."""
    monkeypatch.setenv(TEST_URL_VARIABLE, "not-a-url-with-private-password")

    with pytest.raises(ValueError, match="некорректный SQLAlchemy URL") as raised:
        create_postgres_engine_from_env(variable_name=TEST_URL_VARIABLE)

    assert "private-password" not in str(raised.value)


@pytest.mark.asyncio
async def test_postgres_client_factory_builds_a_closeable_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a client backed by an AsyncEngine and dispose it via its lifecycle API."""
    monkeypatch.setenv(
        TEST_URL_VARIABLE,
        "postgresql://test_user:test_password@127.0.0.1/test_db",
    )

    client = create_postgres_client_from_env(variable_name=TEST_URL_VARIABLE)

    assert isinstance(client, SQLAlchemyPostgresClient)
    await client.close()
