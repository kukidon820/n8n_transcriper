"""Контракты и реестр асинхронных обработчиков workflow-узлов."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.models.workflow import WorkflowNode
from .errors import UnsupportedNodeTypeError
from .models import NodeResult, RuntimeInputPort, WorkflowItem

NodeOutputBatch = tuple[tuple[WorkflowItem, ...] | None, ...]


@dataclass(frozen=True, slots=True)
class NodeExecutionContext:
    """Контекст одного вызова узла, доступный его runtime-обработчику."""

    input_port: RuntimeInputPort | None
    invocation_number: int
    state: dict[str, Any]
    services: Mapping[str, Any] = field(default_factory=dict)
    input_items: tuple[WorkflowItem, ...] = ()
    node_outputs: Mapping[str, tuple[WorkflowItem, ...]] = field(default_factory=dict)
    node_output_runs: Mapping[str, tuple[NodeOutputBatch, ...]] = field(default_factory=dict)


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

    def __init__(
        self,
        executors: Mapping[str | tuple[str, float], NodeExecutor],
        *,
        node_executors: Mapping[str, NodeExecutor] | None = None,
    ):
        """Скопировать обработчики по типу/версии и, при необходимости, имени узла."""
        self._executors = dict(executors)
        self._node_executors = dict(node_executors or {})

    def is_trigger(self, node: WorkflowNode) -> bool:
        """Сообщить, помечен ли зарегистрированный обработчик как trigger."""
        executor = self._find(node)
        return executor is not None and executor.is_trigger

    def get(self, node: WorkflowNode) -> NodeExecutor:
        """Вернуть обработчик узла или явно сообщить о неподдержанном типе."""
        executor = self._find(node)
        if executor is not None:
            return executor
        raise UnsupportedNodeTypeError(
            f"Для узла {node.name!r} типа {node.type!r} версии "
            f"{node.type_version!r} нет runtime-обработчика."
        )

    def _find(self, node: WorkflowNode) -> NodeExecutor | None:
        """Сначала искать точную регистрацию узла, затем версионную или общую."""
        node_executor = self._node_executors.get(node.name)
        if node_executor is not None:
            return node_executor

        versioned = self._executors.get((node.type, node.type_version))
        if versioned is not None:
            return versioned

        try:
            return self._executors[node.type]
        except KeyError:
            return None
