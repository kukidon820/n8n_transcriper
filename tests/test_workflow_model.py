"""Проверки Pydantic-моделей на реальном fixture workflow n8n."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from n8n_to_python.domain.models.workflow import Workflow, WorkflowConnection

WORKFLOW_FIXTURE = Path(__file__).parent / "fixtures" / "workflow_test.json"
SANITIZED_EXPORT_FIXTURE = Path(__file__).parent / "fixtures" / "workflow_export_sanitized.json"


def read_workflow_fixture() -> dict[str, Any]:
    """Прочитать fixture в словарь для сценариев с изменённой копией JSON."""
    return json.loads(WORKFLOW_FIXTURE.read_text(encoding="utf-8"))


def test_model_parses_main_and_ai_connection_ports() -> None:
    """Распознать обычные и AI-связи как типизированные рёбра."""
    workflow = Workflow.model_validate_json(WORKFLOW_FIXTURE.read_bytes())
    first_node = workflow.nodes[0]

    assert first_node.type_version == 1
    assert isinstance(first_node.position, tuple)
    assert all(isinstance(node.parameters, dict) for node in workflow.nodes)

    port_types = {
        port_type
        for connection_types in workflow.connections.values()
        for port_type in connection_types
    }
    assert port_types == {"main", "ai_languageModel", "ai_tool", "ai_memory"}

    edges = [
        edge
        for connection_types in workflow.connections.values()
        for outputs in connection_types.values()
        for output in outputs
        for edge in output
    ]
    assert len(edges) == 14
    assert all(isinstance(edge, WorkflowConnection) for edge in edges)
    assert all(isinstance(edge.index, int) for edge in edges)

    if_node = next(node for node in workflow.nodes if node.type == "n8n-nodes-base.if")
    switch_node = next(node for node in workflow.nodes if node.type == "n8n-nodes-base.switch")
    assert len(workflow.connections[if_node.name]["main"]) == 2
    assert len(workflow.connections[switch_node.name]["main"]) == 3


def test_model_parses_sanitized_full_workflow_export() -> None:
    """Validate a scrubbed 54-node export with its original graph shape intact."""
    workflow = Workflow.model_validate_json(SANITIZED_EXPORT_FIXTURE.read_bytes())

    assert len(workflow.nodes) == 54
    assert len(workflow.connections) == 47
    assert any(
        port.startswith("ai_")
        for connection_types in workflow.connections.values()
        for port in connection_types
    )


def test_model_preserves_unknown_workflow_node_and_connection_fields() -> None:
    """Не терять дополнительные поля на уровнях workflow, узла и ребра."""
    payload = read_workflow_fixture()
    payload["futureWorkflowField"] = "kept"
    payload["nodes"][0]["futureNodeField"] = "kept"

    source_name, connection_types = next(
        (name, ports) for name, ports in payload["connections"].items() if ports
    )
    port_type = next(iter(connection_types))
    payload["connections"][source_name][port_type][0][0]["futureEdgeField"] = "kept"

    workflow = Workflow.model_validate_json(json.dumps(payload))
    parsed_edge = workflow.connections[source_name][port_type][0][0]

    assert workflow.model_extra["futureWorkflowField"] == "kept"
    assert workflow.nodes[0].model_extra["futureNodeField"] == "kept"
    assert parsed_edge.model_extra["futureEdgeField"] == "kept"


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("workflow", "name"),
        ("workflow", "nodes"),
        ("workflow", "connections"),
        ("node", "name"),
        ("node", "type"),
        ("node", "typeVersion"),
        ("node", "position"),
    ],
)
def test_model_rejects_missing_required_fields(section: str, field: str) -> None:
    """Отклонить fixture, если убрать обязательное поле workflow или узла."""
    payload = read_workflow_fixture()
    target = payload if section == "workflow" else payload["nodes"][0]
    target.pop(field)

    with pytest.raises(ValidationError):
        Workflow.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("workflow", "name"),
        ("workflow", "nodes"),
        ("workflow", "connections"),
        ("node", "name"),
        ("node", "type"),
        ("node", "typeVersion"),
        ("node", "position"),
        ("node", "parameters"),
    ],
)
def test_model_rejects_null_for_non_nullable_fields(section: str, field: str) -> None:
    """Отклонить null для полей с обязательным значением и конкретным типом."""
    payload = read_workflow_fixture()
    target = payload if section == "workflow" else payload["nodes"][0]
    target[field] = None

    with pytest.raises(ValidationError):
        Workflow.model_validate_json(json.dumps(payload))


def test_model_defaults_missing_node_parameters_to_empty_dictionary() -> None:
    """Подставить пустые параметры, когда поле отсутствует у узла."""
    payload = read_workflow_fixture()
    payload["nodes"][0].pop("parameters")

    workflow = Workflow.model_validate_json(json.dumps(payload))

    assert workflow.nodes[0].parameters == {}


@pytest.mark.parametrize(
    "invalid_edge",
    [
        "wrong_output_shape",
        "missing_node",
        "invalid_index",
        "negative_index",
        "missing_index",
    ],
)
def test_model_rejects_invalid_connection_entries(invalid_edge: str) -> None:
    """Отклонить некорректную вложенность или неполное ребро связи."""
    payload = read_workflow_fixture()
    source_name, connection_types = next(
        (name, ports) for name, ports in payload["connections"].items() if ports
    )
    port_type = next(iter(connection_types))

    if invalid_edge == "wrong_output_shape":
        payload["connections"][source_name][port_type] = ["not a list of edges"]
    elif invalid_edge == "invalid_index":
        payload["connections"][source_name][port_type][0][0]["index"] = "not an integer"
    elif invalid_edge == "negative_index":
        payload["connections"][source_name][port_type][0][0]["index"] = -1
    elif invalid_edge == "missing_index":
        payload["connections"][source_name][port_type][0][0].pop("index")
    else:
        payload["connections"][source_name][port_type][0][0].pop("node")

    with pytest.raises(ValidationError):
        Workflow.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("broken_reference", ["unknown_source", "unknown_target"])
def test_model_rejects_references_to_unknown_nodes(broken_reference: str) -> None:
    """Отклонить связь, если исходный или целевой узел отсутствует в графе."""
    payload = read_workflow_fixture()

    if broken_reference == "unknown_source":
        payload["connections"]["Unknown source"] = {"main": [[]]}
    else:
        source_name, connection_types = next(
            (name, ports) for name, ports in payload["connections"].items() if ports
        )
        port_type = next(iter(connection_types))
        payload["connections"][source_name][port_type][0][0]["node"] = "Unknown target"

    with pytest.raises(ValidationError):
        Workflow.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("duplicate_field", ["name", "id"])
def test_model_rejects_duplicate_node_names_and_ids(duplicate_field: str) -> None:
    """Отклонить повтор имени или ID, которые делают узел неоднозначным."""
    payload = read_workflow_fixture()
    payload["nodes"][1][duplicate_field] = payload["nodes"][0][duplicate_field]

    with pytest.raises(ValidationError):
        Workflow.model_validate_json(json.dumps(payload))


def test_model_accepts_unconnected_output_slot() -> None:
    """Сохранить null-слот выхода, который n8n использует для пустой ветки."""
    payload = read_workflow_fixture()
    source_name, connection_types = next(
        (name, ports) for name, ports in payload["connections"].items() if ports
    )
    port_type = next(iter(connection_types))
    payload["connections"][source_name][port_type][0] = None

    workflow = Workflow.model_validate_json(json.dumps(payload))

    assert workflow.connections[source_name][port_type][0] is None


def test_model_accepts_unknown_connection_port_type() -> None:
    """Принимать новый тип порта, если форма его соединения корректна."""
    payload = read_workflow_fixture()
    source_name, connection_types = next(
        (name, ports) for name, ports in payload["connections"].items() if ports
    )
    existing_port = next(iter(connection_types))
    target_name = payload["connections"][source_name][existing_port][0][0]["node"]
    payload["connections"][source_name]["future_connection_type"] = [
        [{"node": target_name, "type": "future_connection_type", "index": 0}]
    ]

    workflow = Workflow.model_validate_json(json.dumps(payload))

    assert "future_connection_type" in workflow.connections[source_name]


def test_model_allows_cycles_in_the_workflow_graph() -> None:
    """Не запрещать цикл: циклы могут быть корректной логикой workflow n8n."""
    payload = read_workflow_fixture()
    first_node = payload["nodes"][0]["name"]
    second_node = payload["nodes"][1]["name"]
    payload["connections"][second_node]["main"][0].append(
        {"node": first_node, "type": "main", "index": 0}
    )

    workflow = Workflow.model_validate_json(json.dumps(payload))

    assert workflow.connections[second_node]["main"][0][-1].node == first_node
