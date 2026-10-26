"""Структурированные данные для команды inspect и будущего CLI-вывода."""

from pydantic import BaseModel, ConfigDict

from .diagnostic import WorkflowDiagnostic


class WorkflowNodeSummary(BaseModel):
    """Краткое описание одного узла без сериализации его параметров/секретов."""

    model_config = ConfigDict(frozen=True)

    name: str
    node_type: str
    type_version: float
    is_trigger: bool
    connection_count: int
    supported: bool


class WorkflowInspection(BaseModel):
    """Сводка имени, узлов, связей и диагностики workflow."""

    model_config = ConfigDict(frozen=True)

    workflow_name: str
    node_count: int
    connection_count: int
    trigger_names: tuple[str, ...]
    nodes: tuple[WorkflowNodeSummary, ...]
    diagnostics: tuple[WorkflowDiagnostic, ...]
