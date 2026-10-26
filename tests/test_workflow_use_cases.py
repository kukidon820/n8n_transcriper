"""Проверки Application-сценариев validate/inspect на локальных JSON workflow."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from n8n_to_python.application.services.workflow_inspector import WorkflowInspector
from n8n_to_python.application.services.workflow_loader import WorkflowLoader
from n8n_to_python.application.services.workflow_validator import WorkflowValidator
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.infrastructure.sources.local_file import LocalFileWorkflowSource

FIXTURES = Path(__file__).parent / "fixtures"


def make_loader(filename: str) -> WorkflowLoader:
    """Создать настоящий локальный source и существующий Application loader."""
    return WorkflowLoader(LocalFileWorkflowSource(path=FIXTURES / filename))


@pytest.mark.asyncio
async def test_validate_reports_a_supported_workflow_without_warnings() -> None:
    """Проверить полностью поддержанный локальный граф и пустую диагностику."""
    validator = WorkflowValidator(
        make_loader("converters_basic.json"), default_node_converter_registry()
    )

    report = await validator.execute()

    assert report.workflow_name == "converter basic nodes"
    assert report.node_count == 8
    assert report.valid is True
    assert report.diagnostics == ()


@pytest.mark.asyncio
async def test_validate_reports_unsupported_nodes_as_warnings_by_default() -> None:
    """Показать ограничения адаптеров, не превращая предупреждения в ошибку."""
    validator = WorkflowValidator(
        make_loader("workflow_test.json"), default_node_converter_registry()
    )

    report = await validator.execute()

    assert report.valid is True
    assert any(item.code == "UNSUPPORTED_NODE_TYPE" for item in report.diagnostics)
    assert any(item.node_type == "n8n-nodes-base.code" for item in report.diagnostics)
    assert all(item.severity == "warning" for item in report.diagnostics)
    unsupported = next(
        item for item in report.diagnostics if item.node_type == "n8n-nodes-base.code"
    )
    assert unsupported.node_id is not None
    assert unsupported.node_name is not None
    assert unsupported.context["type_version"] == 2.0


@pytest.mark.asyncio
async def test_validate_strict_turns_unsupported_node_warnings_into_errors() -> None:
    """Проверить, что strict-режим делает каждый warning ошибкой результата."""
    validator = WorkflowValidator(
        make_loader("workflow_test.json"), default_node_converter_registry()
    )

    report = await validator.execute(strict=True)

    assert report.valid is False
    assert report.diagnostics
    assert all(item.severity == "error" for item in report.diagnostics)


@pytest.mark.asyncio
async def test_validate_reports_invalid_supported_node_parameters_as_errors() -> None:
    """Проверить параметры зарегистрированного узла при Application validate."""
    validator = WorkflowValidator(
        make_loader("invalid_node_configuration.json"),
        default_node_converter_registry(),
    )

    report = await validator.execute()

    assert report.valid is False
    assert any(item.code == "INVALID_NODE_CONFIGURATION" for item in report.diagnostics)


@pytest.mark.asyncio
async def test_validate_warns_when_workflow_has_no_supported_trigger() -> None:
    """Добавить предупреждение для графа, который runtime не сможет стартовать."""
    validator = WorkflowValidator(
        make_loader("runtime_no_trigger.json"), default_node_converter_registry()
    )

    report = await validator.execute()

    assert report.valid is True
    assert any(item.code == "NO_SUPPORTED_TRIGGER" for item in report.diagnostics)


@pytest.mark.asyncio
async def test_inspect_returns_node_types_connections_triggers_and_support() -> None:
    """Получить сводку графа без параметров узлов и пользовательских секретов."""
    inspector = WorkflowInspector(
        make_loader("converters_basic.json"), default_node_converter_registry()
    )

    report = await inspector.execute()

    assert report.workflow_name == "converter basic nodes"
    assert report.node_count == 8
    assert report.connection_count == 7
    assert report.trigger_names == ("Manual",)
    assert all(node.supported for node in report.nodes)
    assert all("parameters" not in node.model_dump() for node in report.nodes)


@pytest.mark.asyncio
async def test_inspect_shows_ai_connections_and_unsupported_node_warnings() -> None:
    """Считать main и AI рёбра, сохранив предупреждения о Code/AI-узлах."""
    inspector = WorkflowInspector(
        make_loader("workflow_test.json"), default_node_converter_registry()
    )

    report = await inspector.execute()

    assert report.node_count == 14
    assert report.connection_count == 14
    assert report.trigger_names == ("01 Manual Trigger",)
    code_node = next(node for node in report.nodes if node.node_type == "n8n-nodes-base.code")
    assert code_node.supported is False
    assert any(item.node_name == code_node.name for item in report.diagnostics)


@pytest.mark.asyncio
async def test_application_use_case_propagates_invalid_json_schema_error() -> None:
    """Не превращать ошибку загрузки или domain validation в пустой отчёт."""
    validator = WorkflowValidator(
        make_loader("invalid_workflow.json"), default_node_converter_registry()
    )

    with pytest.raises(ValidationError):
        await validator.execute()
