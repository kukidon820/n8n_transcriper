"""Результат успешной генерации и записи проекта."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..ports.workflow_generator import GenerationBackend
from .diagnostic import ValidationReport


class ConversionResult(BaseModel):
    """Сводка конвертации без хранения содержимого сгенерированных файлов."""

    model_config = ConfigDict(frozen=True)

    backend: GenerationBackend
    output_directory: Path
    file_count: int = Field(ge=0)
    validation: ValidationReport
