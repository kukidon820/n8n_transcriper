"""LangGraph backend for the supported single-active-path n8n runtime subset."""

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..domain.models.workflow import Workflow, WorkflowNode
from .errors import (
    ExecutionLimitExceededError,
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


class LangGraphWorkflowState(TypedDict):
    """Mutable state passed between LangGraph nodes during one workflow run."""

    current_items: tuple[WorkflowItem, ...]
    input_port: RuntimeInputPort | None
    invocation_counts: dict[str, int]
    run_state: dict[str, Any]
    services: dict[str, Any]
    executions: list[NodeExecutionRecord]
    terminal_outputs: list[TerminalOutput]
    next_node_name: str | None


class UnsupportedLangGraphWorkflowError(ValueError):
    """Raised when the graph cannot preserve a workflow's active routing semantics."""


class LangGraphExecutionEngine:
    """Execute supported n8n main-flow transitions as LangGraph nodes and edges.

    Each n8n node becomes a LangGraph node. Conditional edges follow the active
    output selected by the node executor. The current backend deliberately rejects
    simultaneous fan-out because a correct join/reducer contract is not implemented.
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
        if len(trigger_names) != 1:
            raise UnsupportedLangGraphWorkflowError(
                "LangGraph backend пока запускает по одному trigger-узлу за раз. "
                "Передайте initial_items только для нужного trigger."
            )

        runnable_names = self._reachable_node_names(plan)
        graph_keys = {name: f"n8n_node_{index}" for index, name in enumerate(runnable_names)}
        builder = StateGraph(LangGraphWorkflowState)

        async def select_trigger(state: LangGraphWorkflowState) -> dict[str, Any]:
            trigger_name = trigger_names[0]
            return {
                "next_node_name": trigger_name,
                "current_items": tuple(start_items.get(trigger_name, ())),
                "input_port": None,
            }

        builder.add_node("select_trigger", select_trigger)
        builder.add_edge(START, "select_trigger")
        builder.add_conditional_edges(
            "select_trigger",
            lambda state: state["next_node_name"],
            graph_keys,
        )

        for node_name in runnable_names:
            node = plan.nodes_by_name[node_name]
            executor = self._registry.get(node)
            builder.add_node(
                graph_keys[node_name],
                self._build_node_function(node, executor, plan, graph_keys),
            )
            builder.add_conditional_edges(
                graph_keys[node_name],
                lambda state: state["next_node_name"] or "__end__",
                {**graph_keys, "__end__": END},
            )

        graph = builder.compile()
        initial_state: LangGraphWorkflowState = {
            "current_items": (),
            "input_port": None,
            "invocation_counts": {},
            "run_state": {},
            "services": dict(services or {}),
            "executions": [],
            "terminal_outputs": [],
            "next_node_name": None,
        }
        final_state = await graph.ainvoke(
            initial_state,
            config={"recursion_limit": self._max_node_executions + 10},
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
        graph_keys: Mapping[str, str],
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

            next_edges: list[tuple[int, tuple[WorkflowItem, ...], str, RuntimeInputPort]] = []
            terminal_outputs = list(state["terminal_outputs"])
            terminal_candidates: list[tuple[int, tuple[WorkflowItem, ...]]] = []
            for output_index, batch in enumerate(result.outputs):
                if not batch:
                    continue
                targets = plan.outgoing.get((node.name, output_index), ())
                if not targets:
                    terminal_candidates.append((output_index, self._copy_items(batch)))
                    continue
                for target in targets:
                    next_edges.append(
                        (
                            output_index,
                            self._copy_items(batch),
                            target.target_node,
                            target.target_input,
                        )
                    )

            if len(next_edges) + len(terminal_candidates) > 1:
                raise UnsupportedLangGraphWorkflowError(
                    f"Узел {node.name!r} активировал несколько переходов. "
                    "LangGraph backend пока поддерживает один активный main-переход "
                    "за вызов; fan-out и синхронизация нескольких ветвей не реализованы."
                )

            update: dict[str, Any] = {
                "invocation_counts": counts,
                "run_state": state["run_state"],
                "executions": executions,
                "terminal_outputs": terminal_outputs,
                "next_node_name": None,
            }
            if terminal_candidates:
                output_index, output_items = terminal_candidates[0]
                terminal_outputs.append(
                    TerminalOutput(
                        node_name=node.name,
                        output_index=output_index,
                        items=output_items,
                    )
                )
                update["terminal_outputs"] = terminal_outputs
            elif next_edges:
                _, output_items, target_name, target_input = next_edges[0]
                update.update(
                    {
                        "next_node_name": target_name,
                        "current_items": output_items,
                        "input_port": target_input,
                    }
                )
            return update

        return execute_node

    @staticmethod
    def _reachable_node_names(plan: ExecutionPlan) -> tuple[str, ...]:
        """Return nodes reachable from a trigger through any declared main output."""
        found: set[str] = set()
        pending = list(plan.trigger_names)
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
