"""Проверки генерации и запуска отдельного Python Core проекта."""

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from n8n_to_python.application.ports.workflow_generator import GenerationBackend
from n8n_to_python.application.services.progress import ProgressEvent, ProgressReporter
from n8n_to_python.application.services.workflow_converter import WorkflowConverter
from n8n_to_python.converters.langgraph import LangGraphGenerator
from n8n_to_python.converters.python_core import PythonCoreGenerator
from n8n_to_python.domain.models.workflow import Workflow

FIXTURES = Path(__file__).parent / "fixtures"


def load_workflow(fixture_name: str) -> Workflow:
    """Загрузить и проверить локальный n8n JSON fixture."""
    return Workflow.model_validate_json((FIXTURES / fixture_name).read_bytes())


def write_project(project_files: dict[str, str], root: Path) -> None:
    """Записать GeneratedProject во временную директорию теста."""
    for relative_path, content in project_files.items():
        target = root / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def run_generated_project(
    root: Path,
    *,
    unset_environment: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Запустить сгенерированный пакет отдельным процессом Python."""
    environment = os.environ.copy()
    for variable_name in unset_environment:
        environment.pop(variable_name, None)
    source_path = str(root / "src")
    previous_python_path = environment.get("PYTHONPATH")
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = (
        source_path if not previous_python_path else source_path + os.pathsep + previous_python_path
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "generated_workflow.main",
        cwd=root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


@pytest.mark.asyncio
async def test_python_core_generates_and_runs_a_real_branching_workflow(tmp_path: Path) -> None:
    """Сгенерировать модульный проект и выполнить в нём Manual→Set→IF→Switch fixture."""
    events: list[ProgressEvent] = []
    generator = PythonCoreGenerator()

    converter = WorkflowConverter(
        {GenerationBackend.PYTHON_CORE: generator},
        progress=ProgressReporter(events.append),
    )
    project = await converter.convert(
        load_workflow("converters_basic.json"), GenerationBackend.PYTHON_CORE
    )

    assert "pyproject.toml" in project.files
    assert "MANUAL_CHANGES.md" in project.files
    assert "src/generated_workflow/main.py" in project.files
    assert "src/generated_workflow/workflow.json" in project.files
    assert "src/n8n_to_python/runtime/engine.py" in project.files
    assert "src/n8n_to_python/cli/main.py" not in project.files
    manifest = tomllib.loads(project.files["pyproject.toml"])
    assert manifest["project"]["scripts"]["run-workflow"] == "generated_workflow.main:main"
    assert "pydantic>=2.0" in manifest["project"]["dependencies"]
    assert [(event.stage, event.status) for event in events] == [
        ("conversion:python-core", "started"),
        ("python-core:analyze-workflow", "started"),
        ("python-core:analyze-workflow", "succeeded"),
        ("python-core:render-project", "started"),
        ("python-core:render-project", "succeeded"),
        ("conversion:python-core", "succeeded"),
    ]

    write_project(project.files, tmp_path)
    return_code, stdout, stderr = await run_generated_project(tmp_path)

    assert return_code == 0, stderr
    terminal_outputs = json.loads(stdout)
    assert [output["node"] for output in terminal_outputs] == ["Branch A"]
    assert terminal_outputs[0]["items"][0]["json"]["branch"] == "A"


@pytest.mark.asyncio
async def test_langgraph_and_python_core_generate_equal_results_for_the_same_fixture(
    tmp_path: Path,
) -> None:
    """Run the same real branch fixture through both independently generated projects."""
    workflow = load_workflow("converters_basic.json")
    events: list[ProgressEvent] = []
    converter = WorkflowConverter(
        {
            GenerationBackend.PYTHON_CORE: PythonCoreGenerator(),
            GenerationBackend.LANGGRAPH: LangGraphGenerator(),
        },
        progress=ProgressReporter(events.append),
    )
    python_core_project = await converter.convert(workflow, GenerationBackend.PYTHON_CORE)
    langgraph_project = await converter.convert(workflow, GenerationBackend.LANGGRAPH)

    core_root = tmp_path / "python-core"
    langgraph_root = tmp_path / "langgraph"
    write_project(python_core_project.files, core_root)
    write_project(langgraph_project.files, langgraph_root)
    core_exit, core_stdout, core_stderr = await run_generated_project(core_root)
    graph_exit, graph_stdout, graph_stderr = await run_generated_project(langgraph_root)

    assert core_exit == 0, core_stderr
    assert graph_exit == 0, graph_stderr
    assert json.loads(graph_stdout) == json.loads(core_stdout)
    graph_manifest = tomllib.loads(langgraph_project.files["pyproject.toml"])
    core_manifest = tomllib.loads(python_core_project.files["pyproject.toml"])
    assert "langgraph>=1.0,<2" in graph_manifest["project"]["dependencies"]
    assert not any(
        dependency.startswith("langgraph")
        for dependency in core_manifest["project"]["dependencies"]
    )
    assert "LangGraphExecutionEngine" in langgraph_project.files["src/generated_workflow/main.py"]
    assert any(event.stage == "langgraph:render-project" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("generator_type", [PythonCoreGenerator, LangGraphGenerator])
async def test_generated_database_workflow_reports_missing_database_url(
    tmp_path: Path,
    generator_type: type[PythonCoreGenerator] | type[LangGraphGenerator],
) -> None:
    """Fail with a configuration message instead of a later missing-client error."""
    workflow = load_workflow("converters_postgres.json")
    project = await generator_type().generate(workflow, progress=ProgressReporter())
    write_project(project.files, tmp_path)

    return_code, stdout, stderr = await run_generated_project(
        tmp_path,
        unset_environment=("DATABASE_URL",),
    )

    assert return_code != 0
    assert stdout == ""
    assert "Не задана переменная окружения DATABASE_URL" in stderr


@pytest.mark.asyncio
async def test_generated_project_fails_fast_and_documents_unsupported_ai_node(
    tmp_path: Path,
) -> None:
    """Не пропускать AI Agent молча; назвать его и путь расширения в руководстве."""
    workflow = load_workflow("workflow_test.json")
    project = await PythonCoreGenerator().generate(workflow, progress=ProgressReporter())

    manual_guide = project.files["MANUAL_CHANGES.md"]
    assert "typeVersion" in manual_guide
    assert "src/n8n_to_python/converters/nodes.py" in manual_guide
    assert "не поддержан" in manual_guide or "не поддерживается" in manual_guide
    assert (
        "REQUIRED_MANUAL_CHANGES = (" in project.files["src/generated_workflow/manual_changes.py"]
    )

    write_project(project.files, tmp_path)
    return_code, stdout, stderr = await run_generated_project(tmp_path)

    assert return_code != 0
    assert "Сначала выполните ручные доработки" in stderr


@pytest.mark.asyncio
async def test_generator_removes_credentials_and_redacts_secret_like_parameters() -> None:
    """Не копировать credential values в generated files и сообщить, где их вернуть."""
    workflow = load_workflow("converters_basic.json")
    workflow.nodes[0].credentials = {
        "exampleAuth": {"id": "private-credential-id", "token": "private-token-value"}
    }
    workflow.nodes[1].parameters["apiToken"] = "inline-secret-value"

    project = await PythonCoreGenerator().generate(workflow, progress=ProgressReporter())

    generated_contents = "\n".join(project.files.values())
    workflow_json = project.files["src/generated_workflow/workflow.json"]
    manual_guide = project.files["MANUAL_CHANGES.md"]
    assert "private-token-value" not in generated_contents
    assert "private-credential-id" not in generated_contents
    assert "inline-secret-value" not in generated_contents
    assert "<SET_MANUALLY>" in workflow_json
    assert "Input fields.parameters.apiToken" in manual_guide
    assert "Manual (n8n-nodes-base.manualTrigger v1.0): exampleAuth" in manual_guide


@pytest.mark.asyncio
async def test_generator_redacts_http_auth_headers_and_secret_query_values() -> None:
    """Не записывать Authorization headers и API tokens из URL в generated project."""
    workflow = load_workflow("converters_http.json")
    request_node = next(node for node in workflow.nodes if node.type.endswith("httpRequest"))
    request_node.parameters["url"] = (
        "https://user:url-userinfo-secret@example.test/echo?api_key=url-secret-value"
    )
    request_node.parameters["headerParameters"] = {
        "parameters": [{"name": "Authorization", "value": "Bearer header-secret-value"}]
    }
    request_node.parameters["sendHeaders"] = True

    project = await PythonCoreGenerator().generate(workflow, progress=ProgressReporter())

    generated_contents = "\n".join(project.files.values())
    manual_guide = project.files["MANUAL_CHANGES.md"]
    assert "url-secret-value" not in generated_contents
    assert "url-userinfo-secret" not in generated_contents
    assert "header-secret-value" not in generated_contents
    assert "Request.parameters.headerParameters.parameters[0].value" in manual_guide
    assert "Request.parameters.url.query.api_key" in manual_guide
    assert "Request.parameters.url.userinfo" in manual_guide
