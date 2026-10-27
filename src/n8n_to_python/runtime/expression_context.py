"""Подготовка runtime-значений глобальных n8n expressions."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from ..domain.models.workflow import Workflow, WorkflowNode
from .models import WorkflowItem


def build_expression_services(
    workflow: Workflow,
    services: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Добавить workflow/execution metadata к пользовательским runtime services."""
    result = dict(services or {})
    expression_context = dict(result.get("expression_context", {}) or {})
    execution = dict(result.get("execution", {}) or {})
    workflow_context = dict(result.get("workflow", {}) or {})
    nodes = {
        node.name: {
            "id": node.id,
            "name": node.name,
            "type": node.type,
            "type_version": node.type_version,
        }
        for node in workflow.nodes
    }

    execution_id = str(execution.get("id") or result.get("execution_id") or uuid4().hex)
    execution.setdefault("id", execution_id)
    execution.setdefault("mode", result.get("execution_mode", "manual"))
    workflow_context.setdefault("id", getattr(workflow, "id", None))
    workflow_context.setdefault("name", workflow.name)
    workflow_context.setdefault("active", getattr(workflow, "active", None))

    expression_context.setdefault("execution", execution)
    expression_context.setdefault("workflow", workflow_context)
    expression_context.setdefault("vars", result.get("vars", {}))
    expression_context.setdefault("env", result.get("env", {}))
    result.update(
        {
            "execution_id": execution_id,
            "execution": execution,
            "workflow": workflow_context,
            "vars": expression_context["vars"],
            "env": expression_context["env"],
            "node_metadata": nodes,
            "expression_context": expression_context,
        }
    )
    return result


def expression_services_for_node(
    base_services: Mapping[str, Any],
    node: WorkflowNode,
    input_items: Sequence[WorkflowItem],
) -> dict[str, Any]:
    """Установить `$nodeVersion` и `$prevNode` для текущего вызова узла."""
    result = dict(base_services)
    metadata = result.get("node_metadata", {})
    result["current_node"] = metadata.get(
        node.name,
        {"id": node.id, "name": node.name, "type": node.type, "type_version": node.type_version},
    )
    previous_name = next(
        (
            name
            for source_item in reversed(tuple(input_items))
            for name in reversed(tuple(source_item.lineage))
        ),
        None,
    )
    result["previous_node"] = metadata.get(previous_name, {"name": previous_name})
    return result
