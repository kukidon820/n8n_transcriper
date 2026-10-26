"""Интеграционные проверки Code, Webhook, файловых, DB и Email узлов."""

import asyncio
import base64
import json
from email import message_from_bytes
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy.ext.asyncio import create_async_engine

from n8n_to_python.converters.errors import UnsupportedNodeConfigurationError
from n8n_to_python.converters.file_nodes import (
    CompressionExecutor,
    CompressionParameters,
)
from n8n_to_python.converters.integrations import DataTableParameters
from n8n_to_python.converters.nodes import (
    default_node_converter_registry,
    render_webhook_entrypoint,
)
from n8n_to_python.domain.models.workflow import Workflow, WorkflowNode
from n8n_to_python.infrastructure.email_sender import create_email_sender_from_env
from n8n_to_python.infrastructure.postgres_client import SQLAlchemyPostgresClient
from n8n_to_python.infrastructure.stores.sqlalchemy_data_table_store import (
    SQLAlchemyDataTableStore,
)
from n8n_to_python.runtime.engine import WorkflowExecutionEngine
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


def load_workflow(name: str) -> Workflow:
    """Load a local n8n-shaped fixture through the production Pydantic schema."""
    return Workflow.model_validate_json((FIXTURES / name).read_bytes())


def binary_item(content: bytes, *, name: str = "input.bin") -> WorkflowItem:
    """Build an n8n-style inline binary item from real encoded bytes."""
    return WorkflowItem(
        json={},
        binary={
            "data": {
                "data": base64.b64encode(content).decode("ascii"),
                "fileName": name,
                "mimeType": "application/octet-stream",
            }
        },
    )


@pytest.mark.asyncio
async def test_python_native_code_runs_in_the_graph() -> None:
    """Run a Python Code node using n8n's _items variable and return shape."""
    workflow = load_workflow("converters_python_code.json")

    result = await WorkflowExecutionEngine(
        default_node_converter_registry().build_runtime_registry(workflow)
    ).run(workflow)

    assert result.terminal_outputs[0].items[0].json_data == {"answer": 42}


