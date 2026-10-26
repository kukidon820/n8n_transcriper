"""Проверки runtime на небольших локальных графах и node-adapter-ах."""

from pathlib import Path

import pytest

from n8n_to_python.domain.models.workflow import Workflow, WorkflowNode
from n8n_to_python.runtime.engine import RuntimeSettings, WorkflowExecutionEngine
from n8n_to_python.runtime.errors import (
    ExecutionLimitExceededError,
    NodeExecutionError,
    NoTriggerNodesError,
    UnsupportedNodeTypeError,
)
from n8n_to_python.runtime.executors import (
    NodeExecutionContext,
    NodeExecutor,
    NodeExecutorRegistry,
)
from n8n_to_python.runtime.models import NodeResult, WorkflowItem

FIXTURES = Path(__file__).parent / "fixtures"


def load_workflow(filename: str) -> Workflow:
    """Загрузить минимальный JSON-граф из локальной папки fixtures."""
    return Workflow.model_validate_json((FIXTURES / filename).read_bytes())


def test_runtime_item_validates_n8n_json_field_alias() -> None:
    """Pydantic принимает экспортное поле `json` без конфликта с BaseModel.json."""
    item = WorkflowItem.model_validate({"json": {"value": 5}, "pairedItem": {"item": 0}})

    assert item.json_data == {"value": 5}
    assert item.model_dump(by_alias=True)["json"] == {"value": 5}
    assert item.pairedItem == {"item": 0}


class FixtureTriggerExecutor:
    """Одноразовый trigger-адаптер для проверки запуска графа."""

    is_trigger = True

    def __init__(self, item: WorkflowItem | None = None):
        """Задать item, который trigger передаст первому узлу графа."""
        self.item = item or WorkflowItem(json={"count": 0, "value": "start"})

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Вернуть начальный item на единственный выход trigger-узла."""
        assert not items
        assert context.input_port is None
        return NodeResult(outputs=((self.item,),))


class FixturePassExecutor:
    """Передать items дальше без изменений."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Вернуть входные items на выход 0."""
        return NodeResult(outputs=(items,))


class FixtureBranchExecutor:
    """Разделить один пакет items на два выхода для теста ветвления."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Отправить один и тот же входной пакет по обоим выходам."""
        return NodeResult(outputs=(items, items))


class FixtureMutationExecutor:
    """Изменить item в ветке, чтобы проверить изоляцию при fan-out."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Добавить значение ветки и передать скопированный пакет дальше."""
        branch_value = node.parameters["value"]
        modified = tuple(
            WorkflowItem(json_data={**item.json_data, "branch": branch_value}, binary=item.binary)
            for item in items
        )
        return NodeResult(outputs=(modified,))


class FixtureIncrementExecutor:
    """Увеличить счётчик item в теле циклического графа."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Создать новые items, увеличив поле `count` на единицу."""
        incremented = tuple(
            WorkflowItem(json_data={**item.json_data, "count": item.json_data["count"] + 1})
            for item in items
        )
        return NodeResult(outputs=(incremented,))


class FixtureLoopExecutor:
    """Направить item в тело цикла до счётчика 3, затем на выход done."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Выбрать выход тела цикла либо его выход завершения."""
        if items[0].json_data["count"] < 3:
            return NodeResult(outputs=(items, None))
        return NodeResult(outputs=(None, items))


class FixtureInfiniteLoopExecutor:
    """Всегда выбирать выход повторения для проверки защитного лимита."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Возвращать item на выход тела цикла без условия остановки."""
        return NodeResult(outputs=(items, None))


class FixtureFailureExecutor:
    """Завершить выполнение ошибкой для проверки проброса причины."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Поднять исходную ошибку из node-адаптера."""
        raise ValueError("fixture node failed")


def make_registry(
    *,
    trigger: FixtureTriggerExecutor | None = None,
    pass_through: FixturePassExecutor | None = None,
    branch: FixtureBranchExecutor | None = None,
    mutation: FixtureMutationExecutor | None = None,
    increment: FixtureIncrementExecutor | None = None,
    loop: FixtureLoopExecutor | FixtureInfiniteLoopExecutor | None = None,
    failure: FixtureFailureExecutor | None = None,
) -> NodeExecutorRegistry:
    """Собрать реестр небольших исполняемых обработчиков для тестового графа."""
    executors: dict[str, NodeExecutor] = {
        "fixture.pass": pass_through or FixturePassExecutor(),
    }
    if trigger is not None:
        executors["fixture.trigger"] = trigger
    if branch is not None:
        executors["fixture.branch"] = branch
    if mutation is not None:
        executors["fixture.mutate"] = mutation
    if increment is not None:
        executors["fixture.increment"] = increment
    if loop is not None:
        executors["fixture.loop"] = loop
    if failure is not None:
        executors["fixture.fail"] = failure
    return NodeExecutorRegistry(executors)


