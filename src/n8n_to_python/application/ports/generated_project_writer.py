"""Контракт сохранения сгенерированного Python-проекта."""

from pathlib import Path
from typing import Protocol

from ..models.generated_project import GeneratedProject
from ..services.progress import ProgressReporter


class GeneratedProjectWriter(Protocol):
    """Асинхронно сохраняет результат генератора в папку пользователя."""

    async def write(
        self,
        project: GeneratedProject,
        output_directory: Path,
        *,
        progress: ProgressReporter,
    ) -> None:
        """Записать файлы и завершиться ошибкой при небезопасной цели."""
        ...
