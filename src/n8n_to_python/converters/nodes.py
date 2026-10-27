"""Первые проверяемые адаптеры узлов n8n для асинхронного runtime."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..application.models.diagnostic import WorkflowDiagnostic
from ..domain.models.workflow import Workflow, WorkflowNode
from ..runtime.executors import (
    NodeExecutionContext,
    NodeExecutor,
    NodeExecutorRegistry,
)
from ..runtime.models import NodeResult, WorkflowItem
from .errors import (
    NodeConversionError,
    UnsupportedNodeConfigurationError,
    UnsupportedNodeFeatureError,
    UnsupportedNodeVersionError,
)
from .expressions import N8nExpressionEvaluator

NON_EXECUTABLE_NODE_TYPES = frozenset({"n8n-nodes-base.stickyNote"})


class _StrictParameters(BaseModel):
    """Базовая схема параметров, запрещающая неизвестные настройки."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmptyOptions(_StrictParameters):
    """Пустой объект настроек для узлов, чьи опции пока не реализованы."""


class ManualTriggerParameters(_StrictParameters):
    """Параметры одноразового ручного запуска; у n8n узла они пустые."""


class WebhookParameters(_StrictParameters):
    """Входные параметры Webhook trigger; сетевой listener не создаётся."""

    path: str = Field(min_length=1)
    http_method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = Field(
        default="GET", alias="httpMethod"
    )
    response_mode: Literal["onReceived"] = Field(default="onReceived", alias="responseMode")
    options: EmptyOptions = Field(default_factory=EmptyOptions)


class Assignment(_StrictParameters):
    """Одно поле, которое узел Edit Fields добавит или заменит."""

    name: str = Field(min_length=1)
    value: Any
    type: Literal["string", "number", "boolean", "object", "array"] = "string"
    id: str | None = None


class AssignmentList(_StrictParameters):
    """Вложенный список назначений из экспорта n8n."""

    assignments: list[Assignment] = Field(default_factory=list)


class EditFieldsOptions(_StrictParameters):
    """Настройки Edit Fields, влияющие на запись имени поля."""

    dot_notation: bool = Field(default=True, alias="dotNotation")


class EditFieldsParameters(_StrictParameters):
    """Поднабор настроек Set/Edit Fields, реализованный сейчас."""

    mode: Literal["manual"] = "manual"
    duplicate_item: Literal[False] = Field(default=False, alias="duplicateItem")
    assignments: AssignmentList = Field(default_factory=AssignmentList)
    include_other_fields: bool = Field(default=False, alias="includeOtherFields")
    options: EditFieldsOptions = Field(default_factory=EditFieldsOptions)


class Operator(_StrictParameters):
    """Оператор сравнения одной IF/Switch-условной проверки."""

    type: Literal["number", "string", "boolean"]
    operation: Literal[
        "equals",
        "notEquals",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "startsWith",
        "endsWith",
    ]
    name: str | None = None


class Condition(_StrictParameters):
    """Одно условие с левым значением, оператором и правым значением."""

    left_value: Any = Field(alias="leftValue")
    right_value: Any = Field(alias="rightValue")
    operator: Operator
    id: str | None = None

    @model_validator(mode="after")
    def validate_operator_for_type(self) -> Self:
        """Не принимать операторы, поведение которых не реализовано для типа."""
        allowed = {
            "number": {"equals", "notEquals", "gt", "gte", "lt", "lte"},
            "string": {"equals", "notEquals", "contains", "startsWith", "endsWith"},
            "boolean": {"equals", "notEquals"},
        }
        if self.operator.operation not in allowed[self.operator.type]:
            raise ValueError(
                f"Оператор {self.operator.type}.{self.operator.operation} не поддерживается."
            )
        return self


class ConditionOptions(_StrictParameters):
    """Настройки сравнения, сериализованные n8n рядом с условиями."""

    case_sensitive: Literal[True] = Field(default=True, alias="caseSensitive")
    left_value: str = Field(default="", alias="leftValue")
    type_validation: Literal["strict"] = Field(default="strict", alias="typeValidation")
    version: Literal[2] = 2


class Conditions(_StrictParameters):
    """Условия IF или одного правила Switch."""

    conditions: list[Condition] = Field(default_factory=list)
    combinator: Literal["and", "or"] = "and"
    options: ConditionOptions = Field(default_factory=ConditionOptions)


