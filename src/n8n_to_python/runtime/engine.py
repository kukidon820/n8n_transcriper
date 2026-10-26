"""Детерминированный асинхронный движок выполнения графа n8n."""

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ..domain.models.workflow import Workflow
from .errors import (
    ExecutionLimitExceededError,
    IncompleteMergeError,
    InvalidNodeOutputError,
    NodeExecutionError,
)
from .executors import NodeExecutionContext, NodeExecutorRegistry
from .models import (
    ExecutionResult,
    NodeExecutionRecord,
    NodeResult,
    RuntimeInputPort,
    TerminalOutput,
    WorkflowItem,
)
from .planner import ExecutionPlanner


class RuntimeSettings(BaseModel):
    """Ограничения безопасного выполнения workflow."""

    max_node_executions: int = Field(default=10_000, gt=0, strict=True)


@dataclass(frozen=True, slots=True)
class _Invocation:
    """Одна постановка узла в очередь выполнения."""

    node_name: str
    items: tuple[WorkflowItem, ...]
    input_port: RuntimeInputPort | None


class WorkflowExecutionEngine:
    """Запускает trigger-узлы и обходит переходы `main` с FIFO-очередью."""

    def __init__(
        self,
        registry: NodeExecutorRegistry,
        settings: RuntimeSettings | None = None,
        planner: ExecutionPlanner | None = None,
    ):
        """Сохранить реестр обработчиков, лимит вызовов и планировщик."""
        self._registry = registry
        self._settings = settings or RuntimeSettings()
        self._planner = planner or ExecutionPlanner()

    async def run(
        self,
        workflow: Workflow,
        *,
        initial_items: Mapping[str, Sequence[WorkflowItem]] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        """Выполнить trigger-узлы с опциональными входными данными и адаптерами."""
        plan = self._planner.build(workflow, self._registry)
        start_items = initial_items or {}
        runtime_services = services or {}
        trigger_names = plan.trigger_names
        if initial_items is not None:
            unknown = set(initial_items) - set(trigger_names)
            if unknown:
                raise ValueError(f"initial_items ссылается не на trigger-узлы: {sorted(unknown)}")
            trigger_names = tuple(name for name in trigger_names if name in initial_items)
            if not trigger_names:
                raise ValueError("При передаче initial_items укажите имя trigger-узла.")
        queue = deque(
            _Invocation(
                node_name=name,
                items=tuple(start_items.get(name, ())),
                input_port=None,
            )
            for name in trigger_names
        )
        invocation_counts: Counter[str] = Counter()
        executions: list[NodeExecutionRecord] = []
        terminal_outputs: list[TerminalOutput] = []
        total_invocations = 0
        run_state: dict[str, Any] = {}

        while queue:
            if total_invocations >= self._settings.max_node_executions:
                raise ExecutionLimitExceededError(
                    f"Workflow превысил лимит в {self._settings.max_node_executions} вызовов узлов."
                )

            invocation = queue.popleft()
            total_invocations += 1
            node = plan.nodes_by_name[invocation.node_name]
            executor = self._registry.get(node)
            invocation_counts[node.name] += 1
            input_snapshot = self._copy_items(invocation.items)
            execution_context = NodeExecutionContext(
                input_port=invocation.input_port,
                invocation_number=invocation_counts[node.name],
                state=run_state,
                services=runtime_services,
            )

            try:
                result = await executor.execute(
                    node,
                    self._copy_items(invocation.items),
                    execution_context,
                )
            except Exception as error:
                raise NodeExecutionError(node.name) from error

            if not isinstance(result, NodeResult):
                raise InvalidNodeOutputError(
                    f"Узел {node.name!r} должен вернуть NodeResult, "
                    f"получено {type(result).__name__}."
                )

            expected_slots = plan.output_slot_counts.get(node.name, 0)
            if len(result.outputs) < expected_slots:
                raise InvalidNodeOutputError(
                    f"Узел {node.name!r} подключён к {expected_slots} выходам `main`, "
                    f"но вернул только {len(result.outputs)}."
                )

            executions.append(
                NodeExecutionRecord(
                    node_name=node.name,
                    input_port=invocation.input_port,
                    input_items=input_snapshot,
                    output_item_counts=tuple(
                        None if batch is None else len(batch) for batch in result.outputs
                    ),
                )
            )

            for output_index, batch in enumerate(result.outputs):
                if batch is None:
                    continue

                targets = plan.outgoing.get((node.name, output_index), ())
                if not targets:
                    if not batch:
                        continue
                    terminal_outputs.append(
                        TerminalOutput(
                            node_name=node.name,
                            output_index=output_index,
                            items=self._copy_items(batch),
                        )
                    )
                    continue

                for target in targets:
                    queue.append(
                        _Invocation(
                            node_name=target.target_node,
                            items=self._copy_items(batch),
                            input_port=target.target_input,
                        )
                    )

        incomplete_merges = {
            key: value
            for key, value in run_state.items()
            if key.startswith("merge-append:") and isinstance(value, dict)
        }
        if incomplete_merges:
            pending = "; ".join(
                f"{key.removeprefix('merge-append:')}: missing input ports "
                f"{sorted(set(range(value['expected'])) - set(value['received']))} "
                f"(received {sorted(value['received'])})"
                for key, value in incomplete_merges.items()
            )
            raise IncompleteMergeError(f"Merge append завершился без всех входов: {pending}.")

        return ExecutionResult(
            executions=tuple(executions),
            terminal_outputs=tuple(terminal_outputs),
        )

    @staticmethod
    def _copy_items(items: Sequence[WorkflowItem]) -> tuple[WorkflowItem, ...]:
        """Изолировать mutable JSON каждого потребителя при fan-out ветвлении."""
        return tuple(item.model_copy(deep=True) for item in items)
