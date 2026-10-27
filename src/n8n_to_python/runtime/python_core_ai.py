"""One-request OpenAI-compatible AI calls for the standalone Python Core runtime."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)

from ..converters.errors import UnsupportedNodeConfigurationError
from ..converters.expressions import N8nExpressionEvaluator
from ..domain.models.workflow import Workflow, WorkflowNode
from .ai_types import (
    AI_AGENT_TYPE,
    AI_AUXILIARY_TYPES,
    AI_MODEL_TYPES,
    CALCULATOR_TOOL_TYPE,
    DEEPSEEK_MODEL_TYPE,
    OPENAI_MODEL_TYPE,
    SIMPLE_MEMORY_TYPE,
)
from .executors import NodeExecutionContext
from .models import NodeResult, WorkflowItem


class _StrictAIParameters(BaseModel):
    """Reject n8n AI settings whose behavior this simple adapter does not implement."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _AgentOptions(_StrictAIParameters):
    system_message: str = Field(default="", alias="systemMessage")
    max_iterations: int = Field(
        default=10,
        alias="maxIterations",
        ge=1,
        strict=True,
        description="Parsed from n8n but unused because Python Core makes a single API call.",
    )
    enable_streaming: Literal[False] = Field(default=False, alias="enableStreaming")


class PythonCoreAIAgentParameters(_StrictAIParameters):
    """Supported prompt settings for a single request to a connected chat model."""

    prompt_type: Literal["define"] = Field(alias="promptType")
    text: str = Field(min_length=1)
    options: _AgentOptions = Field(default_factory=_AgentOptions)
    has_output_parser: Literal[False] = Field(default=False, alias="hasOutputParser")


class _ChatModelOptions(_StrictAIParameters):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, alias="topP", gt=0, le=1)
    max_tokens: int | None = Field(default=None, alias="maxTokens", ge=1, strict=True)
    timeout: int | None = Field(default=None, ge=1, strict=True)
    max_retries: int | None = Field(default=None, alias="maxRetries", ge=0, strict=True)
    base_url: AnyHttpUrl | None = Field(default=None, alias="baseURL")
    frequency_penalty: float | None = Field(default=None, alias="frequencyPenalty", ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, alias="presencePenalty", ge=-2, le=2)


class DeepSeekChatModelParameters(_StrictAIParameters):
    """Supported DeepSeek Chat Model parameters."""

    model: str | dict[str, Any] | None = None
    options: _ChatModelOptions = Field(default_factory=_ChatModelOptions)


class OpenAIChatModelParameters(_StrictAIParameters):
    """Supported OpenAI Chat Model parameters."""

    model: str | dict[str, Any] = "gpt-4o-mini"
    options: _ChatModelOptions = Field(default_factory=_ChatModelOptions)
    responses_api_enabled: bool | None = Field(default=None, alias="responsesApiEnabled")


class _ChatRuntimeSettings(BaseModel):
    """Validate credentials and the endpoint without exposing secrets in errors."""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1)
    base_url: AnyHttpUrl
    model: str = Field(min_length=1)


class _AssistantMessage(BaseModel):
    """The text-only assistant response accepted by Python Core."""

    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"]
    content: str | None


class _ChatChoice(BaseModel):
    """One choice returned by the Chat Completions endpoint."""

    model_config = ConfigDict(extra="allow")

    message: _AssistantMessage


class _ChatCompletionsResponse(BaseModel):
    """Validated subset of an OpenAI-compatible response."""

    model_config = ConfigDict(extra="allow")

    choices: list[_ChatChoice] = Field(min_length=1)


class UnsupportedPythonCoreAIConfigurationError(UnsupportedNodeConfigurationError):
    """The workflow asks Python Core to perform an unsupported AI-agent feature."""


class UnboundAIComponentExecutor:
    """Placeholder used until a model is bound to an AI Agent at workflow scope."""

    is_trigger = False

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        del items, context
        raise UnsupportedPythonCoreAIConfigurationError(
            f"AI-узел {node.name!r} запускается только как часть связанного AI Agent."
        )