class IfParameters(_StrictParameters):
    """Поддерживаемая конфигурация узла IF."""

    conditions: Conditions
    options: EmptyOptions = Field(default_factory=EmptyOptions)


class SwitchRule(_StrictParameters):
    """Одно правило маршрутизации в режиме Switch Rules."""

    conditions: Conditions
    rename_output: bool | None = Field(default=None, alias="renameOutput")
    output_key: str | None = Field(default=None, alias="outputKey")


class SwitchRules(_StrictParameters):
    """Список правил, индекс правила соответствует индексу выхода."""

    values: list[SwitchRule] = Field(default_factory=list)


class SwitchOptions(_StrictParameters):
    """Реализованные настройки выхода Switch."""

    fallback_output: Literal["none", "extra"] | int = Field(default="none", alias="fallbackOutput")
    send_to_all_matching_outputs: bool = Field(default=False, alias="sendToAllMatchingOutputs")
    rename_fallback_output: str | None = Field(default=None, alias="renameFallbackOutput")


class SwitchParameters(_StrictParameters):
    """Поддерживаемая конфигурация узла Switch."""

    mode: Literal["rules"]
    rules: SwitchRules
    options: SwitchOptions = Field(default_factory=SwitchOptions)


class LoopOptions(_StrictParameters):
    """Настройки Loop Over Items, применяемые к текущему ограниченному режиму."""

    reset: bool = False


class LoopParameters(_StrictParameters):
    """Размер пакета и настройки Loop Over Items."""

    batch_size: int = Field(default=1, alias="batchSize", gt=0, strict=True)
    options: LoopOptions = Field(default_factory=LoopOptions)


class ScheduleInterval(_StrictParameters):
    """Одна настройка интервала Schedule Trigger v1.3."""

    field: Literal["seconds", "minutes", "hours", "days", "weeks", "months", "cronExpression"] = (
        "days"
    )
    seconds_interval: int = Field(default=30, alias="secondsInterval", ge=1, le=59)
    minutes_interval: int = Field(default=5, alias="minutesInterval", ge=1, le=59)
    hours_interval: int = Field(default=1, alias="hoursInterval", ge=1, le=23)
    days_interval: int = Field(default=1, alias="daysInterval", ge=1, le=31)
    weeks_interval: int = Field(default=1, alias="weeksInterval", ge=1)
    months_interval: int = Field(default=1, alias="monthsInterval", ge=1)
    trigger_at_day_of_month: int = Field(default=1, alias="triggerAtDayOfMonth", ge=1, le=31)
    trigger_at_day: list[int] = Field(default_factory=lambda: [0], alias="triggerAtDay")
    trigger_at_hour: int = Field(default=0, alias="triggerAtHour", ge=0, le=23)
    trigger_at_minute: int = Field(default=0, alias="triggerAtMinute", ge=0, le=59)
    expression: str | None = None

    @model_validator(mode="after")
    def validate_interval_fields(self) -> Self:
        """Проверить только поля, которые управляют выбранным видом расписания."""
        if self.field == "cronExpression":
            if not self.expression or not self.expression.strip():
                raise ValueError("Для cronExpression требуется непустое expression.")
        elif self.field == "weeks" and any(day not in range(7) for day in self.trigger_at_day):
            raise ValueError("triggerAtDay должен содержать дни недели от 0 до 6.")
        return self


class ScheduleRule(_StrictParameters):
    """Список правил срабатывания из `parameters.rule` n8n."""

    interval: list[ScheduleInterval] = Field(default_factory=lambda: [ScheduleInterval()])


class ScheduleTriggerParameters(_StrictParameters):
    """Проверенные правила Schedule Trigger; сам таймер запускает вызывающий runtime."""

    rule: ScheduleRule = Field(default_factory=ScheduleRule)


class MergeParameters(_StrictParameters):
    """Поддерживаемый режим Merge v3.2 для последовательного объединения inputs."""

    mode: Literal["append"]
    number_inputs: int = Field(default=2, alias="numberInputs", ge=2, le=10, strict=True)
    options: EmptyOptions = Field(default_factory=EmptyOptions)


class NodeConverter(Protocol):
    """Проверяет узел и создаёт его runtime-обработчик."""

    node_type: str
    type_version: float

    def create_executor(self, node: WorkflowNode) -> NodeExecutor:
        """Проверить параметры и вернуть асинхронный обработчик узла."""
        ...


