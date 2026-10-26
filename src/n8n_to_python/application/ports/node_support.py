"""Порт для проверки готовности узлов к исполнению или конвертации."""

from typing import Protocol

from ...domain.models.workflow import WorkflowNode
from ..models.diagnostic import WorkflowDiagnostic


class NodeSupportInspector(Protocol):
    """Описывает диагностику поддержки одного узла без привязки к реализации."""

    def inspect_node(self, node: WorkflowNode) -> tuple[WorkflowDiagnostic, ...]:
        """Вернуть ошибки параметров и предупреждение для неподдержанного узла."""
        ...

    def is_trigger(self, node: WorkflowNode) -> bool:
        """Сообщить, зарегистрирован ли узел как стартовый trigger."""
        ...
