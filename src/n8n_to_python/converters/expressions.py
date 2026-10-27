"""Безопасный интерпретатор небольшого, явно ограниченного поднабора n8n expressions."""

import ast
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..runtime.models import WorkflowItem
from .errors import UnsupportedNodeConfigurationError

_TOKEN_PATTERN = re.compile(
    r"\s+|"
    r"(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|"
    r"(?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)|"
    r"(?P<variable>\$[A-Za-z_]\w*|\$)|"
    r"(?P<identifier>[A-Za-z_]\w*)|"
    r"(?P<operator>===|!==|\?\?|&&|\|\||==|!=|>=|<=|[+*/%><!?:.,()\[\]{}-])"
)

_LUXON_FORMAT_TOKENS = {
    "yyyy": lambda value: f"{value.year:04d}",
    "yy": lambda value: f"{value.year % 100:02d}",
    "LL": lambda value: f"{value.month:02d}",
    "L": lambda value: str(value.month),
    "dd": lambda value: f"{value.day:02d}",
    "d": lambda value: str(value.day),
    "HH": lambda value: f"{value.hour:02d}",
    "H": lambda value: str(value.hour),
    "hh": lambda value: f"{(value.hour - 1) % 12 + 1:02d}",
    "h": lambda value: str((value.hour - 1) % 12 + 1),
    "mm": lambda value: f"{value.minute:02d}",
    "m": lambda value: str(value.minute),
    "ss": lambda value: f"{value.second:02d}",
    "s": lambda value: str(value.second),
    "SSS": lambda value: f"{value.microsecond // 1000:03d}",
    "ZZ": lambda value: value.strftime("%z")[:3] + ":" + value.strftime("%z")[3:],
}
_LUXON_FORMAT_TOKEN_PATTERN = re.compile(
    "|".join(sorted(map(re.escape, _LUXON_FORMAT_TOKENS), key=len, reverse=True))
)
_LUXON_FORMAT_LETTERS = re.compile(r"[A-Za-z]+")


def _current_datetime(context: Any) -> datetime:
    """Return `$now` in the configured workflow timezone, defaulting to UTC."""
    services = getattr(context, "services", {}) if context is not None else {}
    timezone_name = services.get("timezone", "UTC")
    if not isinstance(timezone_name, str):
        raise UnsupportedNodeConfigurationError("Настройка часового пояса должна быть строкой.")
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise UnsupportedNodeConfigurationError(
            f"Неизвестный часовой пояс для $now: {timezone_name!r}."
        ) from error


def _format_luxon_datetime(value: datetime, pattern: str) -> str:
    """Format `$now` using the commonly used, explicit Luxon token subset."""
    pieces: list[str] = []
    position = 0
    while position < len(pattern):
        if pattern[position] == "'":
            if position + 1 < len(pattern) and pattern[position + 1] == "'":
                pieces.append("'")
                position += 2
                continue
            end = pattern.find("'", position + 1)
            if end < 0:
                raise UnsupportedNodeConfigurationError(
                    "$now.toFormat(): незакрытый текстовый фрагмент в шаблоне."
                )
            pieces.append(pattern[position + 1 : end])
            position = end + 1
            continue

        token_match = _LUXON_FORMAT_TOKEN_PATTERN.match(pattern, position)
        if token_match is not None:
            token = token_match.group()
            pieces.append(_LUXON_FORMAT_TOKENS[token](value))
            position = token_match.end()
            continue
        if pattern[position].isalpha():
            unsupported = _LUXON_FORMAT_LETTERS.match(pattern, position)
            token = unsupported.group() if unsupported else pattern[position]
            raise UnsupportedNodeConfigurationError(
                f"Токен даты {token!r} в $now.toFormat() пока не поддерживается."
            )
        pieces.append(pattern[position])
        position += 1
    return "".join(pieces)


@dataclass(frozen=True, slots=True)
class _Token:
    """Один токен лексически проверенного выражения."""

    kind: str
    value: Any


class _ExpressionNode:
    """Базовый класс узла синтаксического дерева expressions."""


@dataclass(frozen=True, slots=True)
class _Literal(_ExpressionNode):
    value: Any


@dataclass(frozen=True, slots=True)
class _Name(_ExpressionNode):
    value: str


@dataclass(frozen=True, slots=True)
class _Member(_ExpressionNode):
    parent: _ExpressionNode
    name: str