@dataclass(frozen=True, slots=True)
class ValidatedNodeConverter:
    """Связывает точную версию узла со схемой и фабрикой обработчика."""

    node_type: str
    type_version: float
    parameters_model: type[BaseModel]
    executor_factory: Callable[[BaseModel], NodeExecutor]

    def create_executor(self, node: WorkflowNode) -> NodeExecutor:
        """Валидировать параметры узла до начала выполнения всего графа."""
        try:
            parameters = self.parameters_model.model_validate(node.parameters)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
                for detail in error.errors(include_input=False)
            )
            raise UnsupportedNodeConfigurationError(
                f"Узел {node.name!r} ({node.type} v{node.type_version}) имеет "
                f"неподдерживаемые параметры: {details}"
            ) from error
        return self.executor_factory(parameters)


class NodeConverterRegistry:
    """Находит адаптеры строго по паре `node.type` и `typeVersion`."""

    def __init__(self, converters: Mapping[tuple[str, float], NodeConverter]):
        """Скопировать таблицу поддержанных версий узлов."""
        self._converters = dict(converters)

    def inspect_node(self, node: WorkflowNode) -> tuple[WorkflowDiagnostic, ...]:
        """Вернуть warning для неизвестного узла или error для его параметров."""
        if node.type in NON_EXECUTABLE_NODE_TYPES:
            return ()
        converter = self._converters.get((node.type, node.type_version))
        if converter is None:
            supported_versions = sorted(
                version for node_type, version in self._converters if node_type == node.type
            )
            if supported_versions:
                code = "UNSUPPORTED_NODE_VERSION"
                detail = f"Поддержанные версии: {supported_versions}."
            else:
                code = "UNSUPPORTED_NODE_TYPE"
                detail = "Тип узла пока не поддерживается."
            return (
                WorkflowDiagnostic(
                    severity="warning",
                    code=code,
                    message=f"Узел {node.name!r} типа {node.type!r} версии "
                    f"{node.type_version!r} не поддержан. {detail}",
                    node_id=node.id,
                    node_name=node.name,
                    node_type=node.type,
                    context={"type_version": node.type_version},
                ),
            )
        try:
            converter.create_executor(node)
        except UnsupportedNodeFeatureError as error:
            return (
                WorkflowDiagnostic(
                    severity="warning",
                    code="UNSUPPORTED_NODE_FEATURE",
                    message=f"Узел {node.name!r}: {error}",
                    node_id=node.id,
                    node_name=node.name,
                    node_type=node.type,
                    context={"type_version": node.type_version},
                ),
            )
        except NodeConversionError as error:
            return (
                WorkflowDiagnostic(
                    severity="error",
                    code="INVALID_NODE_CONFIGURATION",
                    message=str(error),
                    node_id=node.id,
                    node_name=node.name,
                    node_type=node.type,
                    context={"type_version": node.type_version},
                ),
            )
        return ()

    def is_trigger(self, node: WorkflowNode) -> bool:
        """Проверить, зарегистрирован ли узел как trigger с валидными параметрами."""
        if node.type in NON_EXECUTABLE_NODE_TYPES:
            return False
        converter = self._converters.get((node.type, node.type_version))
        if converter is None:
            return False
        try:
            return converter.create_executor(node).is_trigger
        except NodeConversionError:
            return False

    def build_runtime_registry(
        self,
        workflow: Workflow,
        *,
        node_overrides: Mapping[str, NodeExecutor] | None = None,
        ignored_node_names: frozenset[str] = frozenset(),
    ) -> NodeExecutorRegistry:
        """Проверить узлы и собрать реестр runtime-обработчиков.

        `node_overrides` позволяет backend-у подменить обработчик конкретного узла.
        `ignored_node_names` предназначен для вспомогательных AI-моделей, инструментов
        и памяти: вызывающий код должен предварительно проверить их связи и версии.
        """
        executors: dict[str, NodeExecutor] = {}
        overrides = node_overrides or {}
        for node in workflow.nodes:
            if node.type in NON_EXECUTABLE_NODE_TYPES:
                continue
            if node.name in ignored_node_names:
                continue
            if node.name in overrides:
                executors[node.name] = overrides[node.name]
                continue
            converter = self._converters.get((node.type, node.type_version))
            if converter is None:
                supported_versions = sorted(
                    version for node_type, version in self._converters if node_type == node.type
                )
                if supported_versions:
                    detail = f"Поддержанные версии: {supported_versions}."
                else:
                    detail = "Этот тип узла пока не реализован."
                raise UnsupportedNodeVersionError(
                    f"Узел {node.name!r} типа {node.type!r} версии "
                    f"{node.type_version!r} не поддержан. {detail}"
                )
            try:
                executors[node.name] = converter.create_executor(node)
            except NodeConversionError:
                raise
            except Exception as error:
                raise NodeConversionError(f"Не удалось подготовить узел {node.name!r}.") from error
        return NodeExecutorRegistry({}, node_executors=executors)


