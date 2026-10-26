"""Модели данных для входов и результатов выполнения графа."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowItem(BaseModel):
    """Один item n8n; пользовательские поля item сохраняются."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    json_data: dict[str, Any] = Field(alias="json")
    binary: dict[str, Any] | None = None


class RuntimeInputPort(BaseModel):
    """Входной порт, через который данные поступили в узел."""

    type: str
    index: int = Field(ge=0, strict=True)


OutputItems = tuple[WorkflowItem, ...] | None


class NodeResult(BaseModel):
    """Пакеты items по выходам узла; позиция в tuple — индекс выхода `main`."""

    outputs: tuple[OutputItems, ...] = ()


class NodeExecutionRecord(BaseModel):
    """Краткая запись одного вызова узла для результата запуска."""

    node_name: str
    input_port: RuntimeInputPort | None
    input_items: tuple[WorkflowItem, ...]
    output_item_counts: tuple[int | None, ...]


class TerminalOutput(BaseModel):
    """Items с выхода, к которому в графе не подключён следующий узел."""

    node_name: str
    output_index: int
    items: tuple[WorkflowItem, ...]


class ExecutionResult(BaseModel):
    """Записи вызовов узлов и конечные выходы успешного запуска."""

    executions: tuple[NodeExecutionRecord, ...]
    terminal_outputs: tuple[TerminalOutput, ...]
