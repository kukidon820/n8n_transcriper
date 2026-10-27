"""Проверки однократного AI API-вызова в Python Core без agent runtime."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

from n8n_to_python.application.services.progress import ProgressReporter
from n8n_to_python.converters.python_core import PythonCoreGenerator
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.ai_types import AI_AGENT_TYPE, DEEPSEEK_MODEL_TYPE, OPENAI_MODEL_TYPE
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.models import WorkflowItem
from n8n_to_python.runtime.python_core_ai import (
    PythonCoreAIAgentExecutor,
    inspect_python_core_ai_workflow,
)

FIXTURES = Path(__file__).parent / "fixtures"


def simple_ai_workflow(
    *,
    provider: str = "deepseek",
    agent_text: str = "={{ 'Review ' + $json.topic }}",
) -> Workflow:
    """Build a small workflow with one main edge and one AI model connection."""
    model_type = DEEPSEEK_MODEL_TYPE if provider == "deepseek" else OPENAI_MODEL_TYPE
    model_version = 1 if provider == "deepseek" else 1.3
    model_parameters: dict[str, object] = {
        "model": "deepseek-flash" if provider == "deepseek" else "gpt-4o-mini",
        "options": {"temperature": 0.3, "maxTokens": 100},
    }
    if provider == "openai":
        model_parameters["responsesApiEnabled"] = False
    return Workflow.model_validate(
        {
            "name": "Python Core simple AI",
            "nodes": [
                {
                    "name": "Manual Trigger",
                    "type": "n8n-nodes-base.manualTrigger",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {},
                },
                {
                    "name": "AI Agent",
                    "type": AI_AGENT_TYPE,
                    "typeVersion": 2.2,
                    "position": [200, 0],
                    "parameters": {
                        "promptType": "define",
                        "text": agent_text,
                        "options": {
                            "systemMessage": "Be concise.",
                            "maxIterations": 3,
                            "enableStreaming": False,
                        },
                    },
                },
                {
                    "name": "Chat Model",
                    "type": model_type,
                    "typeVersion": model_version,
                    "position": [200, 200],
                    "parameters": model_parameters,
                },
            ],
            "connections": {
                "Manual Trigger": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}]]},
                "Chat Model": {
                    "ai_languageModel": [
                        [{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]
                    ]
                },
            },
        }
    )


async def start_local_chat_endpoint(
    response_text: str,
) -> tuple[web.AppRunner, str, list[dict[str, object]]]:
    """Serve a real local Chat Completions HTTP endpoint for integration tests."""
    requests: list[dict[str, object]] = []

    async def complete(request: web.Request) -> web.Response:
        body = await request.json()
        requests.append(
            {
                "authorization": request.headers.get("Authorization"),
                **body,
            }
        )
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": response_text}}]}
        )

    app = web.Application()
    app.router.add_post("/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", requests


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "openai"])
async def test_python_core_ai_sends_one_authenticated_request_without_tools(
    provider: str,
) -> None:
    """Use node credentials, evaluate the prompt, and return the model's text in output."""
    workflow = simple_ai_workflow(provider=provider)
    agent = next(node for node in workflow.nodes if node.type == AI_AGENT_TYPE)
    model = next(node for node in workflow.nodes if node.name == "Chat Model")
    executor = PythonCoreAIAgentExecutor.from_workflow(workflow, agent)
    runner, base_url, received = await start_local_chat_endpoint("Direct API reply")

    try:
        result = await executor.execute(
            agent,
            (WorkflowItem(json={"topic": "quarterly results"}, binary={"source": {}}),),
            NodeExecutionContext(
                input_port=None,
                invocation_number=1,
                state={},
                services={
                    "credentials": {
                        model.name: {
                            "api_key": "local-user-key",
                            "base_url": base_url,
                            "model": "configured-chat-model",
                        }
                    }
                },
            ),
        )
    finally:
        await runner.cleanup()

    assert result.outputs[0][0].json_data == {
        "topic": "quarterly results",
        "output": "Direct API reply",
    }
    assert result.outputs[0][0].binary == {"source": {}}
    assert len(received) == 1
    assert received[0]["authorization"] == "Bearer local-user-key"
    assert received[0]["model"] == "configured-chat-model"
    assert received[0]["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Review quarterly results"},
    ]
    assert received[0]["stream"] is False
    assert "tools" not in received[0]


@pytest.mark.asyncio
async def test_python_core_ai_models_with_tools_or_memory_require_langgraph() -> None:
    """Prevent a simple API call from silently pretending to execute agent tools/memory."""
    workflow = Workflow.model_validate_json((FIXTURES / "workflow_test.json").read_bytes())
    plan = inspect_python_core_ai_workflow(workflow)
    messages = "\n".join(plan.manual_changes)

    assert not plan.executors
    assert "Calculator Tool" in messages
    assert "Simple Memory" in messages
    assert "не выполняет инструменты" in messages


@pytest.mark.asyncio
async def test_generated_python_core_project_runs_ai_from_config_toml(tmp_path: Path) -> None:
    """Run generated Python Core against a local HTTP API using its generated config file."""
    workflow = simple_ai_workflow(provider="deepseek", agent_text="Say hello")
    project = await PythonCoreGenerator().generate(workflow, progress=ProgressReporter())
    assert (
        project.files["src/generated_workflow/manual_changes.py"]
        .rstrip()
        .endswith("REQUIRED_MANUAL_CHANGES = ()")
    )
    assert "langgraph" not in project.files["pyproject.toml"].lower()
    assert 'api_key = ""' in project.files["config.toml.example"]

    for relative_path, content in project.files.items():
        target = tmp_path / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    runner, base_url, received = await start_local_chat_endpoint("Generated Python Core reply")
    (tmp_path / "config.toml").write_text(
        '[credentials."Chat Model"]\n'
        'api_key = "local-generated-key"\n'
        f'base_url = "{base_url}"\n'
        'model = "generated-config-model"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    generated_src = str(tmp_path / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        generated_src
        if not existing_python_path
        else generated_src + os.pathsep + existing_python_path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    for variable in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        environment.pop(variable, None)

    try:
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
    finally:
        await runner.cleanup()

    assert process.returncode == 0, stderr.decode("utf-8")
    output = json.loads(stdout.decode("utf-8"))[0]["items"][0]["json"]
    assert output["output"] == "Generated Python Core reply"
    assert len(received) == 1
    assert received[0]["authorization"] == "Bearer local-generated-key"
    assert received[0]["model"] == "generated-config-model"
    assert "tools" not in received[0]