def _set_field(target: dict[str, Any], name: str, value: Any, *, dot_notation: bool) -> None:
    """Записать поле буквально либо создать вложенный объект по точкам."""
    parts = name.split(".") if dot_notation else [name]
    current = target
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = value


def _condition_matches(
    condition: Condition,
    item: WorkflowItem,
    evaluator: N8nExpressionEvaluator,
    item_index: int,
    context: NodeExecutionContext,
) -> bool:
    """Вычислить один ограниченный оператор, не используя eval/exec."""
    left = evaluator.evaluate(condition.left_value, item, item_index=item_index, context=context)
    right = evaluator.evaluate(condition.right_value, item, item_index=item_index, context=context)
    kind = condition.operator.type
    operation = condition.operator.operation
    if kind == "number":
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        operations = {
            "equals": lambda: left == right,
            "notEquals": lambda: left != right,
            "gt": lambda: left > right,
            "gte": lambda: left >= right,
            "lt": lambda: left < right,
            "lte": lambda: left <= right,
        }
    elif kind == "string":
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        operations = {
            "equals": lambda: left == right,
            "notEquals": lambda: left != right,
            "contains": lambda: right in left,
            "startsWith": lambda: left.startswith(right),
            "endsWith": lambda: left.endswith(right),
        }
    else:
        if not isinstance(left, bool) or not isinstance(right, bool):
            return False
        operations = {"equals": lambda: left is right, "notEquals": lambda: left is not right}
    try:
        operation_fn = operations[operation]
    except KeyError as error:
        raise UnsupportedNodeConfigurationError(
            f"Оператор {kind}.{operation} пока не поддержан."
        ) from error
    return operation_fn()


def _conditions_match(
    conditions: Conditions,
    item: WorkflowItem,
    evaluator: N8nExpressionEvaluator,
    item_index: int,
    context: NodeExecutionContext,
) -> bool:
    """Объединить условия операцией AND или OR."""
    results = [
        _condition_matches(condition, item, evaluator, item_index, context)
        for condition in conditions.conditions
    ]
    if not results:
        return False
    return all(results) if conditions.combinator == "and" else any(results)


class ManualTriggerExecutor:
    """Запустить граф вручную и передать начальный пустой n8n item."""

    is_trigger = True

    def __init__(self, parameters: ManualTriggerParameters):
        """Сохранить уже проверенные пустые параметры trigger-узла."""
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Выдать один начальный item без пользовательских данных."""
        return NodeResult(outputs=(items or (WorkflowItem(json={}),),))


class ScheduleTriggerExecutor:
    """Сформировать item одного расписанного события при вызове runtime."""

    is_trigger = True

    def __init__(self, parameters: ScheduleTriggerParameters):
        """Сохранить валидированные правила; планирование оставлено вызывающему скрипту."""
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Вернуть данные времени срабатывания, как их формирует n8n Schedule Trigger."""
        timezone_name = context.services.get("timezone", "UTC")
        if not isinstance(timezone_name, str):
            raise UnsupportedNodeConfigurationError("timezone должен быть строковым именем зоны.")
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise UnsupportedNodeConfigurationError(
                f"Неизвестная временная зона Schedule Trigger: {timezone_name!r}."
            ) from error

        scheduled_at = context.services.get("schedule_time")
        if scheduled_at is None:
            scheduled_at = datetime.now(timezone)
        elif not isinstance(scheduled_at, datetime):
            raise UnsupportedNodeConfigurationError("schedule_time должен быть datetime.")
        elif scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone)
        else:
            scheduled_at = scheduled_at.astimezone(timezone)

        result = _schedule_trigger_item(scheduled_at, timezone_name)
        return NodeResult(outputs=((WorkflowItem(json=result),),))


