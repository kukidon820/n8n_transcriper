"""Проверки передачи байтов источника в Pydantic-модель workflow."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from n8n_to_python.application.services.workflow_loader import WorkflowLoader
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.infrastructure.sources.local_file import LocalFileWorkflowSource

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_loader_reads_local_file_and_returns_validated_workflow() -> None:
    """Загрузить локальную фикстуру и получить заполненный объект Workflow."""
    source = LocalFileWorkflowSource(path=FIXTURES_DIRECTORY / "workflow.json")

    workflow = await WorkflowLoader(source).load()

    assert isinstance(workflow, Workflow)
    assert workflow.name == "Example workflow"
    assert len(workflow.nodes) == 1


@pytest.mark.asyncio
async def test_loader_reads_workflow_with_main_and_ai_connections() -> None:
    """Загрузить расширенный fixture с ветвлениями, AI-моделью и tools."""
    source = LocalFileWorkflowSource(path=FIXTURES_DIRECTORY / "workflow_test.json")

    workflow = await WorkflowLoader(source).load()

    assert len(workflow.nodes) == 14
    assert set(workflow.model_extra) >= {"active", "pinData", "settings", "tags"}

    agent = next(node for node in workflow.nodes if node.type.endswith(".agent"))
    incoming_types = {
        edge.type
        for connection_types in workflow.connections.values()
        for outputs in connection_types.values()
        for output in outputs
        for edge in output
        if edge.node == agent.name
    }
    assert incoming_types == {"main", "ai_languageModel", "ai_tool", "ai_memory"}


@pytest.mark.asyncio
async def test_loader_rejects_json_with_invalid_workflow_shape() -> None:
    """Убедиться, что форма JSON проверяется Pydantic-моделью."""
    source = LocalFileWorkflowSource(path=FIXTURES_DIRECTORY / "invalid_workflow.json")

    with pytest.raises(ValidationError):
        await WorkflowLoader(source).load()


@pytest.mark.asyncio
async def test_loader_propagates_missing_file_error() -> None:
    """Убедиться, что ошибка источника доходит до вызывающего кода."""
    source = LocalFileWorkflowSource(
        path=Path("missing.json"),
        search_dir=FIXTURES_DIRECTORY,
    )

    with pytest.raises(FileNotFoundError):
        await WorkflowLoader(source).load()
