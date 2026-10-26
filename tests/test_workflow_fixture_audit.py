"""Read-only model/support audit for every workflow JSON currently in the workspace."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from n8n_to_python.application.models.diagnostic import WorkflowDiagnostic
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow

PROJECT_ROOT = Path(__file__).parents[1]
WORKFLOW_JSON_FILES = tuple(
    sorted(
        [
            *PROJECT_ROOT.glob("*.json"),
            *(PROJECT_ROOT / "tests" / "fixtures").glob("*.json"),
        ]
    )
)
INTENTIONALLY_INVALID_FILES = {"invalid_workflow.json"}


@pytest.mark.parametrize("path", WORKFLOW_JSON_FILES, ids=lambda path: path.name)
def test_every_workspace_workflow_json_is_validated_and_inspected(path: Path) -> None:
    """Validate all exports and ensure unsupported nodes receive explicit diagnostics."""
    if path.name in INTENTIONALLY_INVALID_FILES:
        with pytest.raises(ValidationError):
            Workflow.model_validate_json(path.read_bytes())
        return

    workflow = Workflow.model_validate_json(path.read_bytes())
    registry = default_node_converter_registry()
    for node in workflow.nodes:
        diagnostics = registry.inspect_node(node)
        assert isinstance(diagnostics, tuple)

        for diagnostic in diagnostics:
            assert isinstance(diagnostic, WorkflowDiagnostic)
            assert diagnostic.node_name == node.name
            assert diagnostic.node_type == node.type
            assert diagnostic.code.strip()
            assert diagnostic.message.strip()