class MergeAppendExecutor:
    """Дождаться каждого входа и объединить их items по порядку input index."""

    is_trigger = False

    def __init__(self, parameters: MergeParameters):
        """Сохранить количество обязательных Merge inputs."""
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Буферизовать входы до прибытия каждого порта, затем выдать один append output."""
        input_port = context.input_port
        if input_port is None or input_port.type != "main":
            raise UnsupportedNodeConfigurationError("Merge append требует main input.")
        if input_port.index >= self._parameters.number_inputs:
            raise UnsupportedNodeConfigurationError(
                f"Merge получил input {input_port.index}, но настроено "
                f"{self._parameters.number_inputs} inputs."
            )

        state_key = f"merge-append:{node.id or node.name}"
        merge_state: dict[str, Any] = context.state.setdefault(
            state_key,
            {"expected": self._parameters.number_inputs, "received": {}},
        )
        received: dict[int, tuple[WorkflowItem, ...]] = merge_state["received"]
        received[input_port.index] = (*received.get(input_port.index, ()), *items)
        expected_indexes = set(range(merge_state["expected"]))
        if set(received) != expected_indexes:
            return NodeResult(outputs=(None,))

        merged = tuple(
            item
            for input_index in range(self._parameters.number_inputs)
            for item in received[input_index]
        )
        del context.state[state_key]
        return NodeResult(outputs=(merged,))


def _schedule_trigger_item(scheduled_at: datetime, timezone_name: str) -> dict[str, str]:
    """Построить словарь даты и времени одного события Schedule Trigger."""
    month_names = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    day = scheduled_at.day
    ordinal = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    hour_12 = scheduled_at.hour % 12 or 12
    meridiem = "am" if scheduled_at.hour < 12 else "pm"
    offset = scheduled_at.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if offset_minutes >= 0 else "-"
    offset_hours, offset_minutes = divmod(abs(offset_minutes), 60)
    timezone_offset = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"
    readable_time = f"{hour_12}:{scheduled_at.minute:02d}:{scheduled_at.second:02d} {meridiem}"
    return {
        "timestamp": scheduled_at.isoformat(timespec="milliseconds"),
        "Readable date": (
            f"{month_names[scheduled_at.month - 1]} {day}{ordinal} {scheduled_at.year}, "
            f"{readable_time}"
        ),
        "Readable time": readable_time,
        "Day of week": weekday_names[scheduled_at.weekday()],
        "Year": f"{scheduled_at.year:04d}",
        "Month": month_names[scheduled_at.month - 1],
        "Day of month": f"{day:02d}",
        "Hour": f"{scheduled_at.hour:02d}",
        "Minute": f"{scheduled_at.minute:02d}",
        "Second": f"{scheduled_at.second:02d}",
        "Timezone": f"{timezone_name} ({timezone_offset})",
    }


class WebhookTriggerExecutor:
    """Принять готовый HTTP payload как начальные items вызываемого workflow."""

    is_trigger = True

    def __init__(self, parameters: WebhookParameters):
        self._parameters = parameters

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        if not items:
            raise UnsupportedNodeConfigurationError(
                f"Webhook {node.name!r} требует initial_items с входным HTTP payload."
            )
        return NodeResult(outputs=(items,))


def render_webhook_entrypoint(node: WorkflowNode) -> str:
    """Сформировать вызываемую async-функцию без запуска HTTP-сервера."""
    import re

    identifier = re.sub(r"\W+", "_", node.name, flags=re.UNICODE).strip("_").lower()
    if not identifier or identifier[0].isdigit():
        identifier = f"webhook_{identifier}"
    return (
        f"async def invoke_{identifier}(payload, engine, workflow):\n"
        "    from n8n_to_python.runtime.models import WorkflowItem\n"
        f"    return await engine.run(workflow, initial_items={{{node.name!r}: "
        "[WorkflowItem(json=payload)]})\n"
    )


class EditFieldsExecutor:
    """Добавить или заменить простые поля в JSON каждого item."""

    is_trigger = False

    def __init__(self, parameters: EditFieldsParameters):
        """Сохранить проверенные назначения и режим переноса полей."""
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Создать новые items, сохранив binary и выбранные исходные поля."""
        output: list[WorkflowItem] = []
        for item_index, item in enumerate(items):
            if self._parameters.include_other_fields and not isinstance(item.json_data, dict):
                raise UnsupportedNodeConfigurationError(
                    f"Edit Fields {node.name!r} ожидает JSON object во входном item."
                )
            json_data = dict(item.json_data) if self._parameters.include_other_fields else {}
            for assignment in self._parameters.assignments.assignments:
                value = self._expressions.evaluate(
                    assignment.value, item, item_index=item_index, context=context
                )
                if assignment.type == "number" and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise UnsupportedNodeConfigurationError(
                        f"Значение поля {assignment.name!r} не является числом."
                    )
                if assignment.type == "string" and not isinstance(value, str):
                    raise UnsupportedNodeConfigurationError(
                        f"Значение поля {assignment.name!r} не является строкой."
                    )
                if assignment.type == "boolean" and not isinstance(value, bool):
                    raise UnsupportedNodeConfigurationError(
                        f"Значение поля {assignment.name!r} не является boolean."
                    )
                if assignment.type == "object" and not isinstance(value, dict):
                    raise UnsupportedNodeConfigurationError(
                        f"Значение поля {assignment.name!r} не является объектом."
                    )
                if assignment.type == "array" and not isinstance(value, list):
                    raise UnsupportedNodeConfigurationError(
                        f"Значение поля {assignment.name!r} не является массивом."
                    )
                _set_field(
                    json_data,
                    assignment.name,
                    value,
                    dot_notation=self._parameters.options.dot_notation,
                )
            output.append(WorkflowItem(json=json_data, binary=item.binary))
        return NodeResult(outputs=(tuple(output),))


