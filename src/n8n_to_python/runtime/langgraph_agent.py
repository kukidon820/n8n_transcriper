"""LangGraph execution for the verified n8n AI Agent + DeepSeek + Calculator subset."""

import ast
import json
import os
from typing import Any, Literal, TypedDict

import aiohttp
from langgraph.graph import END, START, StateGraph
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from ..converters.expressions import N8nExpressionEvaluator
from ..domain.models.workflow import Workflow, WorkflowNode
from .executors import NodeExecutionContext
from .models import NodeResult, WorkflowItem

AI_AGENT_TYPE = "@n8n/n8n-nodes-langchain.agent"
DEEPSEEK_MODEL_TYPE = "@n8n/n8n-nodes-langchain.lmChatDeepSeek"
CALCULATOR_TOOL_TYPE = "@n8n/n8n-nodes-langchain.toolCalculator"
SIMPLE_MEMORY_TYPE = "@n8n/n8n-nodes-langchain.memoryBufferWindow"


class _StrictParameters(BaseModel):
    """Base schema for the n8n AI node parameters implemented by this adapter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _AgentOptions(_StrictParameters):
    system_message: str = Field(default="", alias="systemMessage")
    max_iterations: int = Field(default=6, alias="maxIterations", ge=1, le=20, strict=True)


class _AgentParameters(_StrictParameters):
    prompt_type: Literal["define"] = Field(alias="promptType")
    text: str = Field(min_length=1)
    options: _AgentOptions = Field(default_factory=_AgentOptions)


class _DeepSeekOptions(_StrictParameters):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, alias="topP", gt=0, le=1)
    max_tokens: int | None = Field(default=None, alias="maxTokens", ge=1, strict=True)


class _DeepSeekParameters(_StrictParameters):
    options: _DeepSeekOptions = Field(default_factory=_DeepSeekOptions)


class _MemoryParameters(_StrictParameters):
    session_id_type: Literal["customKey"] = Field(alias="sessionIdType")
    session_key: str = Field(alias="sessionKey", min_length=1)
    context_window_length: int = Field(alias="contextWindowLength", ge=1, le=40, strict=True)


class _DeepSeekRuntimeSettings(BaseModel):
    """Validate runtime-only DeepSeek settings without exposing the key in reprs."""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1)
    base_url: AnyHttpUrl
    model: str = Field(min_length=1)


class _DeepSeekFunctionCall(BaseModel):
    """One tool call returned by the OpenAI-compatible model API."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    arguments: str


class _DeepSeekToolCall(BaseModel):
    """One validated function call in an assistant chat message."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    type: Literal["function"]
    function: _DeepSeekFunctionCall


class _DeepSeekMessage(BaseModel):
    """Supported text-only assistant message returned by the model API."""

    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"]
    content: str | None = None
    tool_calls: list[_DeepSeekToolCall] = Field(default_factory=list)


class _DeepSeekChoice(BaseModel):
    """The selected assistant choice from one model completion."""

    model_config = ConfigDict(extra="allow")

    message: _DeepSeekMessage


class _DeepSeekResponse(BaseModel):
    """Validated subset of the OpenAI-compatible Chat Completions response."""

    model_config = ConfigDict(extra="allow")

    choices: list[_DeepSeekChoice] = Field(min_length=1)


class _CalculatorArguments(BaseModel):
    """Pydantic schema for the expression accepted by Calculator Tool."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    expression: str | None = None

    @model_validator(mode="after")
    def require_one_expression(self) -> "_CalculatorArguments":
        """Require exactly one of the supported argument aliases."""
        if (self.query is None) == (self.expression is None):
            raise ValueError("Укажите ровно один аргумент: query или expression.")
        return self


class _AIAgentState(TypedDict):
    """State for the nested model/tool cycle in the AI Agent node."""

    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    answer: str
    model_calls: int
    next_step: str


class UnsupportedAIAgentConfigurationError(ValueError):
    """Raised when an AI node or one of its connected AI ports is unsupported."""


