"""Use case подготовки безопасной сводки по workflow без его параметров."""

from collections import Counter

from ..models.diagnostic import WorkflowDiagnostic
from ..models.workflow_inspection import WorkflowInspection, WorkflowNodeSummary
from ..ports.node_support import NodeSupportInspector
from .workflow_loader import WorkflowLoader


class WorkflowInspector:
    """Загрузить workflow и вернуть краткое описание графа и поддержки узлов."""

    def __init__(self, loader: WorkflowLoader, node_support: NodeSupportInspector):
        """Сохранить loader и порт диагностики node-конвертеров."""
        self._loader = loader
        self._node_support = node_support

    async def execute(self) -> WorkflowInspection:
        """Собрать узлы, main/AI-связи и диагностику без раскрытия parameters."""
        workflow = await self._loader.load()
        node_diagnostics = {
            node.name: self._node_support.inspect_node(node) for node in workflow.nodes
        }
        diagnostics = tuple(
            diagnostic for node in workflow.nodes for diagnostic in node_diagnostics[node.name]
        )
        triggers = tuple(
            node.name for node in workflow.nodes if self._node_support.is_trigger(node)
        )
        if not triggers:
            diagnostics = (
                *diagnostics,
                WorkflowDiagnostic(
                    severity="warning",
                    code="NO_SUPPORTED_TRIGGER",
                    message="В workflow нет зарегистрированного стартового trigger-узла.",
                ),
            )
        connection_counts: Counter[str] = Counter()
        connection_count = 0
        for source_name, connection_types in workflow.connections.items():
            for outputs in connection_types.values():
                for output in outputs:
                    if output is None:
                        continue
                    for connection in output:
                        connection_counts[source_name] += 1
                        connection_count += 1

        nodes = tuple(
            WorkflowNodeSummary(
                name=node.name,
                node_type=node.type,
                type_version=node.type_version,
                is_trigger=self._node_support.is_trigger(node),
                connection_count=connection_counts[node.name],
                supported=not node_diagnostics[node.name],
            )
            for node in workflow.nodes
        )
        return WorkflowInspection(
            workflow_name=workflow.name,
            node_count=len(workflow.nodes),
            connection_count=connection_count,
            trigger_names=triggers,
            nodes=nodes,
            diagnostics=diagnostics,
        )
