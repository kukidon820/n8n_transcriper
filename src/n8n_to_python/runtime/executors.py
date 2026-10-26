"""Контракты и реестр асинхронных обработчиков workflow-узлов."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ..domain.models.workflow import WorkflowNode
from .errors import UnsupportedNodeTypeError
from .models import NodeResult, RuntimeInputPort, WorkflowItem


@dataclass(frozen=True, slots=True)
class NodeExecutionContext:
    """Контекст одного вызова узла, доступный его runtime-обработчику."""

    input_port: RuntimeInputPort | None
    invocation_number: int


class NodeExecutor(Protocol):
    """Асинхронная реализация поведения одного типа n8n-узла."""

    is_trigger: bool

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Выполнить узел и вернуть items по его выходам."""
        ...


class NodeExecutorRegistry:
    """Ищет runtime-обработчики по `node.type`."""

    def __init__(self, executors: Mapping[str, NodeExecutor]):
        """Скопировать карту зарегистрированных типов узлов."""
        self._executors = dict(executors)

    def is_trigger(self, node: WorkflowNode) -> bool:
        """Сообщить, помечен ли зарегистрированный обработчик как trigger."""
        executor = self._executors.get(node.type)
        return executor is not None and executor.is_trigger

    def get(self, node: WorkflowNode) -> NodeExecutor:
        """Вернуть обработчик узла или явно сообщить о неподдержанном типе."""
        try:
            return self._executors[node.type]
        except KeyError as error:
            raise UnsupportedNodeTypeError(
                f"Для узла {node.name!r} типа {node.type!r} нет runtime-обработчика."
            ) from error
