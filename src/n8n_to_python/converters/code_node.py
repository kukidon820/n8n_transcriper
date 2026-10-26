"""Выполнение Python-варианта n8n Code node в явном runtime-запуске."""

import asyncio
import textwrap
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from ..domain.models.workflow import WorkflowNode
from ..runtime.executors import NodeExecutionContext
from ..runtime.models import NodeResult, WorkflowItem
from .errors import UnsupportedNodeConfigurationError, UnsupportedNodeFeatureError
from .nodes import _StrictParameters


class PythonCodeParameters(_StrictParameters):
    """Python-only Code node subset; JavaScript source is rejected."""

    language: Literal["python", "pythonNative", "javaScript"] = "javaScript"
    mode: Literal["runOnceForAllItems", "runOnceForEachItem"] = "runOnceForAllItems"
    python_code: str | None = Field(default=None, alias="pythonCode")
    code: str | None = None
    js_code: str | None = Field(default=None, alias="jsCode")

    @model_validator(mode="after")
    def require_python_source(self) -> Self:
        """Require one Python source field and reject mixed language settings."""
        if self.language == "javaScript":
            if self.js_code is None or self.python_code is not None or self.code is not None:
                raise ValueError("JavaScript Code должен содержать только поле jsCode.")
            return self
        if self.js_code is not None:
            raise ValueError("Python Code не должен содержать поле jsCode.")
        if (self.python_code is None) == (self.code is None):
            raise ValueError("Укажите ровно одно поле pythonCode или code.")
        return self

    @property
    def source(self) -> str:
        """Return the configured Python source after validation."""
        return self.python_code if self.python_code is not None else self.code or ""


class PythonCodeExecutor:
    """Выполнить доверенный Python-скрипт из Code node для items."""

    is_trigger = False

    def __init__(self, parameters: PythonCodeParameters):
        self._parameters = parameters
        if self._parameters.language == "javaScript":
            raise UnsupportedNodeFeatureError(
                "Узел типа n8n-nodes-base.code: JavaScript Code не поддержан; "
                "преобразуется только language=pythonNative."
            )
        try:
            compile(self._function_source(parameters.source), "<n8n-python-code>", "exec")
        except SyntaxError as error:
            raise UnsupportedNodeConfigurationError(
                f"Python Code содержит синтаксическую ошибку: {error.msg}."
            ) from error

    async def execute(
        self,
        node: WorkflowNode,
        items: tuple[WorkflowItem, ...],
        context: NodeExecutionContext,
    ) -> NodeResult:
        """Запустить пользовательский код в worker thread и проверить выходные items."""
        if self._parameters.mode == "runOnceForAllItems":
            returned = await asyncio.to_thread(
                self._run_code,
                self._parameters.source,
                _items_payload(items),
                None,
            )
            results = _normalize_outputs(returned)
        else:
            results = []
            for item in items:
                returned = await asyncio.to_thread(
                    self._run_code,
                    self._parameters.source,
                    [_item_payload(item)],
                    _item_payload(item),
                )
                if returned is None:
                    continue
                results.extend(_normalize_outputs(returned, fallback_binary=item.binary))
        return NodeResult(outputs=(tuple(results),))

    @staticmethod
    def _function_source(source: str) -> str:
        """Place n8n-style `return` statements inside a callable function."""
        body = textwrap.indent(source or "pass", "    ")
        return f"def __n8n_code__(_items, _item):\n{body}\n"

    @classmethod
    def _run_code(
        cls,
        source: str,
        items_payload: list[dict[str, Any]],
        item_payload: dict[str, Any] | None,
    ) -> Any:
        """Compile and call user Python source; only trusted workflows may be run."""
        namespace: dict[str, Any] = {}
        exec(compile(cls._function_source(source), "<n8n-python-code>", "exec"), namespace)
        return namespace["__n8n_code__"](items_payload, item_payload)


def _item_payload(item: WorkflowItem) -> dict[str, Any]:
    """Expose the n8n-style JSON/binary wrapper to the Python Code node."""
    payload: dict[str, Any] = {"json": item.json_data}
    if item.binary is not None:
        payload["binary"] = item.binary
    return payload


def _items_payload(items: tuple[WorkflowItem, ...]) -> list[dict[str, Any]]:
    """Convert runtime items to the `_items` variable supported by this adapter."""
    return [_item_payload(item) for item in items]


def _normalize_outputs(
    returned: Any,
    *,
    fallback_binary: dict[str, Any] | None = None,
) -> list[WorkflowItem]:
    """Convert Code node return values to validated runtime items."""
    values = returned if isinstance(returned, list) else [returned]
    outputs: list[WorkflowItem] = []
    for value in values:
        if isinstance(value, WorkflowItem):
            outputs.append(value)
            continue
        if not isinstance(value, dict):
            raise UnsupportedNodeConfigurationError(
                "Python Code должен вернуть словарь или список словарей."
            )
        if "json" in value:
            json_data = value["json"]
            binary = value.get("binary", fallback_binary)
        else:
            json_data = value
            binary = fallback_binary
        if not isinstance(json_data, dict):
            raise UnsupportedNodeConfigurationError(
                "Поле json результата Python Code должно быть объектом."
            )
        outputs.append(WorkflowItem(json=json_data, binary=binary))
    return outputs