class IfExecutor:
    """Разделить items по двум выходам IF."""

    is_trigger = False

    def __init__(self, parameters: IfParameters):
        """Сохранить проверенные условия и их AND/OR-комбинатор."""
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Вернуть true items на выход 0, false items на выход 1."""
        true_items: list[WorkflowItem] = []
        false_items: list[WorkflowItem] = []
        for item_index, item in enumerate(items):
            target = (
                true_items
                if _conditions_match(
                    self._parameters.conditions, item, self._expressions, item_index, context
                )
                else false_items
            )
            target.append(item)
        return NodeResult(outputs=(tuple(true_items) or None, tuple(false_items) or None))


class SwitchExecutor:
    """Направить каждый item по первому совпавшему правилу Switch."""

    is_trigger = False

    def __init__(self, parameters: SwitchParameters):
        """Сохранить проверенные правила и стратегию fallback."""
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Вернуть пакет для каждого выхода правил и настроенного fallback."""
        rules = self._parameters.rules.values
        fallback = self._parameters.options.fallback_output
        slot_count = len(rules) + (1 if fallback == "extra" else 0)
        if isinstance(fallback, int) and not isinstance(fallback, bool):
            slot_count = max(slot_count, fallback + 1)
        routed: list[list[WorkflowItem]] = [[] for _ in range(slot_count)]
        for item_index, item in enumerate(items):
            matching = [
                index
                for index, rule in enumerate(rules)
                if _conditions_match(rule.conditions, item, self._expressions, item_index, context)
            ]
            if matching and self._parameters.options.send_to_all_matching_outputs:
                for index in matching:
                    routed[index].append(item)
            elif matching:
                routed[matching[0]].append(item)
            elif fallback == "extra":
                routed[len(rules)].append(item)
            elif isinstance(fallback, int) and not isinstance(fallback, bool):
                if fallback >= len(routed):
                    routed.extend([] for _ in range(fallback + 1 - len(routed)))
                routed[fallback].append(item)
        return NodeResult(outputs=tuple(tuple(batch) or None for batch in routed))


