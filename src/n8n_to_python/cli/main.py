"""Тонкий Typer CLI для проверки, инспекции и генерации workflow."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from n8n_to_python.application.models.diagnostic import WorkflowDiagnostic
from n8n_to_python.application.ports.workflow_generator import GenerationBackend
from n8n_to_python.application.services.progress import ProgressEvent, ProgressReporter
from n8n_to_python.application.services.workflow_conversion_service import (
    WorkflowConversionService,
)
from n8n_to_python.application.services.workflow_converter import WorkflowConverter
from n8n_to_python.application.services.workflow_inspector import WorkflowInspector
from n8n_to_python.application.services.workflow_loader import WorkflowLoader
from n8n_to_python.application.services.workflow_validator import WorkflowValidator
from n8n_to_python.cli.logging_config import configure_logging
from n8n_to_python.converters.langgraph import LangGraphGenerator
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.converters.python_core import PythonCoreGenerator
from n8n_to_python.infrastructure.project_writer import GeneratedProjectFileWriter
from n8n_to_python.infrastructure.sources.factory import workflow_source_from_location

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="n8n-to-python",
    help="Преобразование n8n workflow в Python-проект.",
    no_args_is_help=True,
)


def _progress_callback(event: ProgressEvent) -> None:
    """Показывать пользователю понятный статус каждого Application-этапа."""
    if event.status == "started":
        message = f"Начато: {event.stage}"
    elif event.status == "succeeded":
        duration = event.duration_seconds or 0
        message = f"Готово: {event.stage} ({duration:.2f} с)"
    else:
        message = f"Ошибка: {event.stage} ({event.error_type or 'неизвестная ошибка'})"
    typer.echo(message, err=True)


def _build_command_services(
    source: str,
) -> tuple[
    WorkflowLoader,
    WorkflowValidator,
    WorkflowInspector,
    WorkflowConverter,
    ProgressReporter,
]:
    """Собрать Application use cases с инфраструктурными адаптерами."""
    progress = ProgressReporter(_progress_callback)
    loader = WorkflowLoader(workflow_source_from_location(source))
    registry = default_node_converter_registry()
    validator = WorkflowValidator(loader, registry, progress)
    inspector = WorkflowInspector(loader, registry, progress)
    converter = WorkflowConverter(
        {
            GenerationBackend.PYTHON_CORE: PythonCoreGenerator(converter_registry=registry),
            GenerationBackend.LANGGRAPH: LangGraphGenerator(converter_registry=registry),
        },
        progress=progress,
    )
    return loader, validator, inspector, converter, progress


def _display_diagnostics(diagnostics: Sequence[WorkflowDiagnostic]) -> None:
    """Форматировать структурированные замечания Application для терминала."""
    for diagnostic in diagnostics:
        label = "ОШИБКА" if diagnostic.severity == "error" else "ПРЕДУПРЕЖДЕНИЕ"
        node = f" [{diagnostic.node_name}]" if diagnostic.node_name else ""
        typer.echo(f"{label} {diagnostic.code}{node}: {diagnostic.message}")


@app.callback()
def configure_cli() -> None:
    """Сообщить пользователю о запуске CLI."""
    logger.info("Конвертер n8n workflow запущен")


@app.command()
def validate(
    source: Annotated[str, typer.Argument(help="Путь к .json или HTTP(S) ссылка на workflow.")],
    strict: Annotated[
        bool, typer.Option("--strict", help="Считать предупреждения ошибками.")
    ] = False,
) -> None:
    """Проверить JSON, структуру workflow и поддержку узлов."""
    _, validator, _, _, _ = _build_command_services(source)
    report = asyncio.run(validator.execute(strict=strict))
    _display_diagnostics(report.diagnostics)
    if report.valid:
        typer.echo(f"Workflow {report.workflow_name!r} проверен: {report.node_count} узлов.")
        return
    typer.echo(f"Workflow {report.workflow_name!r} не прошёл проверку.", err=True)
    raise typer.Exit(code=1)


@app.command()
def inspect(
    source: Annotated[str, typer.Argument(help="Путь к .json или HTTP(S) ссылка на workflow.")],
) -> None:
    """Показать безопасную сводку workflow, узлов и связей."""
    _, _, inspector, _, _ = _build_command_services(source)
    report = asyncio.run(inspector.execute())
    typer.echo(f"Workflow: {report.workflow_name}")
    typer.echo(
        f"Узлов: {report.node_count}; связей: {report.connection_count}; "
        f"триггеры: {', '.join(report.trigger_names) or 'нет'}"
    )
    for node in report.nodes:
        support = "поддерживается" if node.supported else "есть ограничения"
        trigger = ", trigger" if node.is_trigger else ""
        typer.echo(
            f"- {node.name}: {node.node_type} v{node.type_version:g}; "
            f"связей {node.connection_count}; {support}{trigger}"
        )
    _display_diagnostics(report.diagnostics)


@app.command()
def convert(
    source: Annotated[str, typer.Argument(help="Путь к .json или HTTP(S) ссылка на workflow.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Папка для нового сгенерированного проекта."),
    ],
    backend: Annotated[
        GenerationBackend,
        typer.Option(help="Генератор проекта: python-core или langgraph."),
    ] = GenerationBackend.PYTHON_CORE,
    project_name: Annotated[
        str | None,
        typer.Option("--project-name", help="Имя Python-проекта; по умолчанию имя workflow."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Считать предупреждения о поддержке ошибками."),
    ] = False,
) -> None:
    """Создать отдельный Python-проект для выбранного backend."""
    loader, validator, _, converter_service, progress = _build_command_services(source)
    conversion = WorkflowConversionService(
        loader,
        validator,
        converter_service,
        GeneratedProjectFileWriter(),
        progress,
    )
    result = asyncio.run(
        conversion.execute(
            output,
            backend,
            strict=strict,
            project_name=project_name,
        )
    )
    _display_diagnostics(result.validation.diagnostics)
    typer.echo(
        f"Создан проект {backend.value!r}: {result.file_count} файлов в "
        f"{result.output_directory.expanduser()}"
    )


def main() -> None:
    """Настроить логи и запустить Typer CLI из консоли или IDE."""
    configure_logging()
    try:
        app()
    except Exception as error:
        logger.error("Не удалось выполнить команду: %s", error)
        logger.debug("Подробная информация об ошибке", exc_info=True)
        raise typer.Exit(code=1) from error


if __name__ == "__main__":
    main()
