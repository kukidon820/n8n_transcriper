"""Сценарий Application, выбирающий backend и запускающий генерацию."""

from collections.abc import Mapping

from ...domain.models.workflow import Workflow
from ..models.generated_project import GeneratedProject
from ..ports.workflow_generator import GenerationBackend, WorkflowGenerator
from .progress import ProgressReporter


class UnsupportedGenerationBackendError(ValueError):
    """Выбранный backend пока не зарегистрирован в приложении."""


class WorkflowConverter:
    """Передаёт валидный workflow выбранной стратегии генерации."""

    def __init__(
        self,
        generators: Mapping[GenerationBackend, WorkflowGenerator],
        progress: ProgressReporter | None = None,
    ):
        """Сохраняет доступные backend-стратегии и канал событий прогресса."""
        self._generators = dict(generators)
        self._progress = progress or ProgressReporter()

        for backend, generator in self._generators.items():
            if generator.backend != backend:
                raise ValueError(
                    f"Ключ backend {backend.value!r} не совпадает с генератором "
                    f"{generator.backend.value!r}"
                )

    async def convert(
        self,
        workflow: Workflow,
        backend: GenerationBackend,
        *,
        project_name: str | None = None,
    ) -> GeneratedProject:
        """Сгенерировать проект выбранным backend для уже валидного workflow."""
        try:
            generator = self._generators[backend]
        except KeyError as error:
            available = ", ".join(sorted(item.value for item in self._generators)) or "нет"
            raise UnsupportedGenerationBackendError(
                f"Backend {backend.value!r} не настроен. Доступны: {available}."
            ) from error

        with self._progress.stage(f"conversion:{backend.value}"):
            return await generator.generate(
                workflow,
                progress=self._progress,
                project_name=project_name,
            )