class LoopOverItemsExecutor:
    """Передавать items порциями и вернуть обработанные items по завершении."""

    is_trigger = False

    def __init__(self, parameters: LoopParameters):
        """Сохранить размер пакета после проверки reset=false."""
        self._parameters = parameters
        if parameters.options.reset:
            raise UnsupportedNodeConfigurationError(
                "Loop Over Items с options.reset=true пока не поддержан."
            )

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Выдать следующий пакет на выход 1; завершение отдаёт агрегат на выход 0."""
        state_key = f"loop-over-items:{node.id or node.name}"
        loop_state = context.state.get(state_key)
        if loop_state is None:
            remaining = list(items)
            completed: list[WorkflowItem] = []
        else:
            remaining = list(loop_state["remaining"])
            completed = [*loop_state["completed"], *items]

        batch_size = self._parameters.batch_size
        batch = tuple(remaining[:batch_size])
        remaining = remaining[batch_size:]
        if batch:
            context.state[state_key] = {"remaining": remaining, "completed": completed}
            return NodeResult(outputs=(None, batch))
        context.state.pop(state_key, None)
        return NodeResult(outputs=(tuple(completed), None))


def default_node_converter_registry() -> NodeConverterRegistry:
    """Создать реестр ограниченных и явно версионированных адаптеров."""
    from .code_node import PythonCodeExecutor, PythonCodeParameters
    from .file_nodes import (
        CompressionExecutor,
        CompressionParameters,
        ConvertToFileExecutor,
        ConvertToFileParameters,
        ExtractFileParameters,
        ExtractFromFileExecutor,
    )
    from .http_request import HttpRequestExecutor, HttpRequestParameters
    from .integrations import (
        DataTableExecutor,
        DataTableParameters,
        EmailSendExecutor,
        EmailSendParameters,
        PostgresExecutor,
        PostgresParameters,
    )

    converters: list[ValidatedNodeConverter] = [
        ValidatedNodeConverter(
            "n8n-nodes-base.manualTrigger", 1, ManualTriggerParameters, ManualTriggerExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.scheduleTrigger",
            1.3,
            ScheduleTriggerParameters,
            ScheduleTriggerExecutor,
        ),
        ValidatedNodeConverter("n8n-nodes-base.merge", 3.2, MergeParameters, MergeAppendExecutor),
        ValidatedNodeConverter("n8n-nodes-base.code", 2, PythonCodeParameters, PythonCodeExecutor),
        ValidatedNodeConverter(
            "n8n-nodes-base.splitInBatches", 3, LoopParameters, LoopOverItemsExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.compression", 1.1, CompressionParameters, CompressionExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.extractFromFile", 1, ExtractFileParameters, ExtractFromFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.extractFromFile", 1.1, ExtractFileParameters, ExtractFromFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.extractFromFile", 1.2, ExtractFileParameters, ExtractFromFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.convertToFile", 1, ConvertToFileParameters, ConvertToFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.convertToFile", 1.1, ConvertToFileParameters, ConvertToFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.convertToFile", 1.2, ConvertToFileParameters, ConvertToFileExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.dataTable", 1, DataTableParameters, DataTableExecutor
        ),
        ValidatedNodeConverter(
            "n8n-nodes-base.dataTable", 1.1, DataTableParameters, DataTableExecutor
        ),
    ]
    shared_implementations: tuple[
        tuple[str, tuple[float, ...], type[BaseModel], Callable[[BaseModel], NodeExecutor]], ...
    ] = (
        (
            "n8n-nodes-base.webhook",
            (1, 1.1, 2, 2.1, 2.2),
            WebhookParameters,
            WebhookTriggerExecutor,
        ),
        (
            "n8n-nodes-base.set",
            (3, 3.1, 3.2, 3.3, 3.4, 3.5),
            EditFieldsParameters,
            EditFieldsExecutor,
        ),
        (
            "n8n-nodes-base.if",
            (2, 2.1, 2.2, 2.3, 2.4),
            IfParameters,
            IfExecutor,
        ),
        (
            "n8n-nodes-base.switch",
            (3, 3.1, 3.2, 3.3, 3.4, 3.5),
            SwitchParameters,
            SwitchExecutor,
        ),
        (
            "n8n-nodes-base.httpRequest",
            (4, 4.1, 4.2, 4.3, 4.4, 4.5),
            HttpRequestParameters,
            HttpRequestExecutor,
        ),
        (
            "n8n-nodes-base.postgres",
            (2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8),
            PostgresParameters,
            PostgresExecutor,
        ),
        (
            "n8n-nodes-base.emailSend",
            (2, 2.1),
            EmailSendParameters,
            EmailSendExecutor,
        ),
    )
    for node_type, versions, parameters_model, executor_factory in shared_implementations:
        converters.extend(
            ValidatedNodeConverter(node_type, version, parameters_model, executor_factory)
            for version in versions
        )
    return NodeConverterRegistry(
        {(converter.node_type, converter.type_version): converter for converter in converters}
    )
