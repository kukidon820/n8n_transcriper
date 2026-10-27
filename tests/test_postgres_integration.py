"""Необязательные интеграционные проверки на живом PostgreSQL."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import event

from n8n_to_python.application.services.progress import ProgressReporter
from n8n_to_python.converters.langgraph import LangGraphGenerator
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.converters.python_core import PythonCoreGenerator
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.infrastructure.database import create_postgres_engine_from_env
from n8n_to_python.infrastructure.postgres_client import SQLAlchemyPostgresClient
from n8n_to_python.runtime.engine import WorkflowExecutionEngine

FIXTURES = Path(__file__).parent / "fixtures"
POSTGRES_TEST_URL_VARIABLE = "N8N_TEST_POSTGRES_URL"


@pytest.mark.asyncio
async def test_real_postgres_node_runs_a_query_and_closes_its_connection() -> None:
    """Run the actual Postgres node adapter and dispose its pooled connection."""
    if not os.environ.get(POSTGRES_TEST_URL_VARIABLE):
        pytest.skip(f"Для живого PostgreSQL задайте {POSTGRES_TEST_URL_VARIABLE}.")

    workflow = Workflow.model_validate_json((FIXTURES / "converters_postgres.json").read_bytes())
    postgres_node = next(node for node in workflow.nodes if node.name == "Query")
    postgres_node.parameters["query"] = "SELECT CAST($1 AS INTEGER) AS value"
    engine = create_postgres_engine_from_env(variable_name=POSTGRES_TEST_URL_VARIABLE)
    client = SQLAlchemyPostgresClient(engine)
    closed_connections = 0

    def on_connection_closed(dbapi_connection, connection_record) -> None:
        nonlocal closed_connections
        closed_connections += 1

    event.listen(engine.sync_engine.pool, "close", on_connection_closed)
    try:
        result = await WorkflowExecutionEngine(
            default_node_converter_registry().build_runtime_registry(workflow)
        ).run(workflow, services={"postgres_client": client})
    finally:
        await client.close()

    assert result.terminal_outputs[0].items[0].json_data == {"value": 42}
    assert closed_connections >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("generator_type", [PythonCoreGenerator, LangGraphGenerator])
async def test_generated_project_runs_postgres_query_and_closes_services(
    tmp_path: Path,
    generator_type: type[PythonCoreGenerator] | type[LangGraphGenerator],
) -> None:
    """Run a generated backend against PostgreSQL and exercise its cleanup path."""
    database_url = os.environ.get(POSTGRES_TEST_URL_VARIABLE)
    if not database_url:
        pytest.skip(f"Для живого PostgreSQL задайте {POSTGRES_TEST_URL_VARIABLE}.")

    workflow = Workflow.model_validate_json((FIXTURES / "converters_postgres.json").read_bytes())
    postgres_node = next(node for node in workflow.nodes if node.name == "Query")
    postgres_node.parameters["query"] = "SELECT CAST($1 AS INTEGER) AS value"
    project = await generator_type().generate(workflow, progress=ProgressReporter())
    for relative_path, content in project.files.items():
        target = tmp_path / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["PYTHONIOENCODING"] = "utf-8"
    source_path = str(tmp_path / "src")
    previous_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not previous_python_path else source_path + os.pathsep + previous_python_path
    )
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

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    terminal_outputs = json.loads(stdout.decode("utf-8"))
    assert terminal_outputs[0]["items"][0]["json"]["value"] == 42