@pytest.mark.asyncio
async def test_python_native_code_runs_once_for_each_item() -> None:
    """Evaluate Python Code once per item and expose the current `_item`."""
    workflow = load_workflow("converters_python_code.json")
    workflow.connections["Manual"]["main"][0][0].node = "Python Code"
    code_node = next(node for node in workflow.nodes if node.name == "Python Code")
    code_node.parameters.update(
        {
            "mode": "runOnceForEachItem",
            "pythonCode": 'return {"json": {"answer": _item["json"]["value"] + 1}}',
        }
    )
    initial_items = [WorkflowItem(json={"value": 41}), WorkflowItem(json={"value": 42})]
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    result = await WorkflowExecutionEngine(registry).run(
        workflow,
        initial_items={"Manual": initial_items},
    )

    assert [item.json_data for item in result.terminal_outputs[0].items] == [
        {"answer": 42},
        {"answer": 43},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("type_version", [1, 1.1, 2, 2.1, 2.2])
async def test_webhook_payload_starts_only_the_selected_trigger(type_version: float) -> None:
    """Pass a real payload to a callable Webhook trigger without starting a server."""
    workflow = load_workflow("converters_files.json")
    workflow.nodes[0].type_version = type_version
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    payload = json.dumps({"hello": "webhook"}).encode()

    result = await WorkflowExecutionEngine(registry).run(
        workflow,
        initial_items={"Incoming": [binary_item(payload, name="records.json")]},
    )

    assert result.terminal_outputs[0].items[0].json_data == {"parsed": {"hello": "webhook"}}
    entrypoint = render_webhook_entrypoint(workflow.nodes[0])
    compile(entrypoint, "<generated-webhook>", "exec")
    assert "async def invoke_incoming" in entrypoint


@pytest.mark.asyncio
async def test_gzip_compression_round_trips_real_binary_data() -> None:
    """Compress and decompress bytes with the stdlib gzip implementation."""
    node = WorkflowNode(
        id="gzip",
        name="GZIP",
        type="n8n-nodes-base.compression",
        typeVersion=1.1,
        position=(0, 0),
        parameters={
            "operation": "compress",
            "binaryPropertyName": "data",
            "outputFormat": "gzip",
            "fileName": "note.txt",
            "binaryPropertyOutput": "compressed",
        },
    )
    original = b"async workflows keep the bytes intact"
    context = NodeExecutionContext(input_port=None, invocation_number=1, state={})
    compressed = await CompressionExecutor(
        CompressionParameters.model_validate(node.parameters)
    ).execute(node, (binary_item(original),), context)
    compressed_value = compressed.outputs[0][0].binary["compressed"]["data"]

    node.parameters = {
        "operation": "decompress",
        "binaryPropertyName": "compressed",
        "outputFormat": "gzip",
        "fileName": "note.txt.gz",
        "outputPrefix": "expanded",
    }
    unpacked = await CompressionExecutor(
        CompressionParameters.model_validate(node.parameters)
    ).execute(
        node,
        (WorkflowItem(json={}, binary={"compressed": {"data": compressed_value}}),),
        context,
    )
    expanded_value = unpacked.outputs[0][0].binary["expanded"]["data"]

    assert base64.b64decode(expanded_value) == original


@pytest.mark.asyncio
async def test_extract_csv_and_pdf_from_real_binary_documents() -> None:
    """Run CSV and PDF extraction through the real Webhook→ZIP→Extract graph."""
    workflow = load_workflow("converters_files.json")
    extract = next(node for node in workflow.nodes if node.name == "Extract")
    extract.parameters["operation"] = "csv"
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    csv_result = await WorkflowExecutionEngine(registry).run(
        workflow,
        initial_items={
            "Incoming": [binary_item(b"name,score\nAda,10\nLin,20\n", name="scores.csv")]
        },
    )
    assert [item.json_data for item in csv_result.terminal_outputs[0].items] == [
        {"parsed": {"name": "Ada", "score": "10"}},
        {"parsed": {"name": "Lin", "score": "20"}},
    ]

    pdf_bytes = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf_bytes)
    extract.parameters["operation"] = "pdf"
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    pdf_result = await WorkflowExecutionEngine(registry).run(
        workflow,
        initial_items={"Incoming": [binary_item(pdf_bytes.getvalue(), name="blank.pdf")]},
    )
    assert pdf_result.terminal_outputs[0].items[0].json_data == {"parsed": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("type_version", [2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8])
async def test_postgres_node_runs_a_parameterized_query_on_async_engine(
    type_version: float,
) -> None:
    """Use a real SQLAlchemy async SQLite engine to verify safe positional binding."""
    workflow = load_workflow("converters_postgres.json")
    next(
        node for node in workflow.nodes if node.type == "n8n-nodes-base.postgres"
    ).type_version = type_version
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        result = await WorkflowExecutionEngine(
            default_node_converter_registry().build_runtime_registry(workflow)
        ).run(workflow, services={"postgres_client": SQLAlchemyPostgresClient(engine)})
    finally:
        await engine.dispose()

    assert result.terminal_outputs[0].items[0].json_data == {"value": 42}


@pytest.mark.asyncio
@pytest.mark.parametrize("type_version", [1, 1.1])
async def test_data_table_node_persists_rows_through_sqlalchemy_store(
    type_version: float,
) -> None:
    """Insert and read a row through a real SQLAlchemy-backed store."""
    workflow = load_workflow("converters_data_table.json")
    for node in workflow.nodes:
        if node.type == "n8n-nodes-base.dataTable":
            node.type_version = type_version
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SQLAlchemyDataTableStore(engine)
    try:
        result = await WorkflowExecutionEngine(
            default_node_converter_registry().build_runtime_registry(workflow)
        ).run(workflow, services={"data_table_store": store})
    finally:
        await engine.dispose()

    assert result.terminal_outputs[0].items[0].json_data["key"] == "alpha"
    assert result.terminal_outputs[0].items[0].json_data["value"] == 42
    assert isinstance(result.terminal_outputs[0].items[0].json_data["id"], int)


@pytest.mark.asyncio
async def test_data_table_store_runs_update_upsert_exists_and_delete() -> None:
    """Exercise supported Data Table CRUD against a real async SQL database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SQLAlchemyDataTableStore(engine)
    try:
        inserted = await store.execute_row_operation(
            table_id="crud-table",
            operation="insert",
            values={"key": "one", "value": 1},
            conditions=(),
            match_type="allConditions",
            limit=10,
            dry_run=False,
        )
        updated = await store.execute_row_operation(
            table_id="crud-table",
            operation="upsert",
            values={"value": 2},
            conditions=(("key", "eq", "one"),),
            match_type="allConditions",
            limit=10,
            dry_run=False,
        )
        found = await store.execute_row_operation(
            table_id="crud-table",
            operation="rowExists",
            values={},
            conditions=(("value", "eq", 2),),
            match_type="allConditions",
            limit=10,
            dry_run=False,
        )
        preview = await store.execute_row_operation(
            table_id="crud-table",
            operation="deleteRows",
            values={},
            conditions=(("key", "eq", "one"),),
            match_type="allConditions",
            limit=10,
            dry_run=True,
        )
        deleted = await store.execute_row_operation(
            table_id="crud-table",
            operation="deleteRows",
            values={},
            conditions=(("key", "eq", "one"),),
            match_type="allConditions",
            limit=10,
            dry_run=False,
        )
    finally:
        await engine.dispose()

    assert inserted[0]["value"] == 1
    assert updated[0]["value"] == 2
    assert found[0]["value"] == 2
    assert preview[0]["value"] == 2
    assert deleted[0]["value"] == 2


def test_data_table_rejects_updates_without_a_filter() -> None:
    """Require an explicit filter before an update can affect every stored row."""
    with pytest.raises(ValueError, match="требует filters.conditions"):
        DataTableParameters.model_validate(
            {
                "operation": "update",
                "dataTableId": {"mode": "id", "value": "table"},
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("type_version", [2, 2.1])
async def test_email_node_sends_through_a_real_local_smtp_server(
    monkeypatch, type_version: float
) -> None:
    """Deliver a message over a local SMTP protocol listener, without a mail mock."""
    captured = bytearray()

    async def smtp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 localhost ESMTP\r\n")
        await writer.drain()
        while line := await reader.readline():
            command = line.decode("ascii", errors="replace").strip().upper()
            if command.startswith("EHLO") or command.startswith("HELO"):
                writer.write(b"250-localhost\r\n250 SIZE 33554432\r\n")
            elif command == "DATA":
                writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                await writer.drain()
                while line := await reader.readline():
                    if line == b".\r\n":
                        break
                    captured.extend(line)
                writer.write(b"250 queued\r\n")
            elif command == "QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 OK\r\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(smtp_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(port))
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    workflow = load_workflow("converters_email.json")
    next(
        node for node in workflow.nodes if node.type == "n8n-nodes-base.emailSend"
    ).type_version = type_version
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    email_sender = create_email_sender_from_env()

    try:
        result = await WorkflowExecutionEngine(registry).run(
            workflow,
            services={"email_sender": email_sender},
        )
    finally:
        server.close()
        await server.wait_closed()

    mail = message_from_bytes(bytes(captured))
    assert mail["To"] == "receiver@example.test"
    assert mail["Subject"] == "Hello Test User"
    assert "Message for Test User" in mail.get_payload(decode=True).decode()
    assert result.terminal_outputs[0].items[0].json_data["name"] == "Test User"


def test_webhook_requires_a_valid_initial_input_mapping() -> None:
    """Fail clearly when a trigger entrypoint is called without selecting a trigger."""
    workflow = load_workflow("converters_files.json")
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    with pytest.raises(ValueError, match="укажите имя trigger-узла"):
        asyncio.run(WorkflowExecutionEngine(registry).run(workflow, initial_items={}))


@pytest.mark.asyncio
async def test_schedule_trigger_v1_3_emits_n8n_time_fields_for_a_runtime_tick() -> None:
    """Create the n8n Schedule Trigger payload for one selected schedule event."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    workflow = load_workflow("converters_schedule.json")
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    scheduled_at = datetime(2026, 10, 26, 15, 4, 5, tzinfo=ZoneInfo("Europe/Minsk"))

    result = await WorkflowExecutionEngine(registry).run(
        workflow,
        services={"timezone": "Europe/Minsk", "schedule_time": scheduled_at},
    )

    assert result.terminal_outputs[0].items[0].json_data == {
        "timestamp": "2026-10-26T15:04:05.000+03:00",
        "Readable date": "October 26th 2026, 3:04:05 pm",
        "Readable time": "3:04:05 pm",
        "Day of week": "Monday",
        "Year": "2026",
        "Month": "October",
        "Day of month": "26",
        "Hour": "15",
        "Minute": "04",
        "Second": "05",
        "Timezone": "Europe/Minsk (UTC+03:00)",
    }


def test_compression_rejects_binary_without_base64_data() -> None:
    """Reject missing binary input as a configuration error, not an empty file."""
    node = WorkflowNode(
        id="compress",
        name="ZIP",
        type="n8n-nodes-base.compression",
        typeVersion=1.1,
        position=(0, 0),
        parameters={"operation": "compress"},
    )
    executor = CompressionExecutor(CompressionParameters.model_validate(node.parameters))
    context = NodeExecutionContext(input_port=None, invocation_number=1, state={})

    with pytest.raises(UnsupportedNodeConfigurationError, match="не содержит binary-поле"):
        asyncio.run(executor.execute(node, (WorkflowItem(json={}),), context))
