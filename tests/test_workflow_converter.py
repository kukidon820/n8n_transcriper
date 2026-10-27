"""Проверки выбора backend и жизненного цикла событий генерации."""

from pathlib import Path

import pytest

from n8n_to_python.application.models.generated_project import GeneratedProject
from n8n_to_python.application.ports.workflow_generator import (
    GenerationBackend,
)
from n8n_to_python.application.services.progress import ProgressEvent, ProgressReporter
from n8n_to_python.application.services.workflow_converter import (
    UnsupportedGenerationBackendError,
    WorkflowConverter,
)
from n8n_to_python.domain.models.workflow import Workflow

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "workflow_test.json"


class FixtureGenerator:
    """Простая реальная реализация контракта для проверки Application-сценария."""

    backend = GenerationBackend.PYTHON_CORE

    def __init__(self, error: Exception | None = None):
        self.workflow: Workflow | None = None
        self.error = error

    async def generate(
        self,
        workflow: Workflow,
        *,
        progress: ProgressReporter,
        project_name: str | None = None,
    ) -> GeneratedProject:
        self.workflow = workflow
        self.project_name = project_name
        if self.error is not None:
            raise self.error
        with progress.stage("render-python-files"):
            return GeneratedProject(files={"main.py": "async def run(): pass\n"})


def load_fixture() -> Workflow:
    """Загрузить настоящий локальный n8n JSON fixture."""
    return Workflow.model_validate_json(FIXTURE_PATH.read_bytes())


@pytest.mark.asyncio
async def test_converter_passes_workflow_to_selected_backend_and_reports_progress() -> None:
    workflow = load_fixture()
    generator = FixtureGenerator()
    events: list[ProgressEvent] = []
    converter = WorkflowConverter(
        {GenerationBackend.PYTHON_CORE: generator},
        progress=ProgressReporter(events.append),
    )

    result = await converter.convert(workflow, GenerationBackend.PYTHON_CORE)

    assert generator.workflow is workflow
    assert result.files == {"main.py": "async def run(): pass\n"}
    assert [(event.stage, event.status) for event in events] == [
        ("conversion:python-core", "started"),
        ("render-python-files", "started"),
        ("render-python-files", "succeeded"),
        ("conversion:python-core", "succeeded"),
    ]


@pytest.mark.asyncio
async def test_converter_propagates_generator_error_and_reports_failed_stage() -> None:
    workflow = load_fixture()
    failure = RuntimeError("generation failed")
    generator = FixtureGenerator(error=failure)
    events: list[ProgressEvent] = []
    converter = WorkflowConverter(
        {GenerationBackend.PYTHON_CORE: generator},
        progress=ProgressReporter(events.append),
    )

    with pytest.raises(RuntimeError, match="generation failed") as raised:
        await converter.convert(workflow, GenerationBackend.PYTHON_CORE)

    assert raised.value is failure
    assert [(event.stage, event.status) for event in events] == [
        ("conversion:python-core", "started"),
        ("conversion:python-core", "failed"),
    ]
    assert events[-1].error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_converter_explains_when_backend_is_not_registered() -> None:
    converter = WorkflowConverter({})

    with pytest.raises(UnsupportedGenerationBackendError, match="langgraph.*Доступны: нет"):
        await converter.convert(load_fixture(), GenerationBackend.LANGGRAPH)


def test_progress_callback_failure_does_not_hide_generation_error() -> None:
    workflow_error = RuntimeError("workflow failed")

    def callback(event: ProgressEvent) -> None:
        if event.status == "failed":
            raise OSError("console unavailable")

    reporter = ProgressReporter(callback)

    with pytest.raises(RuntimeError, match="workflow failed") as raised:
        with reporter.stage("generate"):
            raise workflow_error

    assert raised.value is workflow_error
    assert any("console unavailable" in note for note in raised.value.__notes__)
