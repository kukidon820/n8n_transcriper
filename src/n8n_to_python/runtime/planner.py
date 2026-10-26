"""Построение исполняемого плана из валидированной модели Workflow."""

from collections import defaultdict
from dataclasses import dataclass

from ..domain.models.workflow import Workflow, WorkflowNode
from .errors import NoTriggerNodesError
from .executors import NodeExecutorRegistry
from .models import RuntimeInputPort


@dataclass(frozen=True, slots=True)
class PlannedConnection:
    """Переход по выходу `main` к целевому входному порту."""

    target_node: str
    target_input: RuntimeInputPort


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Backend-независимые узлы, триггеры и переходы основного потока."""

    workflow: Workflow
    nodes_by_name: dict[str, WorkflowNode]
    trigger_names: tuple[str, ...]
    output_slot_counts: dict[str, int]
    outgoing: dict[tuple[str, int], tuple[PlannedConnection, ...]]


class ExecutionPlanner:
    """Преобразует `connections.main` в таблицу переходов runtime."""

    def build(self, workflow: Workflow, registry: NodeExecutorRegistry) -> ExecutionPlan:
        """Построить план или отказаться от workflow без доступного trigger."""
        nodes_by_name = {node.name: node for node in workflow.nodes}
        trigger_names = tuple(node.name for node in workflow.nodes if registry.is_trigger(node))
        if not trigger_names:
            raise NoTriggerNodesError(
                "Workflow не содержит узла с зарегистрированным trigger-обработчиком."
            )

        output_slot_counts: dict[str, int] = {}
        edges: dict[tuple[str, int], list[PlannedConnection]] = defaultdict(list)

        for source_name, connection_types in workflow.connections.items():
            main_outputs = connection_types.get("main")
            if main_outputs is None:
                continue

            output_slot_counts[source_name] = len(main_outputs)
            for output_index, output_connections in enumerate(main_outputs):
                if output_connections is None:
                    continue
                for connection in output_connections:
                    edges[(source_name, output_index)].append(
                        PlannedConnection(
                            target_node=connection.node,
                            target_input=RuntimeInputPort(
                                type=connection.type,
                                index=connection.index,
                            ),
                        )
                    )

        return ExecutionPlan(
            workflow=workflow,
            nodes_by_name=nodes_by_name,
            trigger_names=trigger_names,
            output_slot_counts=output_slot_counts,
            outgoing={key: tuple(value) for key, value in edges.items()},
        )
