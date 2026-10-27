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
from n8n_to_python.converters.python_core import PythonCoreGenerator
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.langgraph_agent import LangGraphAIAgentExecutor
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_WORKFLOW = Path(__file__).parents[1] / "n8n_public_api_python_csv_zip_email.json"
AI_AGENT_NAME = "03 AI Agent - publication review"
AI_MODEL_NAME = "AI DeepSeek Chat Model"
AI_TOOL_NAME = "AI Calculator Tool"
AI_MEMORY_NAME = "AI Simple Memory"


def minimal_ai_workflow(public_api_url: str) -> Workflow:
    """Extract the real HTTP → Python filter → AI path and its connected AI nodes."""
    workflow = Workflow.model_validate_json(PUBLIC_WORKFLOW.read_bytes())
    trigger_name = next(
        node.name for node in workflow.nodes if node.type.endswith(".manualTrigger")
    )
    posts_request = next(node for node in workflow.nodes if node.name.startswith("02 "))
    posts_filter = next(
        node
        for node in workflow.nodes
        if node.type == "n8n-nodes-base.code" and node.parameters.get("language") == "pythonNative"
    )
    selected_names = {
        trigger_name,
        posts_request.name,
        posts_filter.name,
        AI_AGENT_NAME,
        AI_MODEL_NAME,
        AI_TOOL_NAME,
        AI_MEMORY_NAME,
    }
    data = workflow.model_dump(mode="json", by_alias=True)
    data["nodes"] = [node for node in data["nodes"] if node["name"] in selected_names]
    next(node for node in data["nodes"] if node["name"] == posts_request.name)["parameters"][
        "url"
    ] = f"{public_api_url}/posts"
    data["connections"] = {
        trigger_name: {"main": [[{"node": posts_request.name, "type": "main", "index": 0}]]},
        posts_request.name: {"main": [[{"node": posts_filter.name, "type": "main", "index": 0}]]},
        posts_filter.name: {"main": [[{"node": AI_AGENT_NAME, "type": "main", "index": 0}]]},
        **{
            name: data["connections"][name]
            for name in (AI_MODEL_NAME, AI_TOOL_NAME, AI_MEMORY_NAME)
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


@pytest_asyncio.fixture
async def local_public_api_endpoint() -> AsyncIterator[str]:
    """Serve representative posts to the real HTTP Request node without internet."""

    async def posts(_: web.Request) -> web.Response:
        return web.json_response(
            [
                {
                    "id": 2,
                    "userId": 1,
                    "title": "An even public publication",
                    "body": "A filtered publication body.",
                },
                {
                    "id": 3,
                    "userId": 1,
                    "title": "An odd public publication",
                    "body": "This post should be filtered out.",
                },
                {"id": 4, "userId": 1, "title": "Short", "body": "Short title."},
            ]
        )

    application = web.Application()
    application.router.add_get("/posts", posts)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ai_agent_executes_model_calculator_and_simple_memory_connections(
    local_deepseek_endpoint: tuple[str, list[dict[str, object]]],
) -> None:
    """Run the public workflow's three AI ports through the local aiohttp endpoint."""
    workflow = Workflow.model_validate_json(PUBLIC_WORKFLOW.read_bytes())
    agent_node = next(node for node in workflow.nodes if node.name == AI_AGENT_NAME)
    executor = LangGraphAIAgentExecutor.from_workflow(workflow, agent_node)
    base_url, received_requests = local_deepseek_endpoint
    state: dict[str, object] = {}
    services = {
        "deepseek_api_key": "local-integration-key",
        "deepseek_base_url": base_url,
        "deepseek_model": "deepseek-flash",
    }
    item = WorkflowItem(
        json={"id": "workflow-test", "posts_count": 1, "question": "Сколько будет 2 + 3 * 4?"}
    )

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
    local_public_api_endpoint: str,
) -> None:
    """Run the public HTTP → Python → AI path using only local integration endpoints."""
    workflow = minimal_ai_workflow(local_public_api_endpoint)
    project = await LangGraphGenerator().generate(workflow, progress=ProgressReporter())
    assert (
        "REQUIRED_MANUAL_CHANGES = ()" in project.files["src/generated_workflow/manual_changes.py"]
    )
    for name in (AI_AGENT_NAME, AI_MODEL_NAME, AI_TOOL_NAME, AI_MEMORY_NAME):
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
    assert terminal_outputs[0]["node"] == AI_AGENT_NAME
    agent_output = terminal_outputs[0]["items"][0]["json"]
    assert agent_output["output"] == "Calculated 14."
    assert agent_output["posts_count"] == 1
    assert agent_output["posts"][0]["post_id"] == 2
    assert "An even public publication" in received_requests[0]["messages"][-1]["content"]
    assert len(received_requests) == 2


@pytest.mark.asyncio
async def test_public_workflow_ai_support_is_backend_specific() -> None:
    """Keep the AI subgraph supported in LangGraph while Python Core reports its limitation."""
    workflow = Workflow.model_validate_json(PUBLIC_WORKFLOW.read_bytes())

    langgraph_project = await LangGraphGenerator().generate(
        workflow,
        progress=ProgressReporter(),
    )
    langgraph_manual_changes = langgraph_project.files["MANUAL_CHANGES.md"]
    for name in (AI_AGENT_NAME, AI_MODEL_NAME, AI_TOOL_NAME, AI_MEMORY_NAME):
        assert name not in langgraph_manual_changes
    assert "07 · Code — собрать CSV" in langgraph_manual_changes
    assert "fileAttachments" not in langgraph_manual_changes

    python_core_project = await PythonCoreGenerator().generate(
        workflow,
        progress=ProgressReporter(),
    )
    python_core_manual_changes = python_core_project.files["MANUAL_CHANGES.md"]
    assert AI_AGENT_NAME in python_core_manual_changes