@dataclass(frozen=True, slots=True)
class _Index(_ExpressionNode):
    parent: _ExpressionNode
    index: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _Unary(_ExpressionNode):
    operator: str
    operand: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _Binary(_ExpressionNode):
    operator: str
    left: _ExpressionNode
    right: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _Conditional(_ExpressionNode):
    condition: _ExpressionNode
    when_true: _ExpressionNode
    when_false: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _Call(_ExpressionNode):
    function: _ExpressionNode
    arguments: tuple[_ExpressionNode, ...]


@dataclass(frozen=True, slots=True)
class _Array(_ExpressionNode):
    values: tuple[_ExpressionNode, ...]


@dataclass(frozen=True, slots=True)
class _Object(_ExpressionNode):
    fields: tuple[tuple[str, _ExpressionNode], ...]


class _ExpressionParser:
    """Разобрать токены без вызова Python eval или исполнения произвольного кода."""

    _precedence = {
        "??": 1,
        "||": 2,
        "&&": 3,
        "==": 4,
        "===": 4,
        "!=": 4,
        "!==": 4,
        ">": 4,
        ">=": 4,
        "<": 4,
        "<=": 4,
        "+": 5,
        "-": 5,
        "*": 6,
        "/": 6,
        "%": 6,
    }

    def __init__(self, expression: str):
        """Разобрать expression в поток скаляров, имён и операторов."""
        self._tokens = self._tokenize(expression)
        self._position = 0

    def parse(self) -> _ExpressionNode:
        """Вернуть корень дерева и отклонить лишний или незакрытый синтаксис."""
        if not self._tokens:
            raise ValueError("Выражение пустое.")
        result = self._parse_expression(0)
        if self._position != len(self._tokens):
            token = self._tokens[self._position]
            raise ValueError(f"Лишний токен {token.value!r}.")
        return result

    @staticmethod
    def _tokenize(expression: str) -> list[_Token]:
        """Разбить строку и прекратить разбор при неизвестном символе."""
        tokens: list[_Token] = []
        position = 0
        while position < len(expression):
            match = _TOKEN_PATTERN.match(expression, position)
            if match is None:
                raise ValueError(f"Неизвестный символ на позиции {position}.")
            position = match.end()
            if match.group(0).isspace():
                continue
            if match.lastgroup == "string":
                tokens.append(_Token("literal", ast.literal_eval(match.group(0))))
            elif match.lastgroup == "number":
                raw_number = match.group(0)
                number = (
                    float(raw_number)
                    if any(char in raw_number for char in ".eE")
                    else int(raw_number)
                )
                tokens.append(_Token("literal", number))
            elif match.lastgroup in {"variable", "identifier"}:
                tokens.append(_Token(match.lastgroup, match.group(0)))
            else:
                tokens.append(_Token("operator", match.group(0)))
        return tokens

    def _parse_expression(self, minimum_precedence: int) -> _ExpressionNode:
        """Разобрать выражение с приоритетом операторов и условным тернарным оператором."""
        left = self._parse_prefix()
        while self._position < len(self._tokens):
            token = self._tokens[self._position]
            if token.value in {".", "[", "("}:
                left = self._parse_postfix(left)
                continue
            if token.value == "?" and minimum_precedence == 0:
                self._position += 1
                when_true = self._parse_expression(0)
                self._expect(":")
                when_false = self._parse_expression(0)
                left = _Conditional(left, when_true, when_false)
                continue
            precedence = self._precedence.get(token.value)
            if precedence is None or precedence < minimum_precedence:
                break
            self._position += 1
            right = self._parse_expression(precedence + 1)
            left = _Binary(token.value, left, right)
        return left

    def _parse_prefix(self) -> _ExpressionNode:
        """Разобрать литерал, переменную, массив, скобки или унарный оператор."""
        token = self._take()
        if token.kind == "literal":
            return _Literal(token.value)
        if token.kind in {"variable", "identifier"}:
            if token.value == "true":
                return _Literal(True)
            if token.value == "false":
                return _Literal(False)
            if token.value in {"null", "undefined"}:
                return _Literal(None)
            return _Name(token.value)
        if token.value in {"!", "+", "-"}:
            return _Unary(token.value, self._parse_expression(7))
        if token.value == "{":
            fields: list[tuple[str, _ExpressionNode]] = []
            if not self._peek("}"):
                while True:
                    key = self._take()
                    if key.kind == "identifier":
                        key_name = key.value
                    elif key.kind == "literal" and isinstance(key.value, str):
                        key_name = key.value
                    else:
                        raise ValueError("Ключ object literal должен быть именем или строкой.")
                    self._expect(":")
                    fields.append((key_name, self._parse_expression(0)))
                    if not self._peek(","):
                        break
                    self._position += 1
                    if self._peek("}"):
                        break
            self._expect("}")
            return _Object(tuple(fields))
        if token.value == "(":
            expression = self._parse_expression(0)
            self._expect(")")
            return expression
        if token.value == "[":
            values: list[_ExpressionNode] = []
            if not self._peek("]"):
                while True:
                    values.append(self._parse_expression(0))
                    if not self._peek(","):
                        break
                    self._position += 1
            self._expect("]")
            return _Array(tuple(values))
        raise ValueError(f"Токен {token.value!r} нельзя использовать в начале выражения.")

    def _parse_postfix(self, parent: _ExpressionNode) -> _ExpressionNode:
        """Разобрать безопасный доступ к полю, индекс или вызов разрешённой функции."""
        token = self._take()
        if token.value == ".":
            name = self._take()
            if name.kind != "identifier":
                raise ValueError("После точки ожидалось имя поля.")
            return _Member(parent, name.value)
        if token.value == "[":
            index = self._parse_expression(0)
            self._expect("]")
            return _Index(parent, index)
        arguments: list[_ExpressionNode] = []
        if not self._peek(")"):
            while True:
                arguments.append(self._parse_expression(0))
                if not self._peek(","):
                    break
                self._position += 1
        self._expect(")")
        return _Call(parent, tuple(arguments))

    def _take(self) -> _Token:
        """Взять следующий токен или сообщить о незавершённом выражении."""
        if self._position >= len(self._tokens):
            raise ValueError("Выражение неожиданно закончилось.")
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _expect(self, value: str) -> None:
        """Проверить обязательный разделитель выражения."""
        token = self._take()
        if token.value != value:
            raise ValueError(f"Ожидался токен {value!r}, получен {token.value!r}.")

    def _peek(self, value: str) -> bool:
        """Проверить следующий токен без изменения позиции."""
        return self._position < len(self._tokens) and self._tokens[self._position].value == value