class PythonCoreAIAgentExecutor:
    """Send one text-only Chat Completions request per input item, with no agent loop."""

    is_trigger = False

    def __init__(
        self,
        agent_node: WorkflowNode,
        model_node: WorkflowNode,
        provider: Literal["openai", "deepseek"],
        agent_parameters: PythonCoreAIAgentParameters,
        model_parameters: DeepSeekChatModelParameters | OpenAIChatModelParameters,
    ) -> None:
        self.agent_node = agent_node
        self._model_node = model_node
        self._provider = provider
        self._agent_parameters = agent_parameters
        self._model_parameters = model_parameters
        self._expressions = N8nExpressionEvaluator()

    @classmethod
    def from_workflow(
        cls,
        workflow: Workflow,
        agent_node: WorkflowNode,
    ) -> "PythonCoreAIAgentExecutor":
        """Resolve one supported model connection and reject tools, memory, or extra AI ports."""
        if agent_node.type != AI_AGENT_TYPE or agent_node.type_version != 2.2:
            raise UnsupportedPythonCoreAIConfigurationError(
                f"AI Agent поддержан в Python Core только в версии 2.2; "
                f"узел {agent_node.name!r} имеет версию {agent_node.type_version}."
            )

        connected: dict[str, list[WorkflowNode]] = {
            "ai_languageModel": [],
            "ai_tool": [],
            "ai_memory": [],
        }
        node_by_name = {node.name: node for node in workflow.nodes}
        for source_name, connection_types in workflow.connections.items():
            for port_type, output_groups in connection_types.items():
                if not port_type.startswith("ai_"):
                    continue
                for output_group in output_groups or ():
                    for connection in output_group or ():
                        if connection.node != agent_node.name:
                            continue
                        if port_type not in connected:
                            raise UnsupportedPythonCoreAIConfigurationError(
                                f"AI Agent {agent_node.name!r} использует неподдержанный "
                                f"AI-порт {port_type!r}. Python Core делает только один "
                                "вызов модели без agent tools и memory."
                            )
                        if connection.type != port_type or connection.index != 0:
                            raise UnsupportedPythonCoreAIConfigurationError(
                                f"AI-подключение {source_name!r} → {agent_node.name!r} "
                                "должно использовать соответствующий порт с index=0."
                            )
                        connected[port_type].append(node_by_name[source_name])

        if connected["ai_tool"] or connected["ai_memory"]:
            unsupported = [
                *[node.name for node in connected["ai_tool"]],
                *[node.name for node in connected["ai_memory"]],
            ]
            raise UnsupportedPythonCoreAIConfigurationError(
                f"AI Agent {agent_node.name!r} подключает tools или memory "
                f"({', '.join(unsupported)}). Python Core отправляет один запрос к API и "
                "не выполняет инструменты, циклы агента или память; для них выберите LangGraph."
            )

        models = connected["ai_languageModel"]
        if len(models) != 1:
            raise UnsupportedPythonCoreAIConfigurationError(
                f"AI Agent {agent_node.name!r} должен иметь ровно одну подключённую "
                "DeepSeek Chat Model или OpenAI Chat Model."
            )
        model_node = models[0]
        if model_node.type == DEEPSEEK_MODEL_TYPE and model_node.type_version == 1:
            provider: Literal["openai", "deepseek"] = "deepseek"
            model_parameters_type = DeepSeekChatModelParameters
        elif model_node.type == OPENAI_MODEL_TYPE and model_node.type_version in {
            1,
            1.1,
            1.2,
            1.3,
        }:
            provider = "openai"
            model_parameters_type = OpenAIChatModelParameters
        else:
            raise UnsupportedPythonCoreAIConfigurationError(
                f"Модель {model_node.name!r} ({model_node.type} v{model_node.type_version}) "
                "не поддерживается простым вызовом Python Core. Поддерживаются DeepSeek "
                "Chat Model v1 и OpenAI Chat Model v1/v1.1/v1.2/v1.3."
            )

        try:
            agent_parameters = PythonCoreAIAgentParameters.model_validate(agent_node.parameters)
            model_parameters = model_parameters_type.model_validate(model_node.parameters)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors(include_input=False)
            )
            raise UnsupportedPythonCoreAIConfigurationError(
                f"AI Agent {agent_node.name!r} или модель {model_node.name!r} "
                f"содержит неподдержанные настройки: {details}"
            ) from error

        if provider == "openai":
            assert isinstance(model_parameters, OpenAIChatModelParameters)
            raw_responses_setting = model_parameters.responses_api_enabled
            if raw_responses_setting is True or (
                model_node.type_version >= 1.3 and raw_responses_setting is not False
            ):
                raise UnsupportedPythonCoreAIConfigurationError(
                    f"Модель {model_node.name!r}: Responses API пока не поддерживается. "
                    "Для OpenAI Chat Model v1.3 задайте responsesApiEnabled=false."
                )

        return cls(
            agent_node,
            model_node,
            provider,
            agent_parameters,
            model_parameters,
        )

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Evaluate the prompt and make one asynchronous API request for each item."""
        if node.name != self.agent_node.name:
            raise UnsupportedPythonCoreAIConfigurationError(
                f"AI executor {self.agent_node.name!r} нельзя применить к {node.name!r}."
            )
        settings = self._runtime_settings(context.services)
        messages_parameters = self._model_parameters.options
        timeout_ms = messages_parameters.timeout
        timeout = aiohttp.ClientTimeout(total=(timeout_ms / 1000) if timeout_ms else 60)
        output_items: list[WorkflowItem] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item_index, item in enumerate(items):
                if not isinstance(item.json_data, Mapping):
                    raise UnsupportedPythonCoreAIConfigurationError(
                        f"AI Agent {node.name!r} ожидает item с JSON-объектом, "
                        "чтобы сохранить поля и добавить output."
                    )
                prompt = self._expressions.evaluate(
                    self._agent_parameters.text,
                    item,
                    item_index=item_index,
                    context=context,
                )
                system_message = self._expressions.evaluate(
                    self._agent_parameters.options.system_message,
                    item,
                    item_index=item_index,
                    context=context,
                )
                messages: list[dict[str, str]] = []
                if system_message:
                    messages.append({"role": "system", "content": str(system_message)})
                messages.append({"role": "user", "content": str(prompt)})

                answer = await self._call_chat_completions(
                    session=session,
                    settings=settings,
                    messages=messages,
                )
                updated_json = dict(item.json_data)
                updated_json["output"] = answer
                output_items.append(WorkflowItem(json=updated_json, binary=item.binary))
        return NodeResult(outputs=(tuple(output_items),))

    def _runtime_settings(self, services: Mapping[str, Any]) -> _ChatRuntimeSettings:
        """Resolve a model's per-node config, environment fallback, and safe defaults."""
        credentials = services.get("credentials", {})
        model_credentials = (
            credentials.get(self._model_node.name, {}) if isinstance(credentials, Mapping) else {}
        )
        if not isinstance(model_credentials, Mapping):
            model_credentials = {}

        provider_env = self._provider.upper()
        model_value = self._model_parameters.model
        if isinstance(model_value, dict):
            model_value = model_value.get("value")
        options = self._model_parameters.options
        runtime_settings: dict[str, Any] = {
            "api_key": services.get(f"{self._provider}_api_key")
            or model_credentials.get("api_key")
            or model_credentials.get("apiKey")
            or model_credentials.get("token")
            or os.environ.get(f"{provider_env}_API_KEY"),
            "base_url": services.get(f"{self._provider}_base_url")
            or model_credentials.get("base_url")
            or model_credentials.get("baseURL")
            or options.base_url
            or os.environ.get(f"{provider_env}_BASE_URL")
            or (
                "https://api.openai.com/v1"
                if self._provider == "openai"
                else "https://api.deepseek.com"
            ),
            "model": services.get(f"{self._provider}_model")
            or model_credentials.get("model")
            or os.environ.get(f"{provider_env}_MODEL")
            or model_value
            or ("gpt-4o-mini" if self._provider == "openai" else "deepseek-flash"),
        }
        try:
            return _ChatRuntimeSettings.model_validate(runtime_settings)
        except ValidationError as error:
            credential_name = f"{provider_env}_API_KEY"
            raise RuntimeError(
                f"Заполните credentials узла модели {self._model_node.name!r} в config.toml "
                f"или проверьте {credential_name} и настройки провайдера."
            ) from error

    async def _call_chat_completions(
        self,
        *,
        session: aiohttp.ClientSession,
        settings: _ChatRuntimeSettings,
        messages: list[dict[str, str]],
    ) -> str:
        """Call a provider's Chat Completions API and require a text answer."""
        body: dict[str, Any] = {
            "model": settings.model,
            "messages": messages,
            "stream": False,
        }
        options = self._model_parameters.options
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if options.top_p is not None:
            body["top_p"] = options.top_p
        if options.max_tokens is not None:
            body["max_tokens"] = options.max_tokens
        if self._provider == "deepseek":
            body["thinking"] = {"type": "disabled"}
        elif isinstance(self._model_parameters, OpenAIChatModelParameters):
            if options.frequency_penalty is not None:
                body["frequency_penalty"] = options.frequency_penalty
            if options.presence_penalty is not None:
                body["presence_penalty"] = options.presence_penalty

        endpoint = f"{str(settings.base_url).rstrip('/')}/chat/completions"
        provider_name = "OpenAI" if self._provider == "openai" else "DeepSeek"
        retries = options.max_retries or 0
        for attempt in range(retries + 1):
            try:
                async with session.post(
                    endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
                ) as response:
                    if response.status >= 500 and attempt < retries:
                        await response.read()
                        continue
                    if response.status >= 400:
                        raise RuntimeError(
                            f"{provider_name} API вернул HTTP {response.status} для AI Agent "
                            f"{self.agent_node.name!r}. Проверьте credentials, URL и модель."
                        )
                    raw_response = await response.json()
                    break
            except (aiohttp.ClientError, TimeoutError) as error:
                if attempt < retries:
                    continue
                raise RuntimeError(
                    f"Не удалось выполнить запрос к {provider_name} для AI Agent "
                    f"{self.agent_node.name!r}. Проверьте URL, сеть и timeout."
                ) from error

        try:
            response = _ChatCompletionsResponse.model_validate(raw_response)
        except ValidationError as error:
            raise RuntimeError(
                f"{provider_name} API вернул ответ неподдерживаемого формата."
            ) from error
        answer = response.choices[0].message.content
        if answer is None:
            raise RuntimeError(
                f"{provider_name} API не вернул текстовый ответ для AI Agent "
                f"{self.agent_node.name!r}. Python Core не выполняет tool calls."
            )
        return answer


