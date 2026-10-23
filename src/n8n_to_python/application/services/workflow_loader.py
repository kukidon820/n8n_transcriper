"""Сценарии Application для загрузки и разбора workflow."""

from ...domain.models.workflow import Workflow
from ..ports.workflow_source import WorkflowSource


class WorkflowLoader:
    """Получает байты из источника и проверяет их как workflow n8n."""

    def __init__(self, source: WorkflowSource):
        """Сохраняет источник, реализующий контракт `WorkflowSource`."""
        self.source = source

    async def load(self) -> Workflow:
        """Загрузить JSON и вернуть Pydantic-модель `Workflow`.

        Ошибки чтения, разбора JSON и валидации намеренно передаются
        вызывающему коду, чтобы он мог показать пользователю причину сбоя.
        """
        raw_bytes = await self.source.read()
        return Workflow.model_validate_json(raw_bytes)
