"""Тонкий Typer CLI для проверки, инспекции и генерации workflow."""

import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from n8n_to_python.application.models.diagnostic import WorkflowDiagnostic
from n8n_to_python.application.ports.workflow_generator import GenerationBackend
from n8n_to_python.application.services.progress import ProgressEvent, ProgressReporter
from n8n_to_python.application.services.workflow_conversion_service import (
    WorkflowConversionService,
    WorkflowValidationFailed,
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

_PROGRESS_LABELS = {
    "workflow:load": "Загружаю сценарий",
    "workflow:validate": "Проверяю сценарий",
    "workflow:validation-result": "",
    "workflow:inspect": "Анализирую сценарий",
    "conversion:python-core": "",
    "conversion:langgraph": "",
    "python-core:analyze-workflow": "Проверяю узлы",
    "python-core:render-project": "Собираю Python-проект",
    "langgraph:analyze-workflow": "Проверяю узлы",
    "langgraph:render-project": "Собираю проект LangGraph",
    "project:write-files": "Записываю файлы проекта",
}

app = typer.Typer(
    name="n8n-to-python",
    help="Преобразование n8n workflow в Python-проект.",
    no_args_is_help=True,
)


def _progress_callback(event: ProgressEvent) -> None:
    """Показывать только короткое русское сообщение о начале этапа."""
    if event.status != "started":
        return
    label = _PROGRESS_LABELS.get(event.stage, "Обрабатываю данные")
    if label:
        typer.echo(f"{label}...", err=True)


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
    """Показать короткие русские замечания без кодов и Pydantic-деталей."""
    for diagnostic in diagnostics:
        logger.debug(
            "Диагностика %s для узла %r: %s",
            diagnostic.code,
            diagnostic.node_name,
            diagnostic.message,
        )
        label = "Ошибка" if diagnostic.severity == "error" else "Предупреждение"
        node = f"Узел «{diagnostic.node_name}»: " if diagnostic.node_name else ""
        message = _brief_diagnostic_message(diagnostic)
        typer.echo(f"{label}: {node}{message}", err=True)


def _brief_diagnostic_message(diagnostic: WorkflowDiagnostic) -> str:
    """Перевести внутреннюю диагностику в короткое сообщение для пользователя."""
    if diagnostic.code == "UNSUPPORTED_NODE_FEATURE":
        if diagnostic.node_type == "n8n-nodes-base.code":
            return (
                "JavaScript-код не поддерживается; используйте Python или доработайте узел вручную."
            )
        return "Возможность узла пока не поддерживается; её нужно добавить вручную."

    if diagnostic.code == "UNSUPPORTED_NODE_TYPE":
        return "Этот тип узла пока не поддерживается."

    if diagnostic.code == "UNSUPPORTED_NODE_VERSION":
        return "Эта версия узла пока не поддерживается."

    if diagnostic.code == "INVALID_NODE_CONFIGURATION":
        if diagnostic.node_type == "n8n-nodes-base.emailSend":
            if "fileAttachments" in diagnostic.message:
                return (
                    "Настройки письма выходят за поддерживаемый набор; "
                    "ZIP-вложение нужно добавить вручную."
                )
            return "Некоторые настройки письма пока не поддерживаются."
        return "Настройки узла выходят за поддерживаемый набор."

    if diagnostic.code == "NO_SUPPORTED_TRIGGER":
        return "Не найден поддерживаемый стартовый узел."

    return "Проверьте этот узел и его настройки."


@app.callback()
def configure_cli() -> None:
    """Сообщить пользователю о запуске CLI."""
    logger.debug("Запущен конвертер workflow n8n.")


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
        typer.echo(f"Проверка завершена: «{report.workflow_name}». Узлов: {report.node_count}.")
        return
    typer.echo(f"Проверка не пройдена: «{report.workflow_name}».", err=True)
    raise typer.Exit(code=1)


@app.command()
def inspect(
    source: Annotated[str, typer.Argument(help="Путь к .json или HTTP(S) ссылка на workflow.")],
) -> None:
    """Показать безопасную сводку workflow, узлов и связей."""
    _, _, inspector, _, _ = _build_command_services(source)
    report = asyncio.run(inspector.execute())
    typer.echo(f"Сценарий: {report.workflow_name}")
    typer.echo(
        f"Узлов: {report.node_count}; связей: {report.connection_count}; "
        f"стартовые узлы: {', '.join(report.trigger_names) or 'нет'}"
    )
    for node in report.nodes:
        support = "поддерживается" if node.supported else "есть ограничения"
        trigger = ", запускающий узел" if node.is_trigger else ""
        typer.echo(
            f"- {node.name}: {node.node_type}, версия {node.type_version:g}; "
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
    backend_name = "Python Core" if backend is GenerationBackend.PYTHON_CORE else "LangGraph"
    typer.echo(
        f"Создан проект ({backend_name}): файлов {result.file_count}; "
        f"папка: {result.output_directory.expanduser()}"
    )


def main() -> None:
    """Настроить логи и запустить Typer CLI из консоли или IDE."""
    _configure_console_encoding()
    configure_logging()
    try:
        app()
    except WorkflowValidationFailed as error:
        _display_diagnostics(error.report.diagnostics)
        typer.echo(
            f"Преобразование остановлено: «{error.report.workflow_name}». "
            "Исправьте отмеченные узлы.",
            err=True,
        )
        raise typer.Exit(code=1) from error

    except typer.Exit:
        raise
    except FileExistsError as error:
        logger.error("Папка результата не пуста. Укажите пустую папку.")
        logger.debug("Подробности ошибки записи", exc_info=error)
        raise typer.Exit(code=1) from error
    except FileNotFoundError as error:
        logger.error("Файл не найден. Проверьте путь.")
        logger.debug("Подробности ошибки чтения", exc_info=error)
        raise typer.Exit(code=1) from error
    except Exception as error:
        logger.error("Не удалось выполнить команду. Подробности записаны в файл журнала.")
        logger.debug("Технические подробности ошибки", exc_info=error)
        raise typer.Exit(code=1) from error


def _configure_console_encoding() -> None:
    """Keep legacy Windows code pages from crashing on workflow names with Unicode symbols."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


if __name__ == "__main__":
    main()
