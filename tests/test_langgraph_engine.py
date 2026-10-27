"""Runtime checks for LangGraph main-flow routing limits."""

import json
from pathlib import Path

import pytest

from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.engine import WorkflowExecutionEngine
from n8n_to_python.runtime.errors import IncompleteMergeError
from n8n_to_python.runtime.langgraph_engine import LangGraphExecutionEngine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_langgraph_engine_matches_python_core_for_an_active_fanout() -> None:
    """Run both active fan-out branches and preserve the shared FIFO order."""
    workflow_data = json.loads((FIXTURES / "converters_basic.json").read_text(encoding="utf-8"))
    workflow_data["connections"]["Manual"]["main"][0].append(
        {"node": "Branch A", "type": "main", "index": 0}
    )
    workflow = Workflow.model_validate(workflow_data)
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    core_result = await WorkflowExecutionEngine(registry).run(workflow)
    langgraph_result = await LangGraphExecutionEngine(registry).run(workflow)

    assert langgraph_result == core_result
    assert [record.node_name for record in langgraph_result.executions].count("Branch A") == 2


@pytest.mark.asyncio
async def test_langgraph_engine_matches_python_core_for_merge_append() -> None:
    """Wait for each Merge input and append its items in input-port order."""
    workflow = Workflow.model_validate_json((FIXTURES / "converters_merge.json").read_bytes())
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    core_result = await WorkflowExecutionEngine(registry).run(workflow)
    langgraph_result = await LangGraphExecutionEngine(registry).run(workflow)

    assert langgraph_result == core_result
    merge_calls = [record for record in langgraph_result.executions if record.node_name == "Merge"]
    assert [record.input_port.index for record in merge_calls] == [0, 1]
    assert merge_calls[0].output_item_counts == (None,)
    assert merge_calls[1].output_item_counts == (2,)
    assert [item.json_data for item in langgraph_result.terminal_outputs[0].items] == [
        {"branch": "first"},
        {"branch": "second"},
    ]


@pytest.mark.asyncio
async def test_langgraph_engine_fails_when_a_merge_input_never_arrives() -> None:
    """Do not report success while a stateful Merge still lacks a configured input."""
    workflow = Workflow.model_validate_json((FIXTURES / "converters_merge.json").read_bytes())
    workflow.connections["Second branch"]["main"][0] = []
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    with pytest.raises(IncompleteMergeError, match=r"missing input ports \[1\]"):
        await LangGraphExecutionEngine(registry).run(workflow)
