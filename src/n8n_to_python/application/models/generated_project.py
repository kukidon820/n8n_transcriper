"""Результат генерации проекта до записи файлов на диск."""

from pydantic import BaseModel, ConfigDict


class GeneratedProject(BaseModel):
    """Содержимое сгенерированного проекта: относительный путь и текст файла."""

    model_config = ConfigDict(frozen=True)

    files: dict[str, str]
