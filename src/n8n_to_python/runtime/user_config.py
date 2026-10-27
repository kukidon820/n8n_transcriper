"""Validated loader for the generated project's local TOML configuration."""

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class UserConfig(BaseModel):
    """Credentials and shared integration settings supplied by the project user."""

    model_config = ConfigDict(extra="forbid")

    credentials: dict[str, dict[str, Any]] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)


def load_user_config(path: Path) -> UserConfig:
    """Load ``config.toml`` without including credential values in validation errors."""
    if not path.exists():
        return UserConfig()

    try:
        raw_config = tomllib.loads(path.read_text(encoding="utf-8"))
        return UserConfig.model_validate(raw_config)
    except (OSError, tomllib.TOMLDecodeError, ValidationError):
        raise ValueError(
            f"Файл конфигурации {path.name} имеет неверный TOML-формат "
            "или неподдерживаемую структуру."
        ) from None
