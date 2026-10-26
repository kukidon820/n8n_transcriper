"""Integration tests for the supported AI Agent subgraph using a local HTTP endpoint."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from n8n_to_python.application.services.progress import ProgressReporter
from n8n_to_python.converters.langgraph import LangGraphGenerator
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.langgraph_agent import LangGraphAIAgentExecutor
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


def minimal_ai_workflow() -> Workflow:
    """Keep the real AI nodes/ports and add an Edit Fields node that supplies session id."""
    workflow = Workflow.model_validate_json((FIXTURES / "workflow_test.json").read_bytes())
    base_workflow = Workflow.model_validate_json((FIXTURES / "converters_basic.json").read_bytes())
    session_node = next(node for node in base_workflow.nodes if node.name == "Input fields")
    session_node = session_node.model_copy(deep=True, update={"name": "02 Session ID"})
    session_node.parameters["assignments"]["assignments"].append(
        {"id": "session-id", "name": "id", "value": "workflow-test", "type": "string"}
    )
    selected_names = {
        "01 Manual Trigger",
        "11 AI Agent (optional)",
        "12 DeepSeek Chat Model",
        "13 Calculator Tool",
        "14 Simple Memory",
    }
    data = workflow.model_dump(mode="json", by_alias=True)
    data["nodes"] = [node for node in data["nodes"] if node["name"] in selected_names]
    data["nodes"].append(session_node.model_dump(mode="json", by_alias=True))
    data["connections"] = {
        "01 Manual Trigger": {"main": [[{"node": "02 Session ID", "type": "main", "index": 0}]]},
        "02 Session ID": {
            "main": [[{"node": "11 AI Agent (optional)", "type": "main", "index": 0}]]
        },
        **{
            name: workflow.connections[name]
            for name in ("12 DeepSeek Chat Model", "13 Calculator Tool", "14 Simple Memory")
        },
    }
    return Workflow.model_validate(data)


def write_project(project_files: dict[str, str], root: Path) -> None:
    """Write the in-memory generated project into the pytest temporary directory."""
    for relative_path, content in project_files.items():
        target = root / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest_asyncio.fixture
async def local_deepseek_endpoint() -> AsyncIterator[tuple[str, list[dict[str, object]]]]:
    """Serve deterministic OpenAI-compatible chat responses over real local HTTP."""
    received_requests: list[dict[str, object]] = []

    async def complete(request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") == "Bearer local-integration-key"
        body = await request.json()
        received_requests.append(body)
        if len(received_requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "local-call-1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"query":"2 + 3 * 4"}',
                        },
                    }
                ],
            }
        elif len(received_requests) == 2:
            message = {"role": "assistant", "content": "Calculated 14."}
        else:
            message = {"role": "assistant", "content": "Second turn received."}
        return web.json_response({"choices": [{"message": message}]})

    application = web.Application()
    application.router.add_post("/chat/completions", complete)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", received_requests
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ai_agent_executes_model_calculator_and_simple_memory_connections(
    local_deepseek_endpoint: tuple[str, list[dict[str, object]]],
) -> None:
    """Use the real fixture's three AI ports and run model calls through aiohttp."""
    workflow = Workflow.model_validate_json((FIXTURES / "workflow_test.json").read_bytes())
    agent_node = next(node for node in workflow.nodes if node.type.endswith(".agent"))
    executor = LangGraphAIAgentExecutor.from_workflow(workflow, agent_node)
    base_url, received_requests = local_deepseek_endpoint
    state: dict[str, object] = {}
    services = {
        "deepseek_api_key": "local-integration-key",
        "deepseek_base_url": base_url,
        "deepseek_model": "deepseek-flash",
    }
    item = WorkflowItem(json={"id": "workflow-test", "question": "Сколько будет 2 + 3 * 4?"})

    first_result = await executor.execute(
        agent_node,
        (item,),
        NodeExecutionContext(
            input_port=None,
            invocation_number=1,
            state=state,
            services=services,
        ),
    )
    assert first_result.outputs[0][0].json_data["output"] == "Calculated 14."
    assert first_result.outputs[0][0].json_data["id"] == "workflow-test"
    assert [request["model"] for request in received_requests] == [
        "deepseek-flash",
        "deepseek-flash",
    ]
    assert received_requests[0]["tools"][0]["function"]["name"] == "calculator"
    assert received_requests[1]["messages"][-1]["role"] == "tool"
    assert received_requests[1]["messages"][-1]["content"] == "14"

    second_result = await executor.execute(
        agent_node,
        (item,),
        NodeExecutionContext(
            input_port=None,
            invocation_number=2,
            state=state,
            services=services,
        ),
    )
    assert second_result.outputs[0][0].json_data["output"] == "Second turn received."
    second_turn_messages = received_requests[2]["messages"]
    assert [message["role"] for message in second_turn_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "workflow-test" in second_turn_messages[1]["content"]


@pytest.mark.asyncio
async def test_generated_langgraph_ai_workflow_runs_the_real_fixture_end_to_end(
    tmp_path: Path,
    local_deepseek_endpoint: tuple[str, list[dict[str, object]]],
) -> None:
    """Generate and run trigger → AI Agent with its model/tool/memory AI connections."""
    workflow = minimal_ai_workflow()
    project = await LangGraphGenerator().generate(workflow, progress=ProgressReporter())
    assert (
        "REQUIRED_MANUAL_CHANGES = ()" in project.files["src/generated_workflow/manual_changes.py"]
    )
    for name in (
        "11 AI Agent (optional)",
        "12 DeepSeek Chat Model",
        "13 Calculator Tool",
        "14 Simple Memory",
    ):
        assert name not in project.files["MANUAL_CHANGES.md"]

    write_project(project.files, tmp_path)
    base_url, received_requests = local_deepseek_endpoint
    environment = os.environ.copy()
    source_path = str(tmp_path / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing_python_path else source_path + os.pathsep + existing_python_path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["DEEPSEEK_API_KEY"] = "local-integration-key"
    environment["DEEPSEEK_BASE_URL"] = base_url
    environment["DEEPSEEK_MODEL"] = "deepseek-flash"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "generated_workflow.main",
        cwd=tmp_path,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

    assert process.returncode == 0, stderr.decode()
    terminal_outputs = json.loads(stdout.decode())
    assert terminal_outputs[0]["node"] == "11 AI Agent (optional)"
    assert terminal_outputs[0]["items"][0]["json"]["output"] == "Calculated 14."
    assert len(received_requests) == 2
