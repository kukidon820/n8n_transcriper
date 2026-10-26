"""Проверки первых точечно поддержанных конвертеров реальных n8n-узлов."""

import json
from pathlib import Path

import pytest
from aiohttp import ClientResponseError, web

from n8n_to_python.converters.errors import (
    UnsupportedNodeConfigurationError,
    UnsupportedNodeVersionError,
)
from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.engine import WorkflowExecutionEngine
from n8n_to_python.runtime.errors import NodeExecutionError
from n8n_to_python.runtime.executors import NodeExecutionContext
from n8n_to_python.runtime.models import WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


def load_workflow(filename: str) -> Workflow:
    """Проверить локальный workflow fixture той же Pydantic-моделью, что и приложение."""
    return Workflow.model_validate_json((FIXTURES / filename).read_bytes())


def set_input_fields(workflow: Workflow, *, score: int, route: str) -> None:
    """Поменять статические значения тестового Edit Fields узла."""
    node = next(node for node in workflow.nodes if node.name == "Input fields")
    assignments = node.parameters["assignments"]["assignments"]
    assignments[0]["value"] = score
    assignments[1]["value"] = route


@pytest.mark.asyncio
async def test_manual_edit_fields_if_and_switch_route_a() -> None:
    """Проверить запуск trigger, добавление полей, true-ветку IF и правило Switch A."""
    workflow = load_workflow("converters_basic.json")
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    result = await WorkflowExecutionEngine(registry).run(workflow)

    terminal = result.terminal_outputs
    assert len(terminal) == 1
    assert terminal[0].node_name == "Branch A"
    assert terminal[0].items[0].json_data == {"score": 80, "route": "A", "branch": "A"}
    assert [record.node_name for record in result.executions] == [
        "Manual",
        "Input fields",
        "Score check",
        "Route",
        "Branch A",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "route", "expected_terminal"),
    [(80, "B", "Branch B"), (80, "OTHER", "Fallback"), (20, "A", "Low score")],
)
async def test_if_switch_route_other_outputs(
    score: int,
    route: str,
    expected_terminal: str,
) -> None:
    """Проверить альтернативное правило, fallback и false-выход IF."""
    workflow = load_workflow("converters_basic.json")
    set_input_fields(workflow, score=score, route=route)
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    result = await WorkflowExecutionEngine(registry).run(workflow)

    assert [output.node_name for output in result.terminal_outputs] == [expected_terminal]


@pytest.mark.asyncio
async def test_loop_over_items_batches_and_emits_done_output() -> None:
    """Проверить асинхронную петлю, обратное соединение и выход после завершения."""
    workflow = load_workflow("converters_loop.json")
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    result = await WorkflowExecutionEngine(registry).run(workflow)

    assert [record.node_name for record in result.executions].count("Loop") == 2
    assert [output.node_name for output in result.terminal_outputs] == ["Done"]
    assert result.terminal_outputs[0].items[0].json_data == {
        "processed": True,
        "finished": True,
    }


def test_registry_reports_javascript_code_node_as_unsupported() -> None:
    """Не считать JavaScript Code поддержанным и не генерировать ложную реализацию."""
    workflow = load_workflow("workflow_test.json")

    with pytest.raises(UnsupportedNodeVersionError, match="n8n-nodes-base.code.*не поддержан"):
        default_node_converter_registry().build_runtime_registry(workflow)


def test_registry_requires_an_exact_node_version() -> None:
    """Отказаться от не протестированной версии даже при совпавшем типе узла."""
    workflow = load_workflow("converters_basic.json")
    workflow.nodes[0].type_version = 2

    with pytest.raises(UnsupportedNodeVersionError, match=r"Поддержанные версии: \[1\]"):
        default_node_converter_registry().build_runtime_registry(workflow)


def test_registry_rejects_unimplemented_edit_fields_mode() -> None:
    """Сообщить о неподдерживаемом режиме Edit Fields до выполнения workflow."""
    workflow = load_workflow("converters_basic.json")
    fields_node = next(node for node in workflow.nodes if node.name == "Input fields")
    fields_node.parameters["mode"] = "json"

    with pytest.raises(UnsupportedNodeConfigurationError, match="Input fields.*параметры"):
        default_node_converter_registry().build_runtime_registry(workflow)


def test_fixture_has_only_local_data_and_expected_node_types() -> None:
    """Зафиксировать базовую матрицу тестового workflow без сетевых зависимостей."""
    fixture_data = json.loads((FIXTURES / "converters_basic.json").read_text(encoding="utf-8"))

    assert {node["type"] for node in fixture_data["nodes"]} == {
        "n8n-nodes-base.manualTrigger",
        "n8n-nodes-base.set",
        "n8n-nodes-base.if",
        "n8n-nodes-base.switch",
    }


