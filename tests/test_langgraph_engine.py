"""Runtime checks for LangGraph main-flow routing limits."""

import json
from pathlib import Path

import pytest

from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.langgraph_engine import (
    LangGraphExecutionEngine,
    UnsupportedLangGraphWorkflowError,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_langgraph_engine_fails_instead_of_dropping_an_active_fanout_branch() -> None:
    """Reject a real n8n branch fixture that activates two outgoing main edges."""
    workflow_data = json.loads((FIXTURES / "converters_basic.json").read_text(encoding="utf-8"))
    workflow_data["connections"]["Manual"]["main"][0].append(
        {"node": "Branch A", "type": "main", "index": 0}
    )
    workflow = Workflow.model_validate(workflow_data)
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    with pytest.raises(UnsupportedLangGraphWorkflowError, match="fan-out"):
        await LangGraphExecutionEngine(registry).run(workflow)
