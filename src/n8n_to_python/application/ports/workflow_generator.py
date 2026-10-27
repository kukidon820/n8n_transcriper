"""Контракт генераторов Python-проекта для Application."""

from enum import StrEnum
from typing import Protocol

from ...domain.models.workflow import Workflow
from ..models.generated_project import GeneratedProject
from ..services.progress import ProgressReporter


class GenerationBackend(StrEnum):
    """Доступные стратегии генерации проекта."""

    PYTHON_CORE = "python-core"
    LANGGRAPH = "langgraph"


class WorkflowGenerator(Protocol):
    """Стратегия, которая принимает валидный Workflow и создаёт проект."""

    @property
    def backend(self) -> GenerationBackend:
        """Backend, который реализует этот генератор."""
        ...

    async def generate(
        self,
        workflow: Workflow,
        *,
        progress: ProgressReporter,
        project_name: str | None = None,
    ) -> GeneratedProject:
        """Сгенерировать проект в памяти с необязательным именем пакета."""
        ...
