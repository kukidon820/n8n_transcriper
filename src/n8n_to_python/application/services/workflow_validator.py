"""Use case проверки загруженного n8n workflow и его поддерживаемости."""

from ..models.diagnostic import ValidationReport, WorkflowDiagnostic
from ..ports.node_support import NodeSupportInspector
from .workflow_loader import WorkflowLoader


class WorkflowValidator:
    """Загрузить workflow, собрать диагностику узлов и применить strict-режим."""

    def __init__(self, loader: WorkflowLoader, node_support: NodeSupportInspector):
        """Сохранить loader и порт, сообщающий поддержку конкретных узлов."""
        self._loader = loader
        self._node_support = node_support

    async def execute(self, *, strict: bool = False) -> ValidationReport:
        """Вернуть отчёт; ошибки чтения и Pydantic-валидации пробрасываются."""
        workflow = await self._loader.load()
        diagnostics = [
            diagnostic
            for node in workflow.nodes
            for diagnostic in self._node_support.inspect_node(node)
        ]
        if not any(self._node_support.is_trigger(node) for node in workflow.nodes):
            diagnostics.append(
                WorkflowDiagnostic(
                    severity="warning",
                    code="NO_SUPPORTED_TRIGGER",
                    message="В workflow нет зарегистрированного стартового trigger-узла.",
                )
            )
        if strict:
            diagnostics = [
                diagnostic.model_copy(update={"severity": "error"})
                if diagnostic.severity == "warning"
                else diagnostic
                for diagnostic in diagnostics
            ]
        return ValidationReport(
            workflow_name=workflow.name,
            node_count=len(workflow.nodes),
            valid=not any(item.severity == "error" for item in diagnostics),
            diagnostics=tuple(diagnostics),
        )
