"""Безопасная запись GeneratedProject в локальную папку."""

import asyncio
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field, field_validator

from ..application.models.generated_project import GeneratedProject
from ..application.services.progress import ProgressReporter


class ProjectFile(BaseModel):
    """Проверенный относительный путь и содержимое одного файла проекта."""

    path: str = Field(min_length=1)
    content: str

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """Запретить абсолютные пути и выход за пределы output directory."""
        normalized = value.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(normalized)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or not posix_path.parts
            or any(part in {".", ".."} for part in posix_path.parts)
        ):
            raise ValueError("Путь файла проекта должен быть безопасным относительным путём")
        return posix_path.as_posix()


class GeneratedProjectFileWriter:
    """Записывает только безопасные пути и не перезаписывает непустую папку."""

    async def write(
        self,
        project: GeneratedProject,
        output_directory: Path,
        *,
        progress: ProgressReporter,
    ) -> None:
        """Проверить все пути, затем выполнить блокирующую запись в worker thread."""
        with progress.stage("project:write-files"):
            files = [
                ProjectFile(path=path, content=content) for path, content in project.files.items()
            ]
            self._validate_file_tree(files)
            await asyncio.to_thread(self._write_files, files, output_directory)

    @staticmethod
    def _validate_file_tree(files: list[ProjectFile]) -> None:
        """Отклонить нормализованные дубликаты и конфликт файла с родительской папкой."""
        paths = {item.path for item in files}
        if len(paths) != len(files):
            raise ValueError("В сгенерированном проекте есть повторяющиеся пути файлов")
        for item in files:
            parent = PurePosixPath(item.path).parent
            while parent.parts:
                if parent.as_posix() in paths:
                    raise ValueError(
                        f"Путь {item.path!r} конфликтует с файлом {parent.as_posix()!r}"
                    )
                parent = parent.parent

    @staticmethod
    def _write_files(files: list[ProjectFile], output_directory: Path) -> None:
        """Создать цель или использовать пустую папку; существующие файлы не трогать."""
        output = output_directory.expanduser().resolve()
        if output.exists():
            if not output.is_dir():
                raise NotADirectoryError(f"Путь результата не является папкой: {output}")
            if any(output.iterdir()):
                raise FileExistsError(f"Папка результата не пуста, файлы не перезаписаны: {output}")
        else:
            output.mkdir(parents=True)

        for item in files:
            target = output.joinpath(*PurePosixPath(item.path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as file:
                file.write(item.content)
