"""Integration checks for user-supplied AI provider credentials."""

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest
from aiohttp import web

from n8n_to_python.application.services.progress import ProgressReporter
from n8n_to_python.converters.langgraph import LangGraphGenerator
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.langgraph_agent import (
    AI_AGENT_TYPE,
    DEEPSEEK_MODEL_TYPE,
    OPENAI_MODEL_TYPE,
    LangGraphAIAgentExecutor,
    UnsupportedAIAgentConfigurationError,
)
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_langgraph_agent_reads_provider_credentials_from_node_config() -> None:
    """Send the API key and model settings from the provider node's config entry."""
    workflow = Workflow.model_validate_json((FIXTURES / "workflow_test.json").read_bytes())
    agent = next(node for node in workflow.nodes if node.type == AI_AGENT_TYPE)
    model = next(node for node in workflow.nodes if node.type == DEEPSEEK_MODEL_TYPE)
    executor = LangGraphAIAgentExecutor.from_workflow(workflow, agent)
    requests: list[dict[str, object]] = []

    async def complete(request: web.Request) -> web.Response:
        requests.append(
            {
                "authorization": request.headers.get("Authorization"),
                **(await request.json()),
            }
        )
        return web.json_response(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Configured credentials work."}}
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        services={
            "credentials": {
                model.name: {
                    "api_key": "local-config-key",
                    "base_url": f"http://127.0.0.1:{port}",
                    "model": "configured-model",
                }
            }
        },
    )

    try:
        result = await executor.execute(
            agent,
            (WorkflowItem(json={"id": "config-test"}),),
            context,
        )
        assert result.outputs[0][0].json_data["output"] == "Configured credentials work."
        assert requests[0]["authorization"] == "Bearer local-config-key"
        assert requests[0]["model"] == "configured-model"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_generated_langgraph_project_loads_config_toml(tmp_path: Path) -> None:
    """Load a user's config.toml from the generated project root during execution."""
    workflow = Workflow.model_validate(
        {
            "name": "AI credentials config test",
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
                    "parameters": {"promptType": "define", "text": "Say hello", "options": {}},
                },
                {
                    "name": "DeepSeek Model",
                    "type": DEEPSEEK_MODEL_TYPE,
                    "typeVersion": 1,
                    "position": [200, 200],
                    "parameters": {"options": {}},
                },
            ],
            "connections": {
                "Manual Trigger": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}]]},
                "DeepSeek Model": {
                    "ai_languageModel": [
                        [{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]
                    ]
                },
            },
        }
    )
    project = await LangGraphGenerator().generate(workflow, progress=ProgressReporter())
    for relative_path, content in project.files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    received: list[dict[str, object]] = []

    async def complete(request: web.Request) -> web.Response:
        received.append(
            {
                "authorization": request.headers.get("Authorization"),
                **(await request.json()),
            }
        )
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "Loaded from config."}}]}
        )

    app = web.Application()
    app.router.add_post("/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    (tmp_path / "config.toml").write_text(
        '[credentials."DeepSeek Model"]\n'
        'api_key = "local-config-key"\n'
        f'base_url = "http://127.0.0.1:{port}"\n'
        'model = "configured-model"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_path = str(tmp_path / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing_python_path else source_path + os.pathsep + existing_python_path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        environment.pop(name, None)

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
        assert process.returncode == 0, stderr.decode()
        terminal_output = json.loads(stdout.decode())[0]["items"][0]["json"]
        assert terminal_output["output"] == "Loaded from config."
        assert received[0]["authorization"] == "Bearer local-config-key"
        assert received[0]["model"] == "configured-model"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_generated_langgraph_project_runs_openai_and_ignores_setup_note(
    tmp_path: Path,
) -> None:
    """Run the generated OpenAI adapter and treat sticky notes as workflow metadata."""
    workflow = Workflow.model_validate(
        {
            "name": "OpenAI model and note test",
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
                        "text": "Say hello",
                        "hasOutputParser": False,
                        "options": {"enableStreaming": False},
                    },
                },
                {
                    "name": "OpenAI Chat Model",
                    "type": OPENAI_MODEL_TYPE,
                    "typeVersion": 1.3,
                    "position": [200, 200],
                    "parameters": {
                        "model": {
                            "__rl": True,
                            "mode": "list",
                            "value": "gpt-4.1-mini",
                        },
                        "responsesApiEnabled": False,
                        "options": {"temperature": 0.2, "timeout": 20000, "maxRetries": 1},
                    },
                },
                {
                    "name": "Calculator",
                    "type": "@n8n/n8n-nodes-langchain.toolCalculator",
                    "typeVersion": 1,
                    "position": [200, 400],
                    "parameters": {},
                },
                {
                    "name": "Setup guide",
                    "type": "n8n-nodes-base.stickyNote",
                    "typeVersion": 1,
                    "position": [0, -200],
                    "parameters": {"content": "Configuration notes only."},
                },
            ],
            "connections": {
                "Manual Trigger": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}]]},
                "OpenAI Chat Model": {
                    "ai_languageModel": [
                        [{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]
                    ]
                },
                "Calculator": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
            },
        }
    )
    project = await LangGraphGenerator().generate(workflow, progress=ProgressReporter())
    assert (
        "REQUIRED_MANUAL_CHANGES = ()" in project.files["src/generated_workflow/manual_changes.py"]
    )
    assert "Setup guide" not in project.files["MANUAL_CHANGES.md"]
    config_example = tomllib.loads(project.files["config.toml.example"])
    assert config_example["credentials"]["OpenAI Chat Model"]["n8n_types"] == ["openAiApi"]
    assert config_example["credentials"]["OpenAI Chat Model"]["base_url"] == (
        "https://api.openai.com/v1"
    )
    assert config_example["credentials"]["OpenAI Chat Model"]["model"] == ""
    for relative_path, content in project.files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    received: list[dict[str, object]] = []

    async def complete(request: web.Request) -> web.Response:
        request_data = await request.json()
        received.append({"authorization": request.headers.get("Authorization"), **request_data})
        if len(received) == 1:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "calculator",
                                            "arguments": '{"query":"2 + 3"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "OpenAI works."}}]}
        )

    app = web.Application()
    app.router.add_post("/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    (tmp_path / "config.toml").write_text(
        '[credentials."OpenAI Chat Model"]\n'
        'api_key = "local-openai-key"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_path = str(tmp_path / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing_python_path else source_path + os.pathsep + existing_python_path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        environment.pop(name, None)

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
        assert process.returncode == 0, stderr.decode()
        terminal_output = json.loads(stdout.decode())[0]["items"][0]["json"]
        assert terminal_output["output"] == "OpenAI works."
        assert received[0]["authorization"] == "Bearer local-openai-key"
        assert received[0]["model"] == "gpt-4.1-mini"
        assert received[0]["stream"] is False
        assert "thinking" not in received[0]
        assert received[0]["tools"]
        assert received[1]["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "5",
        }
    finally:
        await runner.cleanup()


def test_openai_responses_api_is_reported_as_unsupported() -> None:
    """Reject OpenAI's different Responses API until its request schema is implemented."""
    workflow = Workflow.model_validate(
        {
            "name": "Unsupported Responses API",
            "nodes": [
                {
                    "name": "AI Agent",
                    "type": AI_AGENT_TYPE,
                    "typeVersion": 2.2,
                    "position": [0, 0],
                    "parameters": {
                        "promptType": "define",
                        "text": "Say hello",
                        "hasOutputParser": False,
                        "options": {"enableStreaming": False},
                    },
                },
                {
                    "name": "OpenAI Chat Model",
                    "type": OPENAI_MODEL_TYPE,
                    "typeVersion": 1.3,
                    "position": [200, 0],
                    "parameters": {"responsesApiEnabled": True, "options": {}},
                },
            ],
            "connections": {
                "OpenAI Chat Model": {
                    "ai_languageModel": [
                        [{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]
                    ]
                }
            },
        }
    )
    agent = next(node for node in workflow.nodes if node.type == AI_AGENT_TYPE)

    with pytest.raises(UnsupportedAIAgentConfigurationError, match="Responses API"):
        LangGraphAIAgentExecutor.from_workflow(workflow, agent)


@pytest.mark.parametrize(
    ("parameter_path", "enabled_value"),
    [("hasOutputParser", True), ("options.enableStreaming", True)],
)
def test_ai_agent_rejects_enabled_unimplemented_features(
    parameter_path: str,
    enabled_value: bool,
) -> None:
    """Do not silently ignore an output parser or streaming when either is enabled."""
    workflow = Workflow.model_validate_json((FIXTURES / "workflow_test.json").read_bytes())
    agent = next(node for node in workflow.nodes if node.type == AI_AGENT_TYPE)
    if parameter_path == "hasOutputParser":
        agent.parameters[parameter_path] = enabled_value
    else:
        agent.parameters.setdefault("options", {})["enableStreaming"] = enabled_value

    with pytest.raises(UnsupportedAIAgentConfigurationError, match="неподдержанные параметры"):
        LangGraphAIAgentExecutor.from_workflow(workflow, agent)
