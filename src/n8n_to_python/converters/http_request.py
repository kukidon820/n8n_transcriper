"""Ограниченный асинхронный адаптер HTTP Request на aiohttp."""

import base64
import json
import mimetypes
from email.message import Message
from pathlib import PurePosixPath
from typing import Any, Literal, Self
from urllib.parse import unquote, urlsplit

import aiohttp
from pydantic import Field, model_validator

from ..domain.models.workflow import WorkflowNode
from ..runtime.executors import NodeExecutionContext
from ..runtime.models import NodeResult, WorkflowItem
from .errors import UnsupportedNodeConfigurationError
from .expressions import N8nExpressionEvaluator
from .nodes import _StrictParameters

_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
_BINARY_CONTENT_TYPES = (
    "image/",
    "audio/",
    "video/",
    "application/octet-stream",
    "application/gzip",
    "application/zip",
    "application/vnd.rar",
    "application/epub+zip",
    "application/x-bzip",
    "application/x-bzip2",
    "application/x-cdf",
    "application/vnd.amazon.ebook",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-fontobject",
    "application/vnd.oasis.opendocument.presentation",
    "application/pdf",
    "application/x-tar",
    "application/vnd.visio",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/x-7z-compressed",
)


class HttpParameter(_StrictParameters):
    """Одна query-пара или HTTP-заголовок из экспорта n8n."""

    name: str = Field(min_length=1)
    value: Any
    id: str | None = None


class HttpParameterList(_StrictParameters):
    """Список query-пар или заголовков."""

    parameters: list[HttpParameter] = Field(default_factory=list)


class HttpRequestOptions(_StrictParameters):
    """Параметры HTTP-клиента и формат ответа."""

    timeout: int = Field(default=300_000, gt=0, strict=True)
    response: "HttpResponseGroup" = Field(default_factory=lambda: HttpResponseGroup())


class HttpResponseSettings(_StrictParameters):
    """Официальные форматы ответа HTTP Request v4 и связанные флаги."""

    response_format: Literal["autodetect", "json", "text", "file"] = Field(
        default="autodetect", alias="responseFormat"
    )
    output_property_name: str = Field(default="data", alias="outputPropertyName", min_length=1)
    full_response: bool = Field(default=False, alias="fullResponse")
    never_error: bool = Field(default=False, alias="neverError")


class HttpResponseGroup(_StrictParameters):
    """Вложенность options.response.response в JSON export n8n."""

    response: HttpResponseSettings = Field(default_factory=HttpResponseSettings)


