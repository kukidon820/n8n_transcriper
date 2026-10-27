"""Checks for the generated project's local TOML credentials file."""

from pathlib import Path

import pytest

from n8n_to_python.runtime.user_config import load_user_config


def test_load_user_config_reads_arbitrary_node_credentials(tmp_path: Path) -> None:
    """Read a real TOML file and retain credential fields without knowing their schema."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[credentials."HTTP node"]\n'
        'n8n_types = ["customAuth"]\n'
        'headers = { "X-API-Key" = "local-test-secret" }\n'
        "\n[database]\n"
        'url = "postgresql+asyncpg://user:pass@localhost/db"\n',
        encoding="utf-8",
    )

    config = load_user_config(config_path)

    assert config.credentials["HTTP node"]["n8n_types"] == ["customAuth"]
    assert config.credentials["HTTP node"]["headers"] == {"X-API-Key": "local-test-secret"}
    assert config.database["url"].endswith("/db")


def test_load_user_config_returns_empty_defaults_if_file_is_not_created(tmp_path: Path) -> None:
    """Allow a project to start without credentials when the workflow needs none."""
    config = load_user_config(tmp_path / "missing.toml")

    assert config.credentials == {}
    assert config.database == {}


def test_load_user_config_hides_secret_values_in_invalid_toml_error(tmp_path: Path) -> None:
    """Do not echo malformed config contents, which might include a secret."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('password = "private-secret"\n', encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_user_config(config_path)

    assert "private-secret" not in str(raised.value)
