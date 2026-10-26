"""Ограниченный асинхронный адаптер HTTP Request на aiohttp."""

from typing import Any, Literal, Self

import aiohttp
from pydantic import Field, model_validator

from ..domain.models.workflow import WorkflowNode
from ..runtime.executors import NodeExecutionContext
from ..runtime.models import NodeResult, WorkflowItem
from .errors import UnsupportedNodeConfigurationError
from .expressions import N8nExpressionEvaluator
from .nodes import _StrictParameters


class HttpParameter(_StrictParameters):
    """Одна query-пара или HTTP-заголовок из экспорта n8n."""

    name: str = Field(min_length=1)
    value: Any
    id: str | None = None


class HttpParameterList(_StrictParameters):
    """Список query-пар или заголовков."""

    parameters: list[HttpParameter] = Field(default_factory=list)


class HttpRequestOptions(_StrictParameters):
    """Ограниченные параметры HTTP-клиента."""

    timeout: int = Field(default=300_000, gt=0, strict=True)


class HttpRequestParameters(_StrictParameters):
    """Поднабор HTTP Request без авторизации, но со статическим JSON body."""

    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1)
    authentication: Literal["none"] = "none"
    send_query: bool = Field(default=False, alias="sendQuery")
    query_parameters: HttpParameterList = Field(
        default_factory=HttpParameterList, alias="queryParameters"
    )
    send_headers: bool = Field(default=False, alias="sendHeaders")
    header_parameters: HttpParameterList = Field(
        default_factory=HttpParameterList, alias="headerParameters"
    )
    send_body: bool = Field(default=False, alias="sendBody")
    json_body: Any = Field(default=None, alias="jsonBody")
    options: HttpRequestOptions = Field(default_factory=HttpRequestOptions)

    @model_validator(mode="after")
    def reject_inactive_parameters(self) -> Self:
        """Отклонить параметры, которые флаги узла отключили бы молча."""
        if not self.send_query and self.query_parameters.parameters:
            raise ValueError("queryParameters заданы, но sendQuery не включён.")
        if not self.send_headers and self.header_parameters.parameters:
            raise ValueError("headerParameters заданы, но sendHeaders не включён.")
        if self.send_body and self.json_body is None:
            raise ValueError("sendBody=true требует jsonBody.")
        if not self.send_body and self.json_body is not None:
            raise ValueError("jsonBody задан, но sendBody не включён.")
        return self


class HttpRequestExecutor:
    """Выполнить HTTP-запрос для каждого item и вернуть JSON-объект ответа."""

    is_trigger = False

    def __init__(self, parameters: HttpRequestParameters):
        """Сохранить проверенные параметры запроса и timeout в миллисекундах."""
        self._parameters = parameters
        self._expressions = N8nExpressionEvaluator()

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Использовать aiohttp; сетевые, HTTP и JSON ошибки прерывают workflow."""
        timeout = aiohttp.ClientTimeout(total=self._parameters.options.timeout / 1000)
        output: list[WorkflowItem] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item_index, item in enumerate(items):
                url = self._expressions.evaluate(self._parameters.url, item, item_index=item_index)
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise UnsupportedNodeConfigurationError(
                        f"HTTP Request {node.name!r} требует URL с http:// или https://."
                    )
                query = self._resolve_parameters(
                    self._parameters.query_parameters.parameters, item, item_index
                )
                headers = self._resolve_parameters(
                    self._parameters.header_parameters.parameters, item, item_index
                )
                body = (
                    self._expressions.evaluate(
                        self._parameters.json_body, item, item_index=item_index
                    )
                    if self._parameters.send_body
                    else None
                )
                async with session.request(
                    method=self._parameters.method,
                    url=url,
                    params=query,
                    headers=headers,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    try:
                        body = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as error:
                        raise UnsupportedNodeConfigurationError(
                            f"HTTP Request {node.name!r} получил ответ не в формате JSON."
                        ) from error
                    if not isinstance(body, dict):
                        raise UnsupportedNodeConfigurationError(
                            f"HTTP Request {node.name!r} ожидает JSON-объект в ответе."
                        )
                    output.append(WorkflowItem(json=body))
        return NodeResult(outputs=(tuple(output),))

    def _resolve_parameters(
        self,
        parameters: list[HttpParameter],
        item: WorkflowItem,
        item_index: int,
    ) -> dict[str, str]:
        """Разрешить статические значения и простые `$json` expressions."""
        resolved: dict[str, str] = {}
        for parameter in parameters:
            value = self._expressions.evaluate(parameter.value, item, item_index=item_index)
            if isinstance(value, (dict, list)):
                raise UnsupportedNodeConfigurationError(
                    f"HTTP-параметр {parameter.name!r} должен быть скалярным значением."
                )
            resolved[parameter.name] = str(value)
        return resolved