class LangGraphAIAgentExecutor:
    """Run one verified AI Agent node using an inner LangGraph and aiohttp.

    The model adapter implements the OpenAI-compatible DeepSeek Chat Completions
    request directly with aiohttp so generated workflows do not use a second HTTP
    client library. Calculator calls are evaluated with a small arithmetic AST.
    """

    is_trigger = False

    def __init__(
        self,
        workflow: Workflow,
        agent_node: WorkflowNode,
        *,
        model_node: WorkflowNode,
        tool_node: WorkflowNode | None,
        memory_node: WorkflowNode | None,
    ):
        """Validate this exact AI subgraph and retain only non-secret settings."""
        if agent_node.type != AI_AGENT_TYPE or agent_node.type_version != 2.2:
            raise UnsupportedAIAgentConfigurationError(
                f"AI Agent {agent_node.name!r} поддержан только в версии 2.2."
            )
        if model_node.type != DEEPSEEK_MODEL_TYPE or model_node.type_version != 1:
            raise UnsupportedAIAgentConfigurationError(
                f"Модель {model_node.name!r} должна быть DeepSeek Chat Model v1."
            )
        if tool_node is not None and (
            tool_node.type != CALCULATOR_TOOL_TYPE or tool_node.type_version != 1
        ):
            raise UnsupportedAIAgentConfigurationError(
                f"Инструмент {tool_node.name!r} должен быть Calculator Tool v1."
            )
        if memory_node is not None and (
            memory_node.type != SIMPLE_MEMORY_TYPE or memory_node.type_version != 1.3
        ):
            raise UnsupportedAIAgentConfigurationError(
                f"Память {memory_node.name!r} должна быть Simple Memory v1.3."
            )

        try:
            self._agent_parameters = _AgentParameters.model_validate(agent_node.parameters)
            self._model_parameters = _DeepSeekParameters.model_validate(model_node.parameters)
            self._memory_parameters = (
                _MemoryParameters.model_validate(memory_node.parameters)
                if memory_node is not None
                else None
            )
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors(include_input=False)
            )
            raise UnsupportedAIAgentConfigurationError(
                f"AI Agent {agent_node.name!r} содержит неподдержанные параметры: {details}"
            ) from error

        self.agent_node = agent_node
        self._workflow = workflow
        self._model_node = model_node
        self._tool_node = tool_node
        self._memory_node = memory_node
        self._evaluator = N8nExpressionEvaluator()
        self._tool_name = "calculator" if tool_node is not None else None

    @classmethod
    def from_workflow(
        cls,
        workflow: Workflow,
        agent_node: WorkflowNode,
    ) -> "LangGraphAIAgentExecutor":
        """Resolve and validate the model, tool, and memory AI-port connections."""
        incoming: dict[str, list[WorkflowNode]] = {
            "ai_languageModel": [],
            "ai_tool": [],
            "ai_memory": [],
        }
        node_by_name = {node.name: node for node in workflow.nodes}
        for source_name, connection_types in workflow.connections.items():
            for port_name in incoming:
                for output in connection_types.get(port_name, ()) or ():
                    for connection in output or ():
                        if connection.node == agent_node.name:
                            if connection.type != port_name or connection.index != 0:
                                raise UnsupportedAIAgentConfigurationError(
                                    f"AI-подключение {source_name!r} → {agent_node.name!r} "
                                    "должно использовать соответствующий порт с index=0."
                                )
                            incoming[port_name].append(node_by_name[source_name])

        for source_name, connection_types in workflow.connections.items():
            for port_name, outputs in connection_types.items():
                if port_name.startswith("ai_") and port_name not in incoming:
                    for output in outputs or ():
                        if any(connection.node == agent_node.name for connection in output or ()):
                            raise UnsupportedAIAgentConfigurationError(
                                f"AI-порт {port_name!r} пока не поддерживается."
                            )

        models = incoming["ai_languageModel"]
        tools = incoming["ai_tool"]
        memories = incoming["ai_memory"]
        if len(models) != 1:
            raise UnsupportedAIAgentConfigurationError(
                f"AI Agent {agent_node.name!r} должен иметь ровно одну ai_languageModel связь."
            )
        if len(tools) > 1:
            raise UnsupportedAIAgentConfigurationError(
                "Поддерживается не более одного Calculator Tool на AI Agent."
            )
        if len(memories) > 1:
            raise UnsupportedAIAgentConfigurationError(
                "Поддерживается не более одного Simple Memory на AI Agent."
            )
        executor = cls(
            workflow,
            agent_node,
            model_node=models[0],
            tool_node=tools[0] if tools else None,
            memory_node=memories[0] if memories else None,
        )
        executor.auxiliary_node_names = frozenset(
            node.name for node in [*models, *tools, *memories]
        )
        return executor

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Run the agent once for each input item and add its response as `output`."""
        if node.name != self.agent_node.name:
            raise UnsupportedAIAgentConfigurationError(
                f"AI executor {self.agent_node.name!r} нельзя применить к {node.name!r}."
            )
        runtime_settings = {
            "api_key": context.services.get("deepseek_api_key")
            or os.environ.get("DEEPSEEK_API_KEY"),
            "base_url": context.services.get("deepseek_base_url")
            or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model": context.services.get("deepseek_model")
            or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        }
        try:
            settings = _DeepSeekRuntimeSettings.model_validate(runtime_settings)
        except ValidationError as error:
            raise RuntimeError(
                "Проверьте DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL и DEEPSEEK_MODEL "
                "в окружении или runtime services."
            ) from error
        api_key = settings.api_key.get_secret_value()
        base_url = str(settings.base_url).rstrip("/")
        model_name = settings.model

        output_items: list[WorkflowItem] = []
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item_index, item in enumerate(items):
                prompt = self._evaluator.evaluate(
                    self._agent_parameters.text,
                    item,
                    item_index=item_index,
                    context=context,
                )
                if not isinstance(prompt, str):
                    prompt = str(prompt)
                system_message = self._evaluator.evaluate(
                    self._agent_parameters.options.system_message,
                    item,
                    item_index=item_index,
                    context=context,
                )
                history_key = (
                    self._memory_key(item, item_index, context) if self._memory_parameters else None
                )
                history = (
                    list(context.state.get(history_key, [])) if history_key is not None else []
                )
                messages: list[dict[str, Any]] = []
                if system_message:
                    messages.append({"role": "system", "content": str(system_message)})
                messages.extend(history)
                messages.append({"role": "user", "content": prompt})

                answer = await self._run_agent_graph(
                    session=session,
                    base_url=base_url,
                    api_key=api_key,
                    model_name=model_name,
                    messages=messages,
                )
                if history_key is not None and self._memory_parameters is not None:
                    history.extend(
                        [
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": answer},
                        ]
                    )
                    max_messages = self._memory_parameters.context_window_length * 2
                    context.state[history_key] = history[-max_messages:]
                updated_json = dict(item.json_data)
                updated_json["output"] = answer
                output_items.append(WorkflowItem(json=updated_json, binary=item.binary))
        return NodeResult(outputs=(tuple(output_items),))

    def _memory_key(
        self, item: WorkflowItem, item_index: int, context: NodeExecutionContext
    ) -> str:
        """Resolve a Simple Memory custom session key with the supported expression subset."""
        if self._memory_parameters is None or self._memory_node is None:
            raise RuntimeError("Memory key запрошен для AI Agent без Simple Memory.")
        session_key = self._evaluator.evaluate(
            self._memory_parameters.session_key,
            item,
            item_index=item_index,
            context=context,
        )
        return f"ai-memory:{self._memory_node.name}:{session_key}"

    async def _run_agent_graph(
        self,
        *,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        model_name: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Execute the model/tool loop through LangGraph's asynchronous API."""

        async def call_model(state: _AIAgentState) -> dict[str, Any]:
            if state["model_calls"] >= self._agent_parameters.options.max_iterations:
                raise RuntimeError(
                    f"AI Agent {self.agent_node.name!r} превысил maxIterations="
                    f"{self._agent_parameters.options.max_iterations}."
                )
            response = await self._call_deepseek(
                session=session,
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                messages=state["messages"],
            )
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            next_step = "tools" if tool_calls else "finish"
            answer = message.get("content") or ""
            return {
                "messages": [*state["messages"], message],
                "tool_calls": tool_calls,
                "answer": answer,
                "model_calls": state["model_calls"] + 1,
                "next_step": next_step,
            }

        async def execute_tools(state: _AIAgentState) -> dict[str, Any]:
            messages_after_tools = list(state["messages"])
            for raw_tool_call in state["tool_calls"]:
                try:
                    tool_call = _DeepSeekToolCall.model_validate(raw_tool_call)
                except ValidationError as error:
                    raise RuntimeError("DeepSeek вернул некорректный tool call.") from error
                if tool_call.function.name != self._tool_name or self._tool_node is None:
                    raise RuntimeError(
                        f"AI Agent запросил неподключённый инструмент {tool_call.function.name!r}."
                    )
                arguments_data = json.loads(tool_call.function.arguments)
                try:
                    arguments = _CalculatorArguments.model_validate(arguments_data)
                except ValidationError as error:
                    raise RuntimeError(
                        "Calculator Tool ожидает один строковый аргумент query."
                    ) from error
                expression = arguments.query or arguments.expression
                if expression is None:
                    raise RuntimeError("Calculator Tool не получил арифметическое выражение.")
                calculation = self._calculate(expression)
                messages_after_tools.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(calculation),
                    }
                )
            return {
                "messages": messages_after_tools,
                "tool_calls": [],
                "answer": state["answer"],
                "model_calls": state["model_calls"],
                "next_step": "model",
            }

        def route(state: _AIAgentState) -> str:
            return "execute_tools" if state["next_step"] == "tools" else END

        builder = StateGraph(_AIAgentState)
        builder.add_node("call_model", call_model)
        builder.add_node("execute_tools", execute_tools)
        builder.add_edge(START, "call_model")
        builder.add_conditional_edges(
            "call_model",
            route,
            {"execute_tools": "execute_tools", END: END},
        )
        builder.add_edge("execute_tools", "call_model")
        graph = builder.compile()
        initial_state: _AIAgentState = {
            "messages": messages,
            "tool_calls": [],
            "answer": "",
            "model_calls": 0,
            "next_step": "model",
        }
        final_state = await graph.ainvoke(
            initial_state,
            config={"recursion_limit": self._agent_parameters.options.max_iterations * 2 + 3},
        )
        return final_state["answer"]

    async def _call_deepseek(
        self,
        *,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        model_name: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send one OpenAI-compatible DeepSeek request and validate its response shape."""
        body: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        if self._tool_name is not None:
            body.update(
                {
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": self._tool_name,
                                "description": (
                                    "Calculate a basic arithmetic expression. "
                                    "Pass the expression in the query field."
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "Arithmetic expression, such as 2 + 3 * 4",
                                        }
                                    },
                                    "required": ["query"],
                                    "additionalProperties": False,
                                },
                            },
                        }
                    ],
                    "tool_choice": "auto",
                }
            )
        options = self._model_parameters.options
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if options.top_p is not None:
            body["top_p"] = options.top_p
        if options.max_tokens is not None:
            body["max_tokens"] = options.max_tokens

        endpoint = base_url.rstrip("/") + "/chat/completions"
        try:
            async with session.post(
                endpoint,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as response:
                if response.status >= 400:
                    raise RuntimeError(
                        f"DeepSeek API вернул HTTP {response.status} для AI Agent "
                        f"{self.agent_node.name!r}. Проверьте URL, модель и API key."
                    )
                payload = await response.json()
        except aiohttp.ClientError as error:
            raise RuntimeError(
                f"Не удалось выполнить запрос DeepSeek для AI Agent {self.agent_node.name!r}."
            ) from error
        try:
            validated_response = _DeepSeekResponse.model_validate(payload)
        except ValidationError as error:
            raise RuntimeError(
                "DeepSeek API вернул ответ, который не соответствует Chat Completions schema."
            ) from error
        return validated_response.model_dump(mode="json")

    @staticmethod
    def _calculate(expression: str) -> int | float:
        """Evaluate only numeric literals and basic arithmetic operators."""
        if len(expression) > 256:
            raise ValueError("Длина выражения Calculator Tool ограничена 256 символами.")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as error:
            raise ValueError(f"Некорректное арифметическое выражение: {expression!r}.") from error

        def evaluate(node: ast.AST) -> int | float:
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                if abs(node.value) > 1e100:
                    raise ValueError("Числа Calculator Tool ограничены значением 1e100.")
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand)
                return value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
            ):
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ValueError("Степень калькулятора ограничена значением 100.")
                operation = {
                    ast.Add: lambda: left + right,
                    ast.Sub: lambda: left - right,
                    ast.Mult: lambda: left * right,
                    ast.Div: lambda: left / right,
                    ast.FloorDiv: lambda: left // right,
                    ast.Mod: lambda: left % right,
                    ast.Pow: lambda: left**right,
                }[type(node.op)]
                result = operation()
                if abs(result) > 1e100:
                    raise ValueError("Результат Calculator Tool ограничен значением 1e100.")
                return result
            raise ValueError(
                "Calculator Tool разрешает только числа, скобки и арифметические операции."
            )

        value = evaluate(tree)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value


def build_langgraph_ai_executors(
    workflow: Workflow,
) -> tuple[dict[str, LangGraphAIAgentExecutor], frozenset[str]]:
    """Create validated AI Agent executors and list their checked auxiliary nodes."""
    executors: dict[str, LangGraphAIAgentExecutor] = {}
    auxiliary_names: set[str] = set()
    for node in workflow.nodes:
        if node.type != AI_AGENT_TYPE:
            continue
        executor = LangGraphAIAgentExecutor.from_workflow(workflow, node)
        executors[node.name] = executor
        auxiliary_names.update(executor.auxiliary_node_names)
    return executors, frozenset(auxiliary_names)