@pytest.mark.asyncio
async def test_runtime_runs_triggered_linear_graph_and_returns_terminal_items() -> None:
    """Линейный граф выполняется по FIFO, а конечные items доступны в результате."""
    engine = WorkflowExecutionEngine(make_registry(trigger=FixtureTriggerExecutor()))

    result = await engine.run(load_workflow("runtime_linear.json"))

    assert [record.node_name for record in result.executions] == [
        "Trigger",
        "Transform",
        "Finish",
    ]
    assert result.terminal_outputs[0].node_name == "Finish"
    assert result.terminal_outputs[0].items[0].json_data == {"count": 0, "value": "start"}


@pytest.mark.asyncio
async def test_runtime_routes_branch_outputs_to_the_matching_nodes() -> None:
    """Индекс выхода задаёт ветку, а входной порт назначения передаётся адаптеру."""
    engine = WorkflowExecutionEngine(
        make_registry(
            trigger=FixtureTriggerExecutor(),
            branch=FixtureBranchExecutor(),
        )
    )

    result = await engine.run(load_workflow("runtime_branching.json"))

    assert [record.node_name for record in result.executions] == [
        "Trigger",
        "Branch",
        "Left",
        "Right",
    ]
    assert [output.node_name for output in result.terminal_outputs] == ["Left", "Right"]
    assert result.executions[2].input_port is not None
    assert result.executions[2].input_port.type == "main"
    assert result.executions[2].input_port.index == 0


@pytest.mark.asyncio
async def test_runtime_copies_items_for_fanout_branch_isolation() -> None:
    """Мутация item в одной ветке не меняет данные соседней ветки."""
    trigger = FixtureTriggerExecutor(WorkflowItem(json={"nested": {"value": "start"}}))
    engine = WorkflowExecutionEngine(
        make_registry(trigger=trigger, mutation=FixtureMutationExecutor())
    )

    result = await engine.run(load_workflow("runtime_fanout.json"))

    assert [output.items[0].json_data["branch"] for output in result.terminal_outputs] == [
        "left",
        "right",
    ]
    assert trigger.item.json_data == {"nested": {"value": "start"}}


@pytest.mark.asyncio
async def test_runtime_executes_loop_back_edges_until_done_output() -> None:
    """Обратное ребро запускает новое async-выполнение до выхода done."""
    engine = WorkflowExecutionEngine(
        make_registry(
            trigger=FixtureTriggerExecutor(),
            increment=FixtureIncrementExecutor(),
            loop=FixtureLoopExecutor(),
        )
    )

    result = await engine.run(load_workflow("runtime_loop.json"))

    assert [record.node_name for record in result.executions].count("Loop") == 4
    assert [record.node_name for record in result.executions].count("Body") == 3
    assert result.terminal_outputs[0].node_name == "Done"
    assert result.terminal_outputs[0].items[0].json_data["count"] == 3


@pytest.mark.asyncio
async def test_runtime_rejects_workflow_without_registered_trigger() -> None:
    """Workflow без trigger не запускается неявно от произвольного узла."""
    engine = WorkflowExecutionEngine(make_registry())

    with pytest.raises(NoTriggerNodesError, match="не содержит узла"):
        await engine.run(load_workflow("runtime_no_trigger.json"))


@pytest.mark.asyncio
async def test_runtime_fails_when_executed_node_has_no_registered_executor() -> None:
    """Незарегистрированный тип не пропускается молча во время запуска."""
    registry = NodeExecutorRegistry({"fixture.trigger": FixtureTriggerExecutor()})
    engine = WorkflowExecutionEngine(registry)

    with pytest.raises(UnsupportedNodeTypeError, match="Transform"):
        await engine.run(load_workflow("runtime_linear.json"))


@pytest.mark.asyncio
async def test_runtime_wraps_node_failure_and_keeps_original_cause() -> None:
    """Ошибка узла останавливает граф и доступна как причина NodeExecutionError."""
    engine = WorkflowExecutionEngine(
        make_registry(
            trigger=FixtureTriggerExecutor(),
            failure=FixtureFailureExecutor(),
        )
    )
    workflow = load_workflow("runtime_error.json")

    with pytest.raises(NodeExecutionError, match="Fail") as raised:
        await engine.run(workflow)

    assert isinstance(raised.value.__cause__, ValueError)
    assert str(raised.value.__cause__) == "fixture node failed"


@pytest.mark.asyncio
async def test_runtime_stops_cycles_at_configured_execution_limit() -> None:
    """Защитный лимит прерывает незавершающийся графовый цикл."""
    engine = WorkflowExecutionEngine(
        make_registry(
            trigger=FixtureTriggerExecutor(),
            increment=FixtureIncrementExecutor(),
            loop=FixtureInfiniteLoopExecutor(),
        ),
        settings=RuntimeSettings(max_node_executions=3),
    )

    with pytest.raises(ExecutionLimitExceededError, match="лимит в 3"):
        await engine.run(load_workflow("runtime_loop.json"))