@pytest.mark.asyncio
async def test_edit_fields_reads_simple_expression_and_writes_dot_notation() -> None:
    """Выражение читает входной item, а имя с точкой создаёт вложенный объект."""
    workflow = load_workflow("converters_basic.json")
    fields_node = next(node for node in workflow.nodes if node.name == "Input fields")
    fields_node.parameters["assignments"]["assignments"] = [
        {
            "name": "metadata.label",
            "value": "={{ $json.nested.value }}",
            "type": "string",
        }
    ]
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    executor = registry.get(fields_node)
    item = WorkflowItem(json={"nested": {"value": "from-input"}})

    result = await executor.execute(
        fields_node,
        (item,),
        NodeExecutionContext(input_port=None, invocation_number=1, state={}),
    )

    assert result.outputs[0] is not None
    assert result.outputs[0][0].json_data == {
        "nested": {"value": "from-input"},
        "metadata": {"label": "from-input"},
    }


@pytest.mark.asyncio
async def test_loop_batches_multiple_items_and_collects_processed_results() -> None:
    """Пакеты из нескольких items собираются обратно на done-выходе."""
    workflow = load_workflow("converters_loop.json")
    loop_node = next(node for node in workflow.nodes if node.name == "Loop")
    loop_node.parameters["batchSize"] = 2
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    executor = registry.get(loop_node)
    context = NodeExecutionContext(input_port=None, invocation_number=1, state={})
    incoming = tuple(WorkflowItem(json={"number": value}) for value in (1, 2, 3))

    first = await executor.execute(loop_node, incoming, context)
    first_batch = first.outputs[1]
    assert first.outputs[0] is None
    assert first_batch is not None and len(first_batch) == 2

    processed_first = tuple(
        WorkflowItem(json={**item.json_data, "processed": True}) for item in first_batch
    )
    second = await executor.execute(loop_node, processed_first, context)
    second_batch = second.outputs[1]
    assert second.outputs[0] is None
    assert second_batch is not None and len(second_batch) == 1

    processed_second = tuple(
        WorkflowItem(json={**item.json_data, "processed": True}) for item in second_batch
    )
    third = await executor.execute(loop_node, processed_second, context)

    assert third.outputs[1] is None
    assert third.outputs[0] is not None
    assert [item.json_data["number"] for item in third.outputs[0]] == [1, 2, 3]
    assert all(item.json_data["processed"] for item in third.outputs[0])


@pytest.mark.asyncio
async def test_http_request_uses_aiohttp_and_respects_the_query_parameters() -> None:
    """Проверить реальный HTTP-обмен с локальным сервером без сетевых моков."""

    async def echo(request: web.Request) -> web.Response:
        """Вернуть query-параметр в JSON для проверки клиентского запроса."""
        return web.json_response({"query": request.query.get("q")})

    async def fail(request: web.Request) -> web.Response:
        """Вернуть HTTP-ошибку, которую HTTP Request должен пробросить."""
        return web.json_response({"error": "unavailable"}, status=503)

    async def echo_body(request: web.Request) -> web.Response:
        """Вернуть JSON body принятого запроса для проверки expression в теле."""
        return web.json_response({"payload": await request.json()})

    app = web.Application()
    app.router.add_get("/echo", echo)
    app.router.add_get("/fail", fail)
    app.router.add_post("/body", echo_body)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    workflow = load_workflow("converters_http.json")
    request_node = next(node for node in workflow.nodes if node.name == "Request")
    request_node.parameters["url"] = f"http://127.0.0.1:{port}/echo"
    registry = default_node_converter_registry().build_runtime_registry(workflow)

    try:
        result = await WorkflowExecutionEngine(registry).run(workflow)
        assert result.terminal_outputs[0].items[0].json_data == {"query": "hello"}

        request_node.parameters.update(
            {
                "url": f"http://127.0.0.1:{port}/body",
                "method": "POST",
                "sendBody": True,
                "jsonBody": {"query": "={{ $json.query }}"},
            }
        )
        body_registry = default_node_converter_registry().build_runtime_registry(workflow)
        body_result = await WorkflowExecutionEngine(body_registry).run(workflow)
        assert body_result.terminal_outputs[0].items[0].json_data == {"payload": {"query": "hello"}}

        request_node.parameters["url"] = f"http://127.0.0.1:{port}/fail"
        request_node.parameters["method"] = "GET"
        request_node.parameters["sendBody"] = False
        request_node.parameters.pop("jsonBody")
        failing_registry = default_node_converter_registry().build_runtime_registry(workflow)
        with pytest.raises(NodeExecutionError) as raised:
            await WorkflowExecutionEngine(failing_registry).run(workflow)
        assert isinstance(raised.value.__cause__, ClientResponseError)
        assert raised.value.__cause__.status == 503
    finally:
        await runner.cleanup()


def test_http_request_rejects_enabled_body_without_json_body() -> None:
    """Не отправлять незаданное тело запроса как пустой JSON."""
    workflow = load_workflow("converters_http.json")
    request_node = next(node for node in workflow.nodes if node.name == "Request")
    request_node.parameters["sendBody"] = True

    with pytest.raises(UnsupportedNodeConfigurationError, match="sendBody=true требует jsonBody"):
        default_node_converter_registry().build_runtime_registry(workflow)
