"""HTTP response formats and binary handoff exercised against a local aiohttp server."""

import base64
from pathlib import Path

import pytest
from aiohttp import ClientResponseError, web

from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow, WorkflowConnection, WorkflowNode
from n8n_to_python.runtime.engine import WorkflowExecutionEngine
from n8n_to_python.runtime.errors import NodeExecutionError
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@pytest.mark.parametrize("type_version", [4, 4.1, 4.2, 4.3, 4.4, 4.5])
async def test_http_request_supports_json_text_file_autodetect_and_full_response(
    type_version: float,
) -> None:
    """Exercise response modes for every registered HTTP Request v4 version."""

    async def json_object(_: web.Request) -> web.Response:
        return web.json_response({"answer": 42})

    async def json_array(_: web.Request) -> web.Response:
        return web.json_response([{"answer": 1}, {"answer": 2}])

    async def json_scalar(_: web.Request) -> web.Response:
        return web.json_response(42)

    async def json_scalar_array(_: web.Request) -> web.Response:
        return web.json_response([1, "two", None])

    async def text(_: web.Request) -> web.Response:
        return web.Response(text="plain response", content_type="text/plain")

    async def empty_text(_: web.Request) -> web.Response:
        return web.Response(body=b"", content_type="text/plain")

    async def json_as_text(_: web.Request) -> web.Response:
        return web.json_response({"answer": 42})

    async def html(_: web.Request) -> web.Response:
        return web.Response(text="<h1>hello</h1>", content_type="text/html")

    async def csv_file(_: web.Request) -> web.Response:
        return web.Response(
            body=b"name,score\nAda,10\nGrace,9\n",
            content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="results.csv"'},
        )

    async def pdf_file(_: web.Request) -> web.Response:
        return web.Response(
            body=b"%PDF-1.4\nsmall fixture\n",
            content_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="sample.pdf"'},
        )

    async def missing(_: web.Request) -> web.Response:
        return web.json_response({"error": "missing"}, status=404, reason="Not Found")

    app = web.Application()
    for path, handler in (
        ("/json", json_object),
        ("/array", json_array),
        ("/scalar", json_scalar),
        ("/scalar-array", json_scalar_array),
        ("/text", text),
        ("/empty-text", empty_text),
        ("/json-as-text", json_as_text),
        ("/html", html),
        ("/csv", csv_file),
        ("/pdf", pdf_file),
        ("/missing", missing),
    ):
        app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]

    workflow = Workflow.model_validate_json((FIXTURES / "converters_http.json").read_bytes())
    request_node = next(node for node in workflow.nodes if node.name == "Request")
    request_node.type_version = type_version

    async def run(path: str, response_settings: dict[str, object]) -> object:
        """Run the fixture against a chosen response endpoint and option set."""
        request_node.parameters["url"] = f"http://127.0.0.1:{port}{path}"
        request_node.parameters["options"] = {
            "timeout": 5000,
            "response": {"response": response_settings},
        }
        registry = default_node_converter_registry().build_runtime_registry(workflow)
        return await WorkflowExecutionEngine(registry).run(workflow)

    try:
        json_result = await run("/json", {"responseFormat": "json"})
        assert json_result.terminal_outputs[0].items[0].json_data == {"answer": 42}

        array_result = await run("/array", {"responseFormat": "json"})
        assert [item.json_data for item in array_result.terminal_outputs[0].items] == [
            {"answer": 1},
            {"answer": 2},
        ]

        scalar_result = await run("/scalar", {"responseFormat": "json"})
        assert scalar_result.terminal_outputs[0].items[0].json_data == 42

        scalar_array_result = await run("/scalar-array", {"responseFormat": "json"})
        assert [item.json_data for item in scalar_array_result.terminal_outputs[0].items] == [
            1,
            "two",
            {},
        ]

        text_result = await run(
            "/text", {"responseFormat": "text", "outputPropertyName": "content"}
        )
        assert text_result.terminal_outputs[0].items[0].json_data == {"content": "plain response"}

        empty_text_result = await run(
            "/empty-text", {"responseFormat": "text", "outputPropertyName": "content"}
        )
        assert empty_text_result.terminal_outputs[0].items[0].json_data == {"content": ""}

        json_text_result = await run(
            "/json-as-text", {"responseFormat": "text", "outputPropertyName": "content"}
        )
        assert json_text_result.terminal_outputs[0].items[0].json_data == {
            "content": '{"answer":42}'
        }

        auto_text_result = await run("/html", {"responseFormat": "autodetect"})
        assert auto_text_result.terminal_outputs[0].items[0].json_data == {"data": "<h1>hello</h1>"}

        pdf_result = await run("/pdf", {"responseFormat": "file", "outputPropertyName": "document"})
        pdf_item = pdf_result.terminal_outputs[0].items[0]
        pdf_binary = pdf_item.binary["document"]
        assert pdf_item.json_data == {"query": "hello"}
        assert base64.b64decode(pdf_binary["data"]) == b"%PDF-1.4\nsmall fixture\n"
        assert pdf_binary["mimeType"] == "application/pdf"
        assert pdf_binary["fileName"] == "sample.pdf"

        auto_pdf_result = await run("/pdf", {"responseFormat": "autodetect"})
        assert "data" in auto_pdf_result.terminal_outputs[0].items[0].binary

        full_json_result = await run(
            "/json",
            {"responseFormat": "json", "fullResponse": True},
        )
        full_json = full_json_result.terminal_outputs[0].items[0].json_data
        assert full_json["body"] == {"answer": 42}
        assert full_json["statusCode"] == 200
        assert full_json["headers"]["content-type"].startswith("application/json")

        full_text_result = await run(
            "/text",
            {
                "responseFormat": "text",
                "outputPropertyName": "content",
                "fullResponse": True,
            },
        )
        full_text = full_text_result.terminal_outputs[0].items[0].json_data
        assert full_text["content"] == "plain response"
        assert full_text["statusCode"] == 200
        assert "body" not in full_text

        full_file_result = await run("/pdf", {"responseFormat": "file", "fullResponse": True})
        full_file = full_file_result.terminal_outputs[0].items[0]
        assert full_file.json_data["statusCode"] == 200
        assert "body" not in full_file.json_data
        assert "data" in full_file.binary

        with pytest.raises(NodeExecutionError) as status_error:
            await run("/missing", {"responseFormat": "json"})
        assert isinstance(status_error.value.__cause__, ClientResponseError)
        assert status_error.value.__cause__.status == 404

        never_error_result = await run(
            "/missing",
            {"responseFormat": "json", "fullResponse": True, "neverError": True},
        )
        never_error = never_error_result.terminal_outputs[0].items[0].json_data
        assert never_error["body"] == {"error": "missing"}
        assert never_error["statusCode"] == 404

        # A file response must remain usable by the next file-processing node.
        extract_node = {
            "id": "extract",
            "name": "Extract CSV",
            "type": "n8n-nodes-base.extractFromFile",
            "typeVersion": 1.2,
            "position": [600, 0],
            "parameters": {
                "operation": "csv",
                "binaryPropertyName": "data",
                "destinationKey": "row",
                "options": {"headerRow": True, "keepSource": True},
            },
        }
        workflow.nodes.append(WorkflowNode.model_validate(extract_node))
        workflow.connections["Request"] = {
            "main": [[WorkflowConnection(node="Extract CSV", type="main", index=0)]]
        }
        csv_result = await run("/csv", {"responseFormat": "file"})
        assert [item.json_data for item in csv_result.terminal_outputs[0].items] == [
            {"row": {"name": "Ada", "score": "10"}},
            {"row": {"name": "Grace", "score": "9"}},
        ]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_json_array_outputs_keep_their_input_item_links() -> None:
    """Each expanded response item remains paired with the request input that created it."""

    seen_queries: list[str | None] = []

    async def response(request: web.Request) -> web.Response:
        seen_queries.append(request.query.get("q"))
        return web.json_response([{"value": "a"}, {"value": "b"}])

    app = web.Application()
    app.router.add_get("/items", response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    workflow = Workflow.model_validate_json((FIXTURES / "converters_http.json").read_bytes())
    request_node = next(node for node in workflow.nodes if node.name == "Request")
    request_node.parameters["url"] = f"http://127.0.0.1:{port}/items"
    request_node.parameters["options"] = {
        "timeout": 5000,
        "response": {"response": {"responseFormat": "json"}},
    }
    request_node.parameters["queryParameters"]["parameters"][0]["value"] = (
        "={{ $('Manual').item.json.seed }}"
    )
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    try:
        result = await WorkflowExecutionEngine(registry).run(
            workflow,
            initial_items={
                "Manual": (
                    WorkflowItem(json={"seed": "first"}),
                    WorkflowItem(json={"seed": "second"}),
                )
            },
        )
    finally:
        await runner.cleanup()

    output_items = result.terminal_outputs[0].items
    assert [item.lineage["Request"] for item in output_items] == [0, 1, 2, 3]
    assert [item.lineage["Input query"] for item in output_items] == [0, 0, 1, 1]
    assert seen_queries == ["first", "second"]
