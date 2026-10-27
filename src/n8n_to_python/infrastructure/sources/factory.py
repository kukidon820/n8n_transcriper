"""Сборка подходящего workflow source по локальному пути или HTTP(S) URL."""

from pathlib import Path
from urllib.parse import urlsplit

from ...application.ports.workflow_source import WorkflowSource
from .git_url import GitWorkflowSource
from .local_file import LocalFileWorkflowSource


def workflow_source_from_location(location: str) -> WorkflowSource:
    """Создать адаптер по вводу пользователя; чтение выполняет WorkflowLoader."""
    parsed = urlsplit(location)
    if parsed.scheme.lower() in {"http", "https"}:
        return GitWorkflowSource(url=location)
    if "://" in location:
        raise ValueError("Поддерживаются локальный путь и HTTP(S) ссылка на JSON workflow")
    return LocalFileWorkflowSource(path=Path(location))
