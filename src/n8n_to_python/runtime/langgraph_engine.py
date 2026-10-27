"""LangGraph-backed execution of n8n main-flow graphs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..domain.models.workflow import Workflow, WorkflowNode
from .engine import WorkflowExecutionEngine
from .errors import (
    ExecutionLimitExceededError,
    IncompleteMergeError,
    InvalidNodeOutputError,
    NodeExecutionError,
    NoTriggerNodesError,
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
from .planner import ExecutionPlan, ExecutionPlanner


@dataclass(frozen=True, slots=True)
class _Invocation:
    """One scheduled call to an n8n node."""

    node_name: str
    items: tuple[WorkflowItem, ...]
    input_port: RuntimeInputPort | None


class LangGraphWorkflowState(TypedDict):
    """Mutable state passed between LangGraph nodes during one workflow run."""

    current_items: tuple[WorkflowItem, ...]
    input_port: RuntimeInputPort | None
    current_node_name: str | None
    pending_invocations: tuple[_Invocation, ...]
    invocation_counts: dict[str, int]
    run_state: dict[str, Any]
    node_outputs: dict[str, list[WorkflowItem]]
    services: dict[str, Any]
    executions: list[NodeExecutionRecord]
    terminal_outputs: list[TerminalOutput]


class LangGraphExecutionEngine:
    """Execute n8n main-flow transitions with asynchronous LangGraph nodes.

    A dispatcher schedules one n8n node invocation at a time in FIFO order. This
    keeps branch fan-out deterministic and lets stateful join nodes such as Merge
    receive every input before their output is scheduled.
    """

    def __init__(
        self,
        registry: NodeExecutorRegistry,
        *,
        max_node_executions: int = 10_000,
        planner: ExecutionPlanner | None = None,
    ):
        """Save the validated executor registry and execution bound."""
        if max_node_executions <= 0:
            raise ValueError("max_node_executions должен быть положительным.")
        self._registry = registry
        self._max_node_executions = max_node_executions
        self._planner = planner or ExecutionPlanner()

    async def run(
        self,
        workflow: Workflow,
        *,
        initial_items: Mapping[str, Sequence[WorkflowItem]] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        """Build and invoke the LangGraph for one validated n8n workflow."""
        plan = self._planner.build(workflow, self._registry)
        trigger_names = plan.trigger_names
        start_items = initial_items or {}
        if initial_items is not None:
            unknown = set(initial_items) - set(trigger_names)
            if unknown:
                raise ValueError(f"initial_items ссылается не на trigger-узлы: {sorted(unknown)}")
            trigger_names = tuple(name for name in trigger_names if name in initial_items)
            if not trigger_names:
                raise ValueError("При передаче initial_items укажите имя trigger-узла.")
        if not trigger_names:
            raise NoTriggerNodesError("Workflow не содержит доступного trigger-узла.")

        runnable_names = self._reachable_node_names(plan, trigger_names)
        graph_keys = {name: f"n8n_node_{index}" for index, name in enumerate(runnable_names)}
        builder = StateGraph(LangGraphWorkflowState)

        async def dispatch(state: LangGraphWorkflowState) -> dict[str, Any]:
            """Select the next FIFO invocation or finish after checking joins."""
            pending = list(state["pending_invocations"])
            if not pending:
                self._raise_if_incomplete_merges(state["run_state"])
                return {"current_node_name": None}

            invocation = pending.pop(0)
            return {
                "pending_invocations": tuple(pending),
                "current_node_name": invocation.node_name,
                "current_items": self._copy_items(invocation.items),
                "input_port": invocation.input_port,
            }

        builder.add_node("dispatch", dispatch)
        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges(
            "dispatch",
            lambda state: state["current_node_name"] or "__end__",
            {**graph_keys, "__end__": END},
        )

        for node_name in runnable_names:
            node = plan.nodes_by_name[node_name]
            executor = self._registry.get(node)
            builder.add_node(
                graph_keys[node_name],
                self._build_node_function(node, executor, plan),
            )
            builder.add_edge(graph_keys[node_name], "dispatch")

        graph = builder.compile()
        initial_state: LangGraphWorkflowState = {
            "current_items": (),
            "input_port": None,
            "current_node_name": None,
            "pending_invocations": tuple(
                _Invocation(
                    node_name=name,
                    items=tuple(start_items.get(name, ())),
                    input_port=None,
                )
                for name in trigger_names
            ),
            "invocation_counts": {},
            "run_state": {},
            "node_outputs": {},
            "services": dict(services or {}),
            "executions": [],
            "terminal_outputs": [],
        }
        final_state = await graph.ainvoke(
            initial_state,
            config={"recursion_limit": self._max_node_executions * 2 + 10},
        )
        return ExecutionResult(
            executions=tuple(final_state["executions"]),
            terminal_outputs=tuple(final_state["terminal_outputs"]),
        )

    def _build_node_function(
        self,
        node: WorkflowNode,
        executor: Any,
        plan: ExecutionPlan,
    ):
        """Create the asynchronous LangGraph node that runs one n8n executor."""

        async def execute_node(state: LangGraphWorkflowState) -> dict[str, Any]:
            executions = list(state["executions"])
            if len(executions) >= self._max_node_executions:
                raise ExecutionLimitExceededError(
                    f"Workflow превысил лимит в {self._max_node_executions} вызовов узлов."
                )

            counts = dict(state["invocation_counts"])
            counts[node.name] = counts.get(node.name, 0) + 1
            items = self._copy_items(state["current_items"])
            context = NodeExecutionContext(
                input_port=state["input_port"],
                invocation_number=counts[node.name],
                state=state["run_state"],
                services=state["services"],
                input_items=items,
                node_outputs={
                    name: tuple(values) for name, values in state["node_outputs"].items()
                },
            )
            try:
                result = await executor.execute(node, items, context)
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
            paired_indexes = WorkflowExecutionEngine._paired_input_indexes(result, items, node.name)
            executions.append(
                NodeExecutionRecord(
                    node_name=node.name,
                    input_port=state["input_port"],
                    input_items=items,
                    output_item_counts=tuple(
                        None if batch is None else len(batch) for batch in result.outputs
                    ),
                )
            )

            pending = list(state["pending_invocations"])
            terminal_outputs = list(state["terminal_outputs"])
            node_outputs = {name: list(values) for name, values in state["node_outputs"].items()}
            for output_index, batch in enumerate(result.outputs):
                if batch is None:
                    continue
                history = node_outputs.setdefault(node.name, [])
                history_offset = len(history)
                linked_batch: list[WorkflowItem] = []
                for item_index, item in enumerate(batch):
                    parent_index = paired_indexes[output_index][item_index]
                    lineage = dict(item.lineage)
                    if parent_index is not None:
                        lineage = {**items[parent_index].lineage, **lineage}
                    lineage[node.name] = history_offset + item_index
                    linked_batch.append(item.model_copy(update={"lineage": lineage}))
                linked_items = tuple(linked_batch)
                history.extend(linked_items)
                if not linked_items:
                    continue

                targets = plan.outgoing.get((node.name, output_index), ())
                if not targets:
                    terminal_outputs.append(
                        TerminalOutput(
                            node_name=node.name,
                            output_index=output_index,
                            items=self._copy_items(linked_items),
                        )
                    )
                    continue

                for target in targets:
                    pending.append(
                        _Invocation(
                            node_name=target.target_node,
                            items=self._copy_items(linked_items),
                            input_port=target.target_input,
                        )
                    )

            return {
                "invocation_counts": counts,
                "run_state": state["run_state"],
                "node_outputs": node_outputs,
                "executions": executions,
                "terminal_outputs": terminal_outputs,
                "pending_invocations": tuple(pending),
                "current_node_name": None,
            }

        return execute_node

    @staticmethod
    def _raise_if_incomplete_merges(run_state: Mapping[str, Any]) -> None:
        """Fail when the graph ends while a Merge node is still waiting for inputs."""
        incomplete_merges = {
            key: value
            for key, value in run_state.items()
            if key.startswith("merge-append:") and isinstance(value, dict)
        }
        if not incomplete_merges:
            return

        pending = "; ".join(
            f"{key.removeprefix('merge-append:')}: missing input ports "
            f"{sorted(set(range(value['expected'])) - set(value['received']))} "
            f"(received {sorted(value['received'])})"
            for key, value in incomplete_merges.items()
        )
        raise IncompleteMergeError(f"Merge append завершился без всех входов: {pending}.")

    @staticmethod
    def _reachable_node_names(
        plan: ExecutionPlan,
        trigger_names: Sequence[str],
    ) -> tuple[str, ...]:
        """Return nodes reachable from the selected triggers through `main` edges."""
        found: set[str] = set()
        pending = list(trigger_names)
        while pending:
            name = pending.pop()
            if name in found:
                continue
            found.add(name)
            for (source_name, _), connections in plan.outgoing.items():
                if source_name == name:
                    pending.extend(edge.target_node for edge in connections)
        return tuple(node.name for node in plan.workflow.nodes if node.name in found)

    @staticmethod
    def _copy_items(items: Sequence[WorkflowItem]) -> tuple[WorkflowItem, ...]:
        """Copy mutable item payloads before passing them between graph nodes."""
        return tuple(item.model_copy(deep=True) for item in items)