class HttpRequestParameters(_StrictParameters):
    """Поднабор HTTP Request без credentials и с JSON body."""

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
    """Выполнить HTTP-запросы через aiohttp и сформировать JSON/binary items."""

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
        """Сохранить n8n responseFormat, fullResponse и neverError для v4.x."""
        timeout = aiohttp.ClientTimeout(total=self._parameters.options.timeout / 1000)
        output: list[WorkflowItem] = []
        paired_input_indexes: list[int] = []
        response_settings = self._parameters.options.response.response
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item_index, item in enumerate(items):
                url = self._expressions.evaluate(
                    self._parameters.url,
                    item,
                    item_index=item_index,
                    context=context,
                )
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise UnsupportedNodeConfigurationError(
                        f"HTTP Request {node.name!r} требует URL с http:// или https://."
                    )
                query = self._resolve_parameters(
                    self._parameters.query_parameters.parameters, item, item_index, context
                )
                headers = self._resolve_parameters(
                    self._parameters.header_parameters.parameters, item, item_index, context
                )
                body = (
                    self._expressions.evaluate(
                        self._parameters.json_body,
                        item,
                        item_index=item_index,
                        context=context,
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
                    if response.status >= 400 and not response_settings.never_error:
                        response.raise_for_status()
                    response_body = await self._read_limited_body(response, node.name)
                    response_format = self._resolve_response_format(
                        response_settings.response_format,
                        response.headers.get("Content-Type", ""),
                    )
                    response_json = self._parse_json_body(
                        response_body,
                        node_name=node.name,
                        required=(
                            response_settings.response_format == "json"
                            or (
                                response_format == "json"
                                and not (
                                    response_settings.response_format == "autodetect"
                                    and response_settings.never_error
                                )
                            )
                        )
                        or (
                            response_format == "text"
                            and self._is_json_content_type(response.headers.get("Content-Type", ""))
                        ),
                        fallback_to_empty=(
                            response_settings.response_format == "autodetect"
                            and response_settings.never_error
                            and response_format == "json"
                        ),
                    )
                    if response_format == "file":
                        metadata = self._file_metadata(
                            response_body,
                            content_type=response.headers.get("Content-Type", ""),
                            content_disposition=response.headers.get("Content-Disposition"),
                            request_url=url,
                        )
                        binary = dict(item.binary or {})
                        binary[response_settings.output_property_name] = metadata
                        json_data = (
                            self._full_response_json(response, include_body=False)
                            if response_settings.full_response
                            else item.json_data
                        )
                        output.append(WorkflowItem(json=json_data, binary=binary))
                        paired_input_indexes.append(item_index)
                        continue

                    if response_format == "text":
                        if not response_body.strip():
                            text_value = ""
                        elif response_json is not _NOT_JSON:
                            text_value = json.dumps(
                                response_json,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        else:
                            text_value = await self._decode_text(response, response_body)
                        if response_settings.full_response:
                            json_data = self._full_response_json(
                                response,
                                body_field=response_settings.output_property_name,
                                body=text_value,
                                include_body=False,
                            )
                        else:
                            json_data = {response_settings.output_property_name: text_value}
                        output.append(WorkflowItem(json=json_data))
                        paired_input_indexes.append(item_index)
                        continue

                    if response_settings.full_response:
                        output.append(
                            WorkflowItem(
                                json=self._full_response_json(response, body=response_json)
                            )
                        )
                        paired_input_indexes.append(item_index)
                    else:
                        response_items = self._json_items(response_json, node.name)
                        output.extend(WorkflowItem(json=value) for value in response_items)
                        paired_input_indexes.extend(item_index for _ in response_items)

        return NodeResult(
            outputs=(tuple(output),),
            paired_input_indexes=(tuple(paired_input_indexes),),
        )

    def _resolve_parameters(
        self,
        parameters: list[HttpParameter],
        item: WorkflowItem,
        item_index: int,
        context: NodeExecutionContext,
    ) -> dict[str, str]:
        """Разрешить статические значения и простые `$json` expressions."""
        resolved: dict[str, str] = {}
        for parameter in parameters:
            value = self._expressions.evaluate(
                parameter.value, item, item_index=item_index, context=context
            )
            if isinstance(value, (dict, list)):
                raise UnsupportedNodeConfigurationError(
                    f"HTTP-параметр {parameter.name!r} должен быть скалярным значением."
                )
            resolved[parameter.name] = str(value)
        return resolved

    @staticmethod
    async def _read_limited_body(response: aiohttp.ClientResponse, node_name: str) -> bytes:
        """Read the response body without allowing an unbounded download."""
        body = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise UnsupportedNodeConfigurationError(
                    f"HTTP Request {node_name!r} получил ответ больше 50 MiB."
                )
        return bytes(body)

    @staticmethod
    def _resolve_response_format(requested: str, content_type: str) -> str:
        """Select the explicit response format or n8n's autodetect result."""
        if requested != "autodetect":
            return requested
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type == "application/json":
            return "json"
        if any(media_type.startswith(binary_type) for binary_type in _BINARY_CONTENT_TYPES):
            return "file"
        return "text"

    @staticmethod
    def _is_json_content_type(content_type: str) -> bool:
        """Match JSON media types used by explicit Text and Autodetect responses."""
        media_type = content_type.partition(";")[0].strip().lower()
        return media_type == "application/json" or media_type.endswith("+json")

    @staticmethod
    def _parse_json_body(
        body: bytes,
        *,
        node_name: str,
        required: bool,
        fallback_to_empty: bool,
    ) -> Any:
        """Parse a JSON response, preserving n8n's empty-body and neverError rules."""
        if not body.strip():
            return {} if required or fallback_to_empty else _NOT_JSON
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if fallback_to_empty:
                return {}
            if not required:
                return _NOT_JSON
            raise UnsupportedNodeConfigurationError(
                f"HTTP Request {node_name!r} получил некорректный JSON; "
                "выберите Response Format: Text для текстового ответа."
            ) from error

    @staticmethod
    async def _decode_text(response: aiohttp.ClientResponse, body: bytes) -> str:
        """Decode plain text using its declared charset, defaulting to UTF-8."""
        encoding = response.charset or "utf-8"
        return body.decode(encoding)

    @staticmethod
    def _json_items(value: Any, node_name: str) -> list[Any]:
        """Expand JSON arrays to n8n items and normalize null items to empty objects."""
        values = value if isinstance(value, list) else [value]
        return [{} if item is None else item for item in values]

    @staticmethod
    def _full_response_json(
        response: aiohttp.ClientResponse,
        *,
        body: Any = None,
        body_field: str | None = None,
        include_body: bool = True,
    ) -> dict[str, Any]:
        """Return the n8n fullResponse fields with normalized header names."""
        result: dict[str, Any] = {
            "headers": {key.lower(): value for key, value in response.headers.items()},
            "statusCode": response.status,
            "statusMessage": response.reason or "",
        }
        if include_body:
            result["body"] = {} if body is _NOT_JSON else body
        elif body_field is not None:
            result[body_field] = body
        return result

    @staticmethod
    def _file_metadata(
        body: bytes,
        *,
        content_type: str,
        content_disposition: str | None,
        request_url: str,
    ) -> dict[str, Any]:
        """Create n8n-compatible inline binary metadata for the response body."""
        media_type = content_type.partition(";")[0].strip().lower() or "application/octet-stream"
        disposition = Message()
        if content_disposition:
            disposition["content-disposition"] = content_disposition
        filename = disposition.get_filename()
        if filename:
            filename = unquote(filename)
        else:
            filename = PurePosixPath(urlsplit(request_url).path).name or "response"
        filename = PurePosixPath(filename.replace("\\", "/")).name
        extension = PurePosixPath(filename).suffix.lstrip(".")
        if not extension:
            extension = mimetypes.guess_extension(media_type, strict=False) or ""
            extension = extension.lstrip(".")
        return {
            "data": base64.b64encode(body).decode("ascii"),
            "mimeType": media_type,
            "fileName": filename,
            "fileExtension": extension,
            "fileSize": len(body),
        }


_NOT_JSON = object()
