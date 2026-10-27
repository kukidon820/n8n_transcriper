"""Integration checks for Convert to File and Extract from File formats."""

import base64

import pytest

from n8n_to_python.converters.file_nodes import (
    ConvertToFileExecutor,
    ConvertToFileParameters,
    ExtractFileParameters,
    ExtractFromFileExecutor,
)
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import WorkflowNode
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.models import WorkflowItem


def _context() -> NodeExecutionContext:
    """Create the real runtime context required by node executors."""
    return NodeExecutionContext(input_port=None, invocation_number=1, state={})


def _node(node_type: str, version: float, parameters: dict[str, object]) -> WorkflowNode:
    """Build a Pydantic-validated workflow node for direct runtime execution."""
    return WorkflowNode(
        id=f"{node_type}-{version}",
        name=node_type.rsplit(".", 1)[-1],
        type=node_type,
        typeVersion=version,
        position=(0, 0),
        parameters=parameters,
    )


def _binary_item(content: bytes, *, file_name: str = "source.bin") -> WorkflowItem:
    """Store real file bytes in n8n's inline base64 representation."""
    return WorkflowItem(
        json={},
        binary={
            "data": {
                "data": base64.b64encode(content).decode("ascii"),
                "fileName": file_name,
                "mimeType": "application/octet-stream",
            }
        },
    )


@pytest.mark.asyncio
async def test_convert_text_and_extract_text_support_n8n_export_parameters() -> None:
    """Round-trip the toText/text operations and BOM options from a workflow export."""
    input_item = WorkflowItem(json={"csv_text": "name,score\nAda,10\n"})
    convert_node = _node(
        "n8n-nodes-base.convertToFile",
        1.1,
        {
            "operation": "toText",
            "sourceProperty": "csv_text",
            "binaryPropertyName": "file",
            "options": {"fileName": "tasks.csv", "encoding": "utf8", "addBOM": True},
        },
    )
    convert = ConvertToFileExecutor(ConvertToFileParameters.model_validate(convert_node.parameters))
    converted = await convert.execute(convert_node, (input_item,), _context())
    file_metadata = converted.outputs[0][0].binary["file"]
    file_bytes = base64.b64decode(file_metadata["data"])

    assert file_metadata["fileName"] == "tasks.csv"
    assert file_bytes == b"\xef\xbb\xbfname,score\nAda,10\n"

    extract_node = _node(
        "n8n-nodes-base.extractFromFile",
        1,
        {
            "operation": "text",
            "binaryPropertyName": "file",
            "destinationKey": "csv_text",
            "options": {"encoding": "utf8", "stripBOM": True, "keepSource": "both"},
        },
    )
    extract = ExtractFromFileExecutor(ExtractFileParameters.model_validate(extract_node.parameters))
    result = await extract.execute(extract_node, converted.outputs[0], _context())

    assert result.outputs[0][0].json_data == {"csv_text": "name,score\nAda,10\n"}
    assert result.outputs[0][0].binary["file"]["fileName"] == "tasks.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "compression"),
    [("csv", False), ("html", False), ("xlsx", False), ("xlsx", True), ("xls", False)],
)
async def test_convert_and_extract_round_trip_real_tabular_files(
    operation: str, compression: bool
) -> None:
    """Create actual CSV, HTML, XLSX, and XLS files and parse them back to row items."""
    input_items = (
        WorkflowItem(json={"name": "Ada", "score": 10}),
        WorkflowItem(json={"name": "Lin", "score": 20}),
    )
    convert_node = _node(
        "n8n-nodes-base.convertToFile",
        1.2,
        {
            "operation": operation,
            "binaryPropertyName": "file",
            "options": {
                "fileName": f"people.{operation}",
                "headerRow": True,
                "delimiter": ";" if operation == "csv" else ",",
                "compression": compression,
            },
        },
    )
    convert = ConvertToFileExecutor(ConvertToFileParameters.model_validate(convert_node.parameters))
    converted = await convert.execute(convert_node, input_items, _context())
    file_metadata = converted.outputs[0][0].binary["file"]
    file_bytes = base64.b64decode(file_metadata["data"])

    assert file_metadata["fileName"] == f"people.{operation}"
    assert file_bytes

    extract_node = _node(
        "n8n-nodes-base.extractFromFile",
        1.2,
        {
            "operation": operation,
            "binaryPropertyName": "file",
            "destinationKey": "row",
            "options": {
                "headerRow": True,
                "keepSource": False,
                "delimiter": ";" if operation == "csv" else ",",
            },
        },
    )
    extract = ExtractFromFileExecutor(ExtractFileParameters.model_validate(extract_node.parameters))
    result = await extract.execute(extract_node, converted.outputs[0], _context())

    expected_score: object = "10" if operation in {"csv", "html"} else 10
    second_score: object = "20" if operation in {"csv", "html"} else 20
    assert [item.json_data for item in result.outputs[0]] == [
        {"row": {"name": "Ada", "score": expected_score}},
        {"row": {"name": "Lin", "score": second_score}},
    ]


@pytest.mark.asyncio
async def test_extract_html_text_and_base64_from_binary_items() -> None:
    """Parse the first HTML table and handle n8n text/base64 extract operations."""
    node = _node("n8n-nodes-base.extractFromFile", 1, {})
    cases = (
        (
            "html",
            b"<table><tr><th>name</th><th>score</th></tr><tr><td>Ada</td><td>10</td></tr></table>",
            "row",
            {"name": "Ada", "score": "10"},
        ),
        ("text", b"hello from file", "text", "hello from file"),
        (
            "binaryToPropery",
            b"hello from file",
            "file_base64",
            base64.b64encode(b"hello from file").decode("ascii"),
        ),
    )
    for operation, content, destination_key, expected in cases:
        node.parameters = {
            "operation": operation,
            "binaryPropertyName": "data",
            "destinationKey": destination_key,
            "options": {"keepSource": False},
        }
        extract = ExtractFromFileExecutor(ExtractFileParameters.model_validate(node.parameters))
        result = await extract.execute(node, (_binary_item(content),), _context())
        assert result.outputs[0][0].json_data[destination_key] == expected


def test_file_node_registry_accepts_each_explicitly_supported_type_version() -> None:
    """Validate supported node versions by the registry's exact type/version lookup."""
    registry = default_node_converter_registry()
    for node_type, operation in (
        ("n8n-nodes-base.convertToFile", "toText"),
        ("n8n-nodes-base.extractFromFile", "text"),
    ):
        for version in (1, 1.1, 1.2):
            extra = {"sourceProperty": "text"} if operation == "toText" else {}
            node = _node(node_type, version, {"operation": operation, **extra})
            assert registry.inspect_node(node) == ()
