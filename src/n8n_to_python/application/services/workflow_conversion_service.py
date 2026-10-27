"""Полный Application-сценарий генерации и сохранения Python-проекта."""

from pathlib import Path

from ...domain.models.workflow import Workflow
from ..models.conversion_result import ConversionResult
from ..models.diagnostic import ValidationReport
from ..ports.generated_project_writer import GeneratedProjectWriter
from ..ports.workflow_generator import GenerationBackend
from .progress import ProgressReporter
from .workflow_converter import WorkflowConverter
from .workflow_loader import WorkflowLoader
from .workflow_validator import WorkflowValidator


class WorkflowValidationFailed(ValueError):
    """Проверка workflow вернула ошибки или strict-режим повысил предупреждения."""

    def __init__(self, report: ValidationReport):
        self.report = report
        details = "\n".join(
            f"{item.severity}: {item.code}: {item.message}" for item in report.diagnostics
        )
        super().__init__(f"Workflow {report.workflow_name!r} не прошёл проверку.\n{details}")


class WorkflowConversionService:
    """Координирует загрузку, проверку, генерацию и безопасную запись проекта."""

    def __init__(
        self,
        loader: WorkflowLoader,
        validator: WorkflowValidator,
        converter: WorkflowConverter,
        writer: GeneratedProjectWriter,
        progress: ProgressReporter | None = None,
    ):
        self._loader = loader
        self._validator = validator
        self._converter = converter
        self._writer = writer
        self._progress = progress or ProgressReporter()

    async def execute(
        self,
        output_directory: Path,
        backend: GenerationBackend,
        *,
        strict: bool = False,
        project_name: str | None = None,
    ) -> ConversionResult:
        """Построить проект; ошибки всех шагов передаются вызывающему коду."""
        with self._progress.stage("workflow:load"):
            workflow: Workflow = await self._loader.load()
        report = await self._validator.execute(strict=strict, workflow=workflow)
        with self._progress.stage("workflow:validation-result"):
            if not report.valid:
                raise WorkflowValidationFailed(report)
        project = await self._converter.convert(
            workflow,
            backend,
            project_name=project_name,
        )
        await self._writer.write(project, output_directory, progress=self._progress)
        return ConversionResult(
            backend=backend,
            output_directory=output_directory,
            file_count=len(project.files),
            validation=report,
        )