@dataclass(frozen=True, slots=True)
class _Namespace:
    """Закрытое пространство имён разрешённых встроенных функций."""

    name: str


@dataclass(frozen=True, slots=True)
class _Function:
    """Маркер функции, вызываемой только явным evaluator dispatch."""

    name: str
    receiver: Any = None


@dataclass(frozen=True, slots=True)
class _NodeReference:
    """A safe reference to one named node's recorded output items."""

    name: str


class N8nExpressionEvaluator:
    """Вычислить безопасный поднабор expressions для item и node references."""

    _interpolation = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.DOTALL)

    def evaluate(
        self,
        value: Any,
        item: WorkflowItem,
        *,
        item_index: int = 0,
        context: Any = None,
    ) -> Any:
        """Разобрать expression или текст с interpolation markers, сохранив литералы."""
        if isinstance(value, dict):
            return {
                key: self.evaluate(child, item, item_index=item_index, context=context)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self.evaluate(child, item, item_index=item_index, context=context)
                for child in value
            ]
        if not isinstance(value, str):
            return value
        if value.startswith("={{") and value.endswith("}}"):
            return self._evaluate_source(value[3:-2].strip(), item, item_index, context)
        if value.startswith("{{") and value.endswith("}}"):
            return self._evaluate_source(value[2:-2].strip(), item, item_index, context)
        if "{{" in value:
            return self._interpolation.sub(
                lambda match: self._js_string(
                    self._evaluate_source(match.group(1), item, item_index, context)
                ),
                value,
            )
        return value

    def _evaluate_source(
        self, source: str, item: WorkflowItem, item_index: int, context: Any
    ) -> Any:
        """Разобрать выражение и превратить синтаксические ошибки в понятную диагностику."""
        try:
            expression = _ExpressionParser(source).parse()
            return self._evaluate_node(expression, item, item_index, context)
        except UnsupportedNodeConfigurationError:
            raise
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            raise UnsupportedNodeConfigurationError(
                f"Не удалось вычислить n8n expression {source!r}: {error}"
            ) from error

    def _evaluate_node(
        self, node: _ExpressionNode, item: WorkflowItem, item_index: int, context: Any
    ) -> Any:
        """Вычислить AST-узел только через разрешённые типы и операции."""
        if isinstance(node, _Literal):
            return node.value
        if isinstance(node, _Name):
            roots = {
                "$json": item.json_data,
                "$binary": item.binary or {},
                "$itemIndex": item_index,
                "$now": _current_datetime(context),
                "$input": _Namespace("$input"),
                "$": _Namespace("$"),
                "JSON": _Namespace("JSON"),
                "Object": _Namespace("Object"),
                "String": _Function("String"),
                "Number": _Function("Number"),
                "Boolean": _Function("Boolean"),
            }
            if node.value not in roots:
                raise UnsupportedNodeConfigurationError(
                    f"Переменная или функция {node.value!r} не входит в поддержанный expression subset."
                )
            return roots[node.value]
        if isinstance(node, _Member):
            return self._resolve_member(
                self._evaluate_node(node.parent, item, item_index, context),
                node.name,
                item,
                context,
            )
        if isinstance(node, _Index):
            parent = self._evaluate_node(node.parent, item, item_index, context)
            index = self._evaluate_node(node.index, item, item_index, context)
            if isinstance(parent, dict) and isinstance(index, str):
                if index not in parent:
                    raise UnsupportedNodeConfigurationError(
                        f"Поле {index!r} отсутствует в expression input."
                    )
                return parent[index]
            if isinstance(parent, (list, str)) and isinstance(index, int):
                try:
                    return parent[index]
                except IndexError as error:
                    raise UnsupportedNodeConfigurationError(
                        f"Индекс {index} выходит за границы expression input."
                    ) from error
            raise UnsupportedNodeConfigurationError("Этот тип expression index не поддерживается.")
        if isinstance(node, _Array):
            return [self._evaluate_node(child, item, item_index, context) for child in node.values]
        if isinstance(node, _Object):
            return {
                key: self._evaluate_node(value, item, item_index, context)
                for key, value in node.fields
            }
        if isinstance(node, _Unary):
            operand = self._evaluate_node(node.operand, item, item_index, context)
            if node.operator == "!":
                return not self._js_truthy(operand)
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                raise UnsupportedNodeConfigurationError(
                    f"Унарный оператор {node.operator!r} требует число."
                )
            return operand if node.operator == "+" else -operand
        if isinstance(node, _Binary):
            return self._evaluate_binary(node, item, item_index, context)
        if isinstance(node, _Conditional):
            condition = self._evaluate_node(node.condition, item, item_index, context)
            branch = node.when_true if self._js_truthy(condition) else node.when_false
            return self._evaluate_node(branch, item, item_index, context)
        if isinstance(node, _Call):
            function = self._evaluate_node(node.function, item, item_index, context)
            arguments = [
                self._evaluate_node(arg, item, item_index, context) for arg in node.arguments
            ]
            return self._call(function, arguments, item, context)
        raise UnsupportedNodeConfigurationError("Обнаружена неизвестная форма expression.")

    def _evaluate_binary(
        self, node: _Binary, item: WorkflowItem, item_index: int, context: Any
    ) -> Any:
        """Вычислить оператор с JavaScript-подобным short circuit и coalescing."""
        left = self._evaluate_node(node.left, item, item_index, context)
        if node.operator == "&&":
            return (
                self._evaluate_node(node.right, item, item_index, context)
                if self._js_truthy(left)
                else left
            )
        if node.operator == "||":
            return (
                left
                if self._js_truthy(left)
                else self._evaluate_node(node.right, item, item_index, context)
            )
        if node.operator == "??":
            return (
                left
                if left is not None
                else self._evaluate_node(node.right, item, item_index, context)
            )
        right = self._evaluate_node(node.right, item, item_index, context)
        if node.operator == "+":
            if isinstance(left, str) or isinstance(right, str):
                return self._js_string(left) + self._js_string(right)
            return self._number(left) + self._number(right)
        if node.operator in {"-", "*", "/", "%"}:
            lhs, rhs = self._number(left), self._number(right)
            if node.operator == "-":
                return lhs - rhs
            if node.operator == "*":
                return lhs * rhs
            if node.operator == "/":
                return lhs / rhs
            return lhs % rhs
        if node.operator in {"==", "===", "!=", "!=="}:
            equal = type(left) is type(right) and left == right
            return not equal if node.operator in {"!=", "!=="} else equal
        if node.operator in {">", ">=", "<", "<="}:
            if type(left) is not type(right) or not isinstance(left, (str, int, float)):
                raise UnsupportedNodeConfigurationError(
                    f"Оператор {node.operator!r} требует два значения одного простого типа."
                )
            return {
                ">": left > right,
                ">=": left >= right,
                "<": left < right,
                "<=": left <= right,
            }[node.operator]
        raise UnsupportedNodeConfigurationError(f"Оператор {node.operator!r} не поддерживается.")

    def _resolve_member(self, parent: Any, name: str, item: WorkflowItem, context: Any) -> Any:
        """Разрешить JSON key, строковую длину или whitelist-функцию namespace."""
        if isinstance(parent, dict):
            if name not in parent:
                raise UnsupportedNodeConfigurationError(
                    f"Поле {name!r} отсутствует в expression input."
                )
            return parent[name]
        if isinstance(parent, WorkflowItem):
            if name == "json":
                return parent.json_data
            if name == "binary":
                return parent.binary or {}
            raise UnsupportedNodeConfigurationError(
                f"Свойство WorkflowItem {name!r} не поддерживается."
            )
        if isinstance(parent, _NodeReference):
            if name == "item":
                history = self._node_history(context, parent.name)
                try:
                    index = item.lineage[parent.name]
                    return history[index]
                except (KeyError, IndexError) as error:
                    raise UnsupportedNodeConfigurationError(
                        f"Для expression $('{parent.name}').item нет однозначной связи с текущим item."
                    ) from error
            if name in {"first", "last", "all"}:
                return _Function(f"Node.{name}", parent.name)
            raise UnsupportedNodeConfigurationError(
                f"Свойство узла {parent.name!r}.{name} не поддерживается."
            )
        if isinstance(parent, _Namespace):
            allowed = {
                "JSON": {
                    "stringify": _Function("JSON.stringify"),
                    "parse": _Function("JSON.parse"),
                },
                "Object": {"keys": _Function("Object.keys")},
            }
            if parent.name == "$":
                return _Function("Node.reference")
            if parent.name == "$input":
                if name == "item":
                    return item
                if name in {"first", "last", "all"}:
                    return _Function(f"Input.{name}")
            try:
                return allowed[parent.name][name]
            except KeyError as error:
                raise UnsupportedNodeConfigurationError(
                    f"Свойство {parent.name}.{name} не поддерживается."
                ) from error
        if isinstance(parent, list) and name == "join":
            return _Function("Array.join", parent)
        if isinstance(parent, list) and name == "includes":
            return _Function("Array.includes", parent)
        if isinstance(parent, list) and name in {"first", "last"}:
            return _Function(f"Array.{name}", parent)
        if isinstance(parent, str) and name in {
            "toUpperCase",
            "toLowerCase",
            "trim",
            "startsWith",
            "endsWith",
            "includes",
        }:
            return _Function(f"String.{name}", parent)
        if isinstance(parent, (list, str)) and name == "length":
            return len(parent)
        if isinstance(parent, datetime) and name == "toFormat":
            return _Function("DateTime.toFormat", parent)
        raise UnsupportedNodeConfigurationError(
            f"Доступ к свойству {name!r} этого expression value не поддерживается."
        )

    def _call(
        self,
        function: Any,
        arguments: list[Any],
        item: WorkflowItem,
        context: Any,
    ) -> Any:
        """Выполнить одну из встроенных функций, разрешённых evaluator-ом."""
        if isinstance(function, _Namespace) and function.name == "$" and len(arguments) == 1:
            if not isinstance(arguments[0], str) or not arguments[0]:
                raise UnsupportedNodeConfigurationError("$('node') требует имя узла строкой.")
            self._node_history(context, arguments[0])
            return _NodeReference(arguments[0])
        if not isinstance(function, _Function):
            raise UnsupportedNodeConfigurationError("Вызов произвольной функции запрещён.")
        if function.name == "Node.reference" and len(arguments) == 1:
            if not isinstance(arguments[0], str) or not arguments[0]:
                raise UnsupportedNodeConfigurationError("$('node') требует имя узла строкой.")
            self._node_history(context, arguments[0])
            return _NodeReference(arguments[0])
        if function.name.startswith("Input."):
            input_items = tuple(getattr(context, "input_items", ()) or (item,))
            return self._select_item(function.name.removeprefix("Input."), input_items, arguments)
        if function.name.startswith("Node."):
            history = self._node_history(context, str(function.receiver))
            return self._select_item(function.name.removeprefix("Node."), history, arguments)
        if function.name == "String" and len(arguments) == 1:
            return self._js_string(arguments[0])
        if function.name == "Number" and len(arguments) == 1:
            return self._number(arguments[0])
        if function.name == "Boolean" and len(arguments) == 1:
            return self._js_truthy(arguments[0])
        if function.name == "DateTime.toFormat" and len(arguments) == 1:
            if not isinstance(arguments[0], str):
                raise UnsupportedNodeConfigurationError("$now.toFormat() ожидает строковый шаблон.")
            return _format_luxon_datetime(function.receiver, arguments[0])
        if function.name == "JSON.stringify" and len(arguments) == 1:
            return json.dumps(arguments[0], ensure_ascii=False, separators=(",", ":"))
        if function.name == "JSON.parse" and len(arguments) == 1 and isinstance(arguments[0], str):
            return json.loads(arguments[0])
        if (
            function.name == "Object.keys"
            and len(arguments) == 1
            and isinstance(arguments[0], dict)
        ):
            return list(arguments[0])
        if function.name == "Array.join" and len(arguments) <= 1:
            separator = self._js_string(arguments[0]) if arguments else ","
            return separator.join(self._js_string(value) for value in function.receiver)
        if function.name == "Array.includes" and len(arguments) == 1:
            return arguments[0] in function.receiver
        if function.name in {"Array.first", "Array.last"} and not arguments:
            if not function.receiver:
                raise UnsupportedNodeConfigurationError(
                    "Нельзя выбрать first/last из пустого массива items."
                )
            return function.receiver[0] if function.name == "Array.first" else function.receiver[-1]
        if function.name == "String.toUpperCase" and not arguments:
            return function.receiver.upper()
        if function.name == "String.toLowerCase" and not arguments:
            return function.receiver.lower()
        if function.name == "String.trim" and not arguments:
            return function.receiver.strip()
        if (
            function.name in {"String.startsWith", "String.endsWith", "String.includes"}
            and len(arguments) == 1
            and isinstance(arguments[0], str)
        ):
            operation = {
                "String.startsWith": function.receiver.startswith,
                "String.endsWith": function.receiver.endswith,
                "String.includes": lambda value: value in function.receiver,
            }[function.name]
            return operation(arguments[0])
        raise UnsupportedNodeConfigurationError(
            f"Вызов {function.name} с такой сигнатурой не поддерживается."
        )

    @staticmethod
    def _select_item(selector: str, items: tuple[WorkflowItem, ...], arguments: list[Any]) -> Any:
        """Resolve the common first/last/all selectors for node and input items."""
        if arguments:
            raise UnsupportedNodeConfigurationError(
                "Индексы ветки и запуска в first/last/all пока не поддерживаются."
            )
        if selector == "all":
            return [
                {
                    "json": value.json_data,
                    **({"binary": value.binary} if value.binary is not None else {}),
                }
                for value in items
            ]
        if not items:
            raise UnsupportedNodeConfigurationError("Expression запрашивает пустой список items.")
        return items[0] if selector == "first" else items[-1]

    @staticmethod
    def _node_history(context: Any, node_name: str) -> tuple[WorkflowItem, ...]:
        """Get an earlier node's recorded outputs or explain the missing reference."""
        history = getattr(context, "node_outputs", {}) if context is not None else {}
        try:
            return tuple(history[node_name])
        except KeyError as error:
            raise UnsupportedNodeConfigurationError(
                f"Expression ссылается на узел {node_name!r}, который ещё не выполнялся."
            ) from error

    @staticmethod
    def _js_truthy(value: Any) -> bool:
        """Применить truthiness для JSON-совместимых JavaScript значений."""
        if value is None or value is False or value == 0 or value == "":
            return False
        return True

    @staticmethod
    def _js_string(value: Any) -> str:
        """Преобразовать простое JSON-значение в строку в стиле JavaScript."""
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, dict):
            return "[object Object]"
        if isinstance(value, list):
            return ",".join(N8nExpressionEvaluator._js_string(item) for item in value)
        return str(value)

    @staticmethod
    def _number(value: Any) -> int | float:
        """Преобразовать поддержанное числовое значение или вернуть ошибку."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return float(value) if "." in value else int(value)
        raise UnsupportedNodeConfigurationError("Expression value нельзя преобразовать в число.")
