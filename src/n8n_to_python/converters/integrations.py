"""Data Table, PostgreSQL и SMTP адаптеры с явным внедрением зависимостей."""

import re
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from ..application.models.outgoing_email import OutgoingEmail
from ..application.models.postgres_query_result import PostgresQueryResult
from ..application.ports.data_table_store import (
    DataTableCondition,
    DataTableOperation,
)
from ..domain.models.workflow import WorkflowNode
from ..runtime.executors import NodeExecutionContext
from ..runtime.models import NodeResult, WorkflowItem
from .errors import UnsupportedNodeConfigurationError
from .expressions import N8nExpressionEvaluator
from .nodes import EmptyOptions, _StrictParameters


class PostgresParameters(_StrictParameters):
    """Только Postgres Execute Query с позиционными SQL-параметрами."""

    operation: Literal["executeQuery"]
    query: str = Field(min_length=1)
    query_replacement: Any = Field(default=None, alias="queryReplacement")
    options: EmptyOptions = Field(default_factory=EmptyOptions)


class PostgresExecutor:
    """Подготовить SQL и делегировать запрос внедрённому PostgresClient."""

    is_trigger = False

    def __init__(self, parameters: PostgresParameters):
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        client = context.services.get("postgres_client")
        if not callable(getattr(client, "execute_query", None)):
            raise UnsupportedNodeConfigurationError(
                "Postgres требует PostgresClient в runtime services['postgres_client']."
            )

        input_items = items or (WorkflowItem(json={}),)
        output: list[WorkflowItem] = []
        for item_index, item in enumerate(input_items):
            query = self._expressions.evaluate(self._parameters.query, item, item_index=item_index)
            replacements = self._resolve_replacements(item, item_index)
            statement, bind_values = self._bind_query(query, replacements)
            result: PostgresQueryResult = await client.execute_query(statement, bind_values)
            if result.rows:
                output.extend(WorkflowItem(json=row, binary=item.binary) for row in result.rows)
            else:
                output.append(WorkflowItem(json={"rowCount": result.row_count}, binary=item.binary))
        return NodeResult(outputs=(tuple(output),))

    def _resolve_replacements(self, item: WorkflowItem, item_index: int) -> list[Any]:
        raw = self._parameters.query_replacement
        if raw is None:
            return []
        value = self._expressions.evaluate(raw, item, item_index=item_index)
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _bind_query(query: Any, replacements: list[Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(query, str):
            raise UnsupportedNodeConfigurationError("Postgres query должен вычислиться в строку.")
        positions = [int(match) for match in re.findall(r"\$(\d+)", query)]
        if not positions and replacements:
            raise UnsupportedNodeConfigurationError(
                "queryReplacement задан, но запрос не содержит параметров вида $1."
            )
        if positions and max(positions) > len(replacements):
            raise UnsupportedNodeConfigurationError(
                "Для каждого параметра $n в query требуется значение queryReplacement."
            )
        bind_values = {f"n8n_p{index}": value for index, value in enumerate(replacements, 1)}
        statement = re.sub(r"\$(\d+)", lambda match: f":n8n_p{match.group(1)}", query)
        return statement, bind_values


class DataTableLocator(_StrictParameters):
    """Resource locator в формате экспортированного workflow n8n."""

    mode: Literal["list", "id", "name"]
    value: str = Field(min_length=1)
    ref: bool | None = Field(default=None, alias="__rl")
    cached_result_name: str | None = Field(default=None, alias="cachedResultName")


class DataTableColumnMapping(_StrictParameters):
    """Режим автоматического или явного сопоставления колонок."""

    mapping_mode: Literal["autoMapInputData", "defineBelow"] = Field(
        default="autoMapInputData", alias="mappingMode"
    )
    value: dict[str, Any] = Field(default_factory=dict)
    schema_fields: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    matching_columns: list[str] = Field(default_factory=list, alias="matchingColumns")
    attempt_to_convert_types: Literal[False] = Field(default=False, alias="attemptToConvertTypes")
    convert_fields_to_string: Literal[False] = Field(default=False, alias="convertFieldsToString")


class DataTableFilterCondition(_StrictParameters):
    """Одно условие фильтрации строки по колонке таблицы."""

    key_name: str = Field(alias="keyName", min_length=1)
    condition: Literal[
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "like",
        "notLike",
        "isEmpty",
        "isNotEmpty",
        "isTrue",
        "isFalse",
    ] = "eq"
    key_value: Any = Field(default=None, alias="keyValue")


class DataTableFilters(_StrictParameters):
    """Набор фильтров из `filters.conditions`."""

    conditions: list[DataTableFilterCondition] = Field(default_factory=list)


class DataTableOptions(_StrictParameters):
    """Явно поддерживаемые опции строковых Data Table операций."""

    dry_run: bool = Field(default=False, alias="dryRun")
    optimize_bulk: bool = Field(default=False, alias="optimizeBulk")


class DataTableParameters(_StrictParameters):
    """Подмножество строковых CRUD-операций Data Table, хранимых через SQL."""

    resource: Literal["row"] = "row"
    operation: DataTableOperation
    data_table_id: DataTableLocator = Field(alias="dataTableId")
    match_type: Literal["allConditions", "anyCondition"] = Field(
        default="allConditions", alias="matchType"
    )
    filters: DataTableFilters = Field(default_factory=DataTableFilters)
    columns: DataTableColumnMapping = Field(default_factory=DataTableColumnMapping)
    limit: int = Field(default=1000, gt=0, strict=True)
    options: DataTableOptions = Field(default_factory=DataTableOptions)

    @model_validator(mode="after")
    def require_filters_for_mutation(self) -> Self:
        """Защититься от случайного update/delete/upsert всей таблицы."""
        if self.operation in {"update", "upsert", "deleteRows"} and not self.filters.conditions:
            raise ValueError(f"Операция {self.operation!r} требует filters.conditions.")
        if self.options.dry_run and self.operation not in {"update", "upsert", "deleteRows"}:
            raise ValueError(f"options.dryRun не поддерживается для {self.operation!r}.")
        return self


class DataTableExecutor:
    """Разрешить expressions и передать операцию внедрённому DataTableStore."""

    is_trigger = False

    def __init__(self, parameters: DataTableParameters):
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        store = context.services.get("data_table_store")
        if not callable(getattr(store, "execute_row_operation", None)):
            raise UnsupportedNodeConfigurationError(
                "Data Table требует DataTableStore в runtime services['data_table_store']."
            )
        inputs = items or ((WorkflowItem(json={}),) if self._parameters.operation == "get" else ())
        results: list[WorkflowItem] = []
        for item_index, item in enumerate(inputs):
            table_id = self._expressions.evaluate(
                self._parameters.data_table_id.value, item, item_index=item_index
            )
            if not isinstance(table_id, str) or not table_id.strip():
                raise UnsupportedNodeConfigurationError("dataTableId должен быть непустой строкой.")
            values = self._resolve_columns(item, item_index)
            conditions = self._resolve_conditions(item, item_index)
            rows = await store.execute_row_operation(
                table_id=table_id,
                operation=self._parameters.operation,
                values=values,
                conditions=conditions,
                match_type=self._parameters.match_type,
                limit=self._parameters.limit,
                dry_run=self._parameters.options.dry_run,
            )
            if self._parameters.operation == "rowExists":
                if rows:
                    results.append(item)
            elif self._parameters.operation == "rowNotExists":
                if not rows:
                    results.append(item)
            elif self._parameters.operation != "deleteRows" or self._parameters.options.dry_run:
                results.extend(WorkflowItem(json=row, binary=item.binary) for row in rows)
        return NodeResult(outputs=(tuple(results),))

    def _resolve_columns(self, item: WorkflowItem, item_index: int) -> dict[str, Any]:
        mapping = self._parameters.columns
        if mapping.mapping_mode == "autoMapInputData":
            return {
                key: value
                for key, value in item.json_data.items()
                if key not in {"id", "createdAt", "updatedAt"}
            }
        return {
            key: self._expressions.evaluate(value, item, item_index=item_index)
            for key, value in mapping.value.items()
        }

    def _resolve_conditions(
        self, item: WorkflowItem, item_index: int
    ) -> tuple[DataTableCondition, ...]:
        return tuple(
            (
                condition.key_name,
                condition.condition,
                self._expressions.evaluate(condition.key_value, item, item_index=item_index),
            )
            for condition in self._parameters.filters.conditions
        )


class EmailSendOptions(_StrictParameters):
    """Дополнительные простые поля SMTP email."""

    cc_email: str | None = Field(default=None, alias="ccEmail")
    bcc_email: str | None = Field(default=None, alias="bccEmail")
    reply_to: str | None = Field(default=None, alias="replyTo")


class EmailSendParameters(_StrictParameters):
    """SMTP-поля Send Email v2.1 без вложений и шаблонов."""

    from_email: str = Field(alias="fromEmail", min_length=1)
    to_email: str = Field(alias="toEmail", min_length=1)
    subject: str
    text: str | None = None
    html: str | None = None
    options: EmailSendOptions = Field(default_factory=EmailSendOptions)

    @model_validator(mode="after")
    def require_message_body(self) -> Self:
        """Требовать текстовую или HTML-версию сообщения."""
        if self.text is None and self.html is None:
            raise ValueError("Укажите text или html для отправки письма.")
        return self


class EmailSendExecutor:
    """Подготовить email item-ы и передать их внедрённому EmailSender."""

    is_trigger = False

    def __init__(self, parameters: EmailSendParameters):
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        sender = context.services.get("email_sender")
        if not callable(getattr(sender, "send", None)):
            raise UnsupportedNodeConfigurationError(
                "Email требует EmailSender в runtime services['email_sender']."
            )
        for item_index, item in enumerate(items):
            await sender.send(self._create_message(item, item_index))
        return NodeResult(outputs=(items,))

    def _create_message(self, item: WorkflowItem, item_index: int) -> OutgoingEmail:
        """Вычислить n8n expressions и построить типизированное письмо."""
        sender = self._evaluate(self._parameters.from_email, item, item_index)
        recipients = self._evaluate(self._parameters.to_email, item, item_index)
        subject = self._evaluate(self._parameters.subject, item, item_index)
        if not all(isinstance(value, str) for value in (sender, recipients, subject)):
            raise UnsupportedNodeConfigurationError("From, To и Subject должны быть строками.")
        text_body = self._evaluate(self._parameters.text, item, item_index)
        html_body = self._evaluate(self._parameters.html, item, item_index)
        cc = self._resolve_optional_addresses(self._parameters.options.cc_email, item, item_index)
        bcc = self._resolve_optional_addresses(self._parameters.options.bcc_email, item, item_index)
        reply_to = self._evaluate(self._parameters.options.reply_to, item, item_index)
        if reply_to is not None and not isinstance(reply_to, str):
            raise UnsupportedNodeConfigurationError("Поле Email Reply-To должно быть строкой.")
        if text_body is not None and not isinstance(text_body, str):
            raise UnsupportedNodeConfigurationError("Поле Email text должно быть строкой.")
        if html_body is not None and not isinstance(html_body, str):
            raise UnsupportedNodeConfigurationError("Поле Email html должно быть строкой.")
        return OutgoingEmail(
            from_email=sender,
            to=tuple(_email_addresses(recipients)),
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            subject=subject,
            text=text_body,
            html=html_body,
        )

    def _resolve_optional_addresses(
        self,
        value: str | None,
        item: WorkflowItem,
        item_index: int,
    ) -> tuple[str, ...]:
        """Разобрать необязательный список CC/BCC после вычисления expression."""
        if value is None:
            return ()
        resolved = self._evaluate(value, item, item_index)
        if not isinstance(resolved, str):
            raise UnsupportedNodeConfigurationError("Список Email recipients должен быть строкой.")
        return tuple(_email_addresses(resolved))

    def _evaluate(self, value: Any, item: WorkflowItem, item_index: int) -> Any:
        """Разрешить строковое поле или оставить отсутствующее поле пустым."""
        return (
            None
            if value is None
            else self._expressions.evaluate(value, item, item_index=item_index)
        )


def _email_addresses(value: str) -> list[str]:
    """Разобрать строку из одного или нескольких comma-separated адресов."""
    return [address.strip() for address in value.split(",") if address.strip()]