@dataclass(frozen=True, slots=True)
class PythonCoreAIWorkflowPlan:
    """Validated AI executors and manual actions discovered in one workflow."""

    executors: dict[str, PythonCoreAIAgentExecutor]
    auxiliary_node_names: frozenset[str]
    handled_node_names: frozenset[str]
    manual_changes: tuple[str, ...]


def inspect_python_core_ai_workflow(workflow: Workflow) -> PythonCoreAIWorkflowPlan:
    """Build simple AI overrides and list features that require LangGraph/manual work."""
    executors: dict[str, PythonCoreAIAgentExecutor] = {}
    auxiliary_names: set[str] = set()
    handled_names: set[str] = set()
    manual_changes: list[str] = []
    agent_nodes = [node for node in workflow.nodes if node.type == AI_AGENT_TYPE]

    for agent_node in agent_nodes:
        incoming_nodes = _connected_ai_nodes(workflow, agent_node)
        handled_names.add(agent_node.name)
        handled_names.update(node.name for node in incoming_nodes)
        try:
            executor = PythonCoreAIAgentExecutor.from_workflow(workflow, agent_node)
        except UnsupportedPythonCoreAIConfigurationError as error:
            manual_changes.append(
                f"AI Agent {agent_node.name!r}: {error} Для полноценного agent runtime "
                "выберите LangGraph; точка расширения Python Core — "
                "`src/n8n_to_python/runtime/python_core_ai.py`."
            )
            continue
        executors[agent_node.name] = executor
        auxiliary_names.add(executor._model_node.name)

    for node in workflow.nodes:
        if node.type not in AI_AUXILIARY_TYPES or node.name in handled_names:
            continue
        handled_names.add(node.name)
        if node.type in AI_MODEL_TYPES:
            manual_changes.append(
                f"AI-модель {node.name!r} не подключена к AI Agent; Python Core не запускает "
                "узел модели отдельно."
            )
        elif node.type == CALCULATOR_TOOL_TYPE:
            manual_changes.append(
                f"Calculator Tool {node.name!r} не подключён к поддержанному AI Agent."
            )
        elif node.type == SIMPLE_MEMORY_TYPE:
            manual_changes.append(
                f"Simple Memory {node.name!r} не подключена к поддержанному AI Agent."
            )

    return PythonCoreAIWorkflowPlan(
        executors=executors,
        auxiliary_node_names=frozenset(auxiliary_names),
        handled_node_names=frozenset(handled_names),
        manual_changes=tuple(manual_changes),
    )


def build_python_core_ai_executors(
    workflow: Workflow,
) -> tuple[dict[str, PythonCoreAIAgentExecutor], frozenset[str]]:
    """Return the supported simple-AI executors and model nodes to omit from main flow."""
    plan = inspect_python_core_ai_workflow(workflow)
    if plan.manual_changes:
        raise UnsupportedPythonCoreAIConfigurationError(
            "Python Core AI требует ручной доработки: " + " ".join(plan.manual_changes)
        )
    return plan.executors, plan.auxiliary_node_names


def _connected_ai_nodes(workflow: Workflow, agent_node: WorkflowNode) -> list[WorkflowNode]:
    """Resolve the nodes with AI-port edges targeting one agent, regardless of port support."""
    nodes_by_name = {node.name: node for node in workflow.nodes}
    connected: list[WorkflowNode] = []
    for source_name, connection_types in workflow.connections.items():
        for port_type, output_groups in connection_types.items():
            if not port_type.startswith("ai_"):
                continue
            for output_group in output_groups or ():
                for connection in output_group or ():
                    if connection.node == agent_node.name and source_name in nodes_by_name:
                        connected.append(nodes_by_name[source_name])
    return connected
