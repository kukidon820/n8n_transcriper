"""Безопасный интерпретатор небольшого, явно ограниченного поднабора n8n expressions."""

import ast
import base64
import calendar
import json
import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cmp_to_key
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..runtime.models import WorkflowItem
from .errors import UnsupportedNodeConfigurationError

_TOKEN_PATTERN = re.compile(
    r"\s+|"
    r"(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|"
    r"(?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)|"
    r"(?P<variable>\$[A-Za-z_]\w*|\$)|"
    r"(?P<identifier>[A-Za-z_]\w*)|"
    r"(?P<operator>\.\.\.|===|!==|\?\.|\?\?|&&|\|\||==|!=|>=|<=|=>|\*\*|"
    r"[=+*/%><!?:.,;()\[\]{}^-])"
)

_LUXON_FORMAT_TOKENS = {
    "yyyy": lambda value: f"{value.year:04d}",
    "yy": lambda value: f"{value.year % 100:02d}",
    "MMMM": lambda value: value.strftime("%B"),
    "MMM": lambda value: value.strftime("%b"),
    "cccc": lambda value: value.strftime("%A"),
    "LL": lambda value: f"{value.month:02d}",
    "L": lambda value: str(value.month),
    "MM": lambda value: f"{value.month:02d}",
    "M": lambda value: str(value.month),
    "dd": lambda value: f"{value.day:02d}",
    "d": lambda value: str(value.day),
    "EEEE": lambda value: value.strftime("%A"),
    "EEE": lambda value: value.strftime("%a"),
    "ccc": lambda value: value.strftime("%a"),
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
    optional: bool = False


@dataclass(frozen=True, slots=True)
class _Index(_ExpressionNode):
    parent: _ExpressionNode
    index: _ExpressionNode
    optional: bool = False


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
    optional: bool = False


@dataclass(frozen=True, slots=True)
class _Array(_ExpressionNode):
    values: tuple[_ExpressionNode, ...]


@dataclass(frozen=True, slots=True)
class _Object(_ExpressionNode):
    fields: tuple[tuple[str | _ExpressionNode | None, _ExpressionNode], ...]


@dataclass(frozen=True, slots=True)
class _RegexLiteral(_ExpressionNode):
    pattern: str
    flags: str


@dataclass(frozen=True, slots=True)
class _Template(_ExpressionNode):
    parts: tuple[str | _ExpressionNode, ...]


@dataclass(frozen=True, slots=True)
class _Spread(_ExpressionNode):
    value: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _BindingPattern:
    """A simple JavaScript callback parameter or destructuring pattern."""

    name: str | None = None
    array_items: tuple["_BindingPattern", ...] = ()
    object_items: tuple[tuple[str, "_BindingPattern"], ...] = ()


@dataclass(frozen=True, slots=True)
class _Arrow(_ExpressionNode):
    parameters: tuple[_BindingPattern, ...]
    body: _ExpressionNode


@dataclass(frozen=True, slots=True)
class _Block(_ExpressionNode):
    statements: tuple[tuple[str | None, _ExpressionNode], ...]


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
        "**": 7,
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
            if expression[position] == "`":
                template, position = _ExpressionParser._read_template_literal(expression, position)
                tokens.append(_Token("template", template))
                continue
            if expression[position] == "/" and _ExpressionParser._expects_operand(tokens):
                regex_match = _ExpressionParser._read_regex(expression, position)
                if regex_match is not None:
                    pattern, flags, position = regex_match
                    tokens.append(_Token("regex", (pattern, flags)))
                    continue
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

    @staticmethod
    def _expects_operand(tokens: list[_Token]) -> bool:
        """Distinguish a regular expression literal from the division operator."""
        if not tokens:
            return True
        previous = tokens[-1].value
        return previous in {
            "(",
            "[",
            "{",
            ",",
            ":",
            "?",
            "??",
            "&&",
            "||",
            "=",
            "=>",
            "!",
            "+",
            "-",
            "*",
            "/",
            "%",
            ">",
            ">=",
            "<",
            "<=",
        }

    @staticmethod
    def _read_regex(expression: str, start: int) -> tuple[str, str, int] | None:
        """Read a `/pattern/flags` literal without treating slashes in classes as terminators."""
        position = start + 1
        escaped = False
        in_character_class = False
        while position < len(expression):
            character = expression[position]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                in_character_class = True
            elif character == "]":
                in_character_class = False
            elif character == "/" and not in_character_class:
                end = position + 1
                while end < len(expression) and expression[end].isalpha():
                    end += 1
                return expression[start + 1 : position], expression[position + 1 : end], end
            position += 1
        return None

    @staticmethod
    def _read_template_literal(source: str, start: int) -> tuple[str, int]:
        """Считать шаблонную строку с вложенными `${...}` и другими шаблонами."""
        position = start + 1
        while position < len(source):
            if source[position] == "\\":
                position += 2
                continue
            if source.startswith("${", position):
                closing = _ExpressionParser._find_template_expression_end(source, position + 2)
                position = closing + 1
                continue
            if source[position] == "`":
                return source[start + 1 : position], position + 1
            position += 1
        raise ValueError("Незакрытая шаблонная строка.")

    @staticmethod
    def _find_template_expression_end(source: str, start: int) -> int:
        """Найти закрывающую фигурную скобку `${...}`, пропуская строки и regex."""
        position = start
        depth = 1
        quote_char: str | None = None
        escaped = False
        previous = ""
        while position < len(source):
            character = source[position]
            if quote_char is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote_char:
                    quote_char = None
                position += 1
                continue
            if character == "`":
                _, position = _ExpressionParser._read_template_literal(source, position)
                previous = "value"
                continue
            if character in {"'", '"'}:
                quote_char = character
                position += 1
                continue
            if character == "/" and previous in {
                "",
                "(",
                "[",
                "{",
                ",",
                ":",
                "?",
                "=",
                "!",
                "+",
                "-",
                "*",
            }:
                regex_result = _ExpressionParser._read_regex(source, position)
                if regex_result is not None:
                    _, _, position = regex_result
                    previous = "value"
                    continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return position
            if not character.isspace():
                previous = character
            position += 1
        raise ValueError("Незакрытое выражение ${...} в шаблонной строке.")

    def _parse_expression(self, minimum_precedence: int) -> _ExpressionNode:
        """Разобрать выражение с приоритетом операторов и условным тернарным оператором."""
        left = self._parse_prefix()
        while self._position < len(self._tokens):
            token = self._tokens[self._position]
            if token.value in {".", "?.", "[", "("}:
                left = self._parse_postfix(left)
                continue
            if token.value == "=>" and isinstance(left, _Name):
                self._position += 1
                left = _Arrow((_BindingPattern(name=left.value),), self._parse_arrow_body())
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
            right = self._parse_expression(precedence if token.value == "**" else precedence + 1)
            left = _Binary(token.value, left, right)
        return left

    def _parse_prefix(self) -> _ExpressionNode:
        """Разобрать литерал, переменную, массив, скобки или унарный оператор."""
        token = self._take()
        if token.kind == "literal":
            return _Literal(token.value)
        if token.kind == "template":
            return _Template(self._parse_template_parts(token.value))
        if token.kind == "regex":
            return _RegexLiteral(*token.value)
        if token.kind in {"variable", "identifier"}:
            if token.value == "true":
                return _Literal(True)
            if token.value == "false":
                return _Literal(False)
            if token.value in {"null", "undefined"}:
                return _Literal(None)
            if token.value == "typeof":
                return _Unary("typeof", self._parse_expression(8))
            if token.value == "new":
                constructor = self._take()
                if constructor.kind != "identifier":
                    raise ValueError("После new ожидалось имя конструктора.")
                return _Name(constructor.value)
            if self._peek("=>"):
                self._position += 1
                return _Arrow((_BindingPattern(name=token.value),), self._parse_arrow_body())
            return _Name(token.value)
        if token.value in {"!", "+", "-"}:
            return _Unary(token.value, self._parse_expression(8))
        if token.value == "...":
            return _Spread(self._parse_expression(8))
        if token.value == "{":
            fields: list[tuple[str | _ExpressionNode | None, _ExpressionNode]] = []
            if not self._peek("}"):
                while True:
                    if self._peek("..."):
                        spread = self._parse_prefix()
                        if not isinstance(spread, _Spread):
                            raise ValueError("Некорректный spread в объекте.")
                        fields.append((None, spread.value))
                    elif self._peek("["):
                        self._position += 1
                        key_expression = self._parse_expression(0)
                        self._expect("]")
                        self._expect(":")
                        fields.append((key_expression, self._parse_expression(0)))
                    else:
                        key = self._take()
                        if key.kind == "identifier":
                            key_name = key.value
                        elif key.kind == "literal" and isinstance(key.value, str):
                            key_name = key.value
                        else:
                            raise ValueError(
                                "Ключ object literal должен быть именем, строкой или expression."
                            )
                        if self._peek(":"):
                            self._position += 1
                            field_value = self._parse_expression(0)
                        else:
                            field_value = _Name(key_name)
                        fields.append((key_name, field_value))
                    if not self._peek(","):
                        break
                    self._position += 1
                    if self._peek("}"):
                        break
            self._expect("}")
            return _Object(tuple(fields))
        if token.value == "(":
            if self._parenthesized_arrow_ahead():
                parameters = self._parse_binding_parameters()
                self._expect("=>")
                return _Arrow(parameters, self._parse_arrow_body())
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
        optional = token.value == "?."
        if token.value in {".", "?."}:
            if optional and self._peek("["):
                self._position += 1
                index = self._parse_expression(0)
                self._expect("]")
                return _Index(parent, index, optional=True)
            if optional and self._peek("("):
                self._position += 1
                return self._parse_call_arguments(parent, optional=True)
            name = self._take()
            if name.kind != "identifier":
                raise ValueError("После точки ожидалось имя поля.")
            return _Member(parent, name.value, optional=optional)
        if token.value == "[":
            index = self._parse_expression(0)
            self._expect("]")
            return _Index(parent, index)
        return self._parse_call_arguments(parent)

    def _parse_call_arguments(self, parent: _ExpressionNode, *, optional: bool = False) -> _Call:
        """Parse call arguments after the opening parenthesis has been consumed."""
        arguments: list[_ExpressionNode] = []
        if not self._peek(")"):
            while True:
                arguments.append(self._parse_expression(0))
                if not self._peek(","):
                    break
                self._position += 1
        self._expect(")")
        return _Call(parent, tuple(arguments), optional=optional)

    def _parenthesized_arrow_ahead(self) -> bool:
        """Look through a parenthesized parameter list before parsing it as an expression."""
        depth = 1
        for index in range(self._position, len(self._tokens)):
            value = self._tokens[index].value
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth == 0:
                    return index + 1 < len(self._tokens) and self._tokens[index + 1].value == "=>"
        return False

    def _parse_binding_parameters(self) -> tuple[_BindingPattern, ...]:
        """Parse callback names and the array/object destructuring used in the guide."""
        parameters: list[_BindingPattern] = []
        if not self._peek(")"):
            while True:
                parameters.append(self._parse_binding_pattern())
                if not self._peek(","):
                    break
                self._position += 1
        self._expect(")")
        return tuple(parameters)

    def _parse_binding_pattern(self) -> _BindingPattern:
        """Parse a name or one-level/nested destructuring callback parameter."""
        token = self._take()
        if token.kind == "identifier":
            return _BindingPattern(name=token.value)
        if token.value == "[":
            items: list[_BindingPattern] = []
            if not self._peek("]"):
                while True:
                    items.append(self._parse_binding_pattern())
                    if not self._peek(","):
                        break
                    self._position += 1
            self._expect("]")
            return _BindingPattern(array_items=tuple(items))
        if token.value == "{":
            items: list[tuple[str, _BindingPattern]] = []
            if not self._peek("}"):
                while True:
                    key = self._take()
                    if key.kind != "identifier":
                        raise ValueError("В destructuring объекта ожидалось имя поля.")
                    if self._peek(":"):
                        self._position += 1
                        value = self._parse_binding_pattern()
                    else:
                        value = _BindingPattern(name=key.value)
                    items.append((key.value, value))
                    if not self._peek(","):
                        break
                    self._position += 1
            self._expect("}")
            return _BindingPattern(object_items=tuple(items))
        raise ValueError("Параметр callback должен быть именем или destructuring pattern.")

    def _parse_arrow_body(self) -> _ExpressionNode:
        """Разобрать arrow-expression или небольшой блок const/let/return."""
        if not self._peek("{"):
            return self._parse_expression(0)
        self._position += 1
        statements: list[tuple[str | None, _ExpressionNode]] = []
        while not self._peek("}"):
            if self._position >= len(self._tokens):
                raise ValueError("Незакрытый блок arrow-функции.")
            token = self._take()
            if token.value in {"const", "let", "var"}:
                name = self._take()
                if name.kind != "identifier":
                    raise ValueError("После объявления переменной ожидалось имя.")
                self._expect("=")
                statements.append((name.value, self._parse_expression(0)))
            elif token.value == "return":
                if self._peek(";") or self._peek("}"):
                    statements.append(("$return", _Literal(None)))
                else:
                    statements.append(("$return", self._parse_expression(0)))
            else:
                self._position -= 1
                statements.append((None, self._parse_expression(0)))
            if self._peek(";"):
                self._position += 1
            elif not self._peek("}"):
                raise ValueError("В блоке arrow-функции ожидалась точка с запятой.")
        self._expect("}")
        return _Block(tuple(statements))

    @staticmethod
    def _parse_template_parts(template: str) -> tuple[str | _ExpressionNode, ...]:
        """Parse `${...}` fragments in a JavaScript template literal."""
        parts: list[str | _ExpressionNode] = []
        text: list[str] = []
        position = 0
        while position < len(template):
            if template.startswith("\\${", position):
                text.append("${")
                position += 3
                continue
            if template.startswith("${", position):
                if text:
                    parts.append("".join(text))
                    text.clear()
                start = position + 2
                closing = _ExpressionParser._find_template_expression_end(template, start)
                parts.append(_ExpressionParser(template[start:closing]).parse())
                position = closing + 1
                continue
            if template[position] == "\\" and position + 1 < len(template):
                escape = template[position + 1]
                text.append(
                    {"n": "\n", "r": "\r", "t": "\t", "`": "`", "\\": "\\"}.get(escape, escape)
                )
                position += 2
                continue
            text.append(template[position])
            position += 1
        if text:
            parts.append("".join(text))
        return tuple(parts)

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
class _Closure:
    parameters: tuple[_BindingPattern, ...]
    body: _ExpressionNode
    local_values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RegexValue:
    pattern: str
    flags: str = ""


@dataclass(frozen=True, slots=True)
class _SetValue:
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Duration:
    seconds: float


@dataclass(frozen=True, slots=True)
class _InvalidDateTime:
    reason: str


class _UndefinedType:
    """JavaScript `undefined`, distinct from JSON null while evaluating expressions."""


_UNDEFINED = _UndefinedType()


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
            return self._public_value(
                self._evaluate_source(value[3:-2].strip(), item, item_index, context)
            )
        if value.startswith("{{") and value.endswith("}}"):
            return self._public_value(
                self._evaluate_source(value[2:-2].strip(), item, item_index, context)
            )
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
        self,
        node: _ExpressionNode,
        item: WorkflowItem,
        item_index: int,
        context: Any,
        local_values: dict[str, Any] | None = None,
    ) -> Any:
        """Вычислить AST через разрешённые операции, не запуская произвольный Python."""
        if isinstance(node, _Literal):
            return node.value
        if isinstance(node, _Template):
            return "".join(
                part
                if isinstance(part, str)
                else self._js_string(
                    self._evaluate_node(part, item, item_index, context, local_values)
                )
                for part in node.parts
            )
        if isinstance(node, _RegexLiteral):
            return _RegexValue(node.pattern, node.flags)
        if isinstance(node, _Spread):
            return self._evaluate_node(node.value, item, item_index, context, local_values)
        if isinstance(node, _Arrow):
            return _Function(
                "Arrow", _Closure(node.parameters, node.body, dict(local_values or {}))
            )
        if isinstance(node, _Block):
            scope = dict(local_values or {})
            last_value: Any = _UNDEFINED
            for name, expression in node.statements:
                last_value = self._evaluate_node(expression, item, item_index, context, scope)
                if name == "$return":
                    return last_value
                if name is not None:
                    scope[name] = last_value
            return last_value
        if isinstance(node, _Name):
            if local_values is not None and node.value in local_values:
                return local_values[node.value]
            services = self._context_mapping(context, "services")
            expression_context = services.get("expression_context", {})
            current_node = services.get("current_node", {})
            previous_node = services.get("previous_node", {})
            workflow = expression_context.get("workflow", services.get("workflow", {"name": ""}))
            execution = expression_context.get(
                "execution", services.get("execution", {"id": "", "mode": "manual"})
            )
            invocation_number = getattr(context, "invocation_number", 1) or 1
            now = _current_datetime(context)
            roots = {
                "$json": item.json_data,
                "$binary": item.binary or {},
                "$itemIndex": item_index,
                "$now": now,
                "$today": now.replace(hour=0, minute=0, second=0, microsecond=0),
                "$execution": expression_context.get("execution", execution),
                "$workflow": workflow,
                "$vars": expression_context.get("vars", services.get("vars", {})),
                "$env": expression_context.get("env", services.get("env", {})),
                "$runIndex": invocation_number - 1,
                "$prevNode": previous_node,
                "$nodeVersion": current_node.get("type_version", _UNDEFINED),
                "$pageCount": expression_context.get(
                    "page_count", services.get("page_count", _UNDEFINED)
                ),
                "$response": expression_context.get(
                    "response", services.get("response", _UNDEFINED)
                ),
                "$request": expression_context.get("request", services.get("request", _UNDEFINED)),
                "$input": _Namespace("$input"),
                "$": _Namespace("$"),
                "JSON": _Namespace("JSON"),
                "Object": _Namespace("Object"),
                "Array": _Namespace("Array"),
                "Math": _Namespace("Math"),
                "Date": _Namespace("Date"),
                "DateTime": _Namespace("DateTime"),
                "Set": _Function("Set"),
                "String": _Function("String"),
                "Number": _Function("Number"),
                "Boolean": _Function("Boolean"),
                "parseInt": _Function("parseInt"),
                "parseFloat": _Function("parseFloat"),
                "encodeURIComponent": _Function("encodeURIComponent"),
                "decodeURIComponent": _Function("decodeURIComponent"),
                "$if": _Function("n8n.if"),
                "$ifEmpty": _Function("n8n.ifEmpty"),
                "$fromAI": _Function("n8n.fromAI"),
                "$jmespath": _Function("n8n.jmespath"),
            }
            if node.value not in roots:
                raise UnsupportedNodeConfigurationError(
                    f"Переменная или функция {node.value!r} не входит в поддержанный expression subset."
                )
            return roots[node.value]
        if isinstance(node, _Member):
            parent = self._evaluate_node(node.parent, item, item_index, context, local_values)
            if node.optional and (parent is None or parent is _UNDEFINED):
                return _UNDEFINED
            return self._resolve_member(parent, node.name, item, context)
        if isinstance(node, _Index):
            parent = self._evaluate_node(node.parent, item, item_index, context, local_values)
            if node.optional and (parent is None or parent is _UNDEFINED):
                return _UNDEFINED
            index = self._evaluate_node(node.index, item, item_index, context, local_values)
            if index is _UNDEFINED or index is None:
                return _UNDEFINED
            if isinstance(parent, dict) and isinstance(index, (str, int)):
                return parent.get(index, _UNDEFINED)
            if isinstance(parent, (list, str)) and isinstance(index, int):
                try:
                    return parent[index]
                except IndexError:
                    return _UNDEFINED
            raise UnsupportedNodeConfigurationError(
                "Индекс выражения должен применяться к объекту или массиву."
            )
        if isinstance(node, _Array):
            values: list[Any] = []
            for child in node.values:
                value = self._evaluate_node(child, item, item_index, context, local_values)
                values.extend(self._spread_values(value) if isinstance(child, _Spread) else [value])
            return values
        if isinstance(node, _Object):
            result: dict[str, Any] = {}
            for key, expression in node.fields:
                value = self._evaluate_node(expression, item, item_index, context, local_values)
                if key is None:
                    if not isinstance(value, dict):
                        raise UnsupportedNodeConfigurationError(
                            "Spread объекта требует JSON-объект."
                        )
                    result.update(value)
                else:
                    key_value = (
                        self._evaluate_node(key, item, item_index, context, local_values)
                        if isinstance(key, _ExpressionNode)
                        else key
                    )
                    result[self._js_string(key_value)] = value
            return result
        if isinstance(node, _Unary):
            operand = self._evaluate_node(node.operand, item, item_index, context, local_values)
            if node.operator == "!":
                return not self._js_truthy(operand)
            if node.operator == "typeof":
                return self._js_typeof(operand)
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                raise UnsupportedNodeConfigurationError(
                    f"Унарный оператор {node.operator!r} требует число."
                )
            return operand if node.operator == "+" else -operand
        if isinstance(node, _Binary):
            return self._evaluate_binary(node, item, item_index, context, local_values)
        if isinstance(node, _Conditional):
            condition = self._evaluate_node(node.condition, item, item_index, context, local_values)
            branch = node.when_true if self._js_truthy(condition) else node.when_false
            return self._evaluate_node(branch, item, item_index, context, local_values)
        if isinstance(node, _Call):
            function = self._evaluate_node(node.function, item, item_index, context, local_values)
            if node.optional and (function is None or function is _UNDEFINED):
                return _UNDEFINED
            arguments = [
                self._evaluate_node(argument, item, item_index, context, local_values)
                for argument in node.arguments
            ]
            return self._call(function, arguments, item, context, local_values=local_values)
        raise UnsupportedNodeConfigurationError(
            "Не удалось определить тип выражения для вычисления."
        )

    def _evaluate_binary(
        self,
        node: _Binary,
        item: WorkflowItem,
        item_index: int,
        context: Any,
        local_values: dict[str, Any] | None = None,
    ) -> Any:
        """Вычислить оператор с JavaScript-подобным short circuit и coalescing."""
        left = self._evaluate_node(node.left, item, item_index, context, local_values)
        if node.operator == "&&":
            return (
                self._evaluate_node(node.right, item, item_index, context, local_values)
                if self._js_truthy(left)
                else left
            )
        if node.operator == "||":
            return (
                left
                if self._js_truthy(left)
                else self._evaluate_node(node.right, item, item_index, context, local_values)
            )
        if node.operator == "??":
            return (
                left
                if left is not None and left is not _UNDEFINED
                else self._evaluate_node(node.right, item, item_index, context, local_values)
            )
        right = self._evaluate_node(node.right, item, item_index, context, local_values)
        if node.operator == "+":
            if isinstance(left, (str, list, dict)) or isinstance(right, (str, list, dict)):
                return self._js_string(left) + self._js_string(right)
            return self._number(left) + self._number(right)
        if node.operator in {"-", "*", "/", "%", "**"}:
            lhs, rhs = self._number(left), self._number(right)
            if node.operator == "-":
                return lhs - rhs
            if node.operator == "*":
                return lhs * rhs
            if node.operator == "/":
                return lhs / rhs
            if node.operator == "**":
                return lhs**rhs
            return lhs % rhs
        if node.operator in {"==", "===", "!=", "!=="}:
            if node.operator in {"==", "!="}:
                equal = self._js_loose_equal(left, right)
            else:
                equal = type(left) is type(right) and left == right
            return not equal if node.operator in {"!=", "!=="} else equal
        if node.operator in {">", ">=", "<", "<="}:
            if isinstance(left, str) and isinstance(right, str):
                lhs, rhs = left, right
            else:
                lhs, rhs = self._number(left), self._number(right)
            return {
                ">": lhs > rhs,
                ">=": lhs >= rhs,
                "<": lhs < rhs,
                "<=": lhs <= rhs,
            }[node.operator]
        raise UnsupportedNodeConfigurationError(f"Оператор {node.operator!r} не поддерживается.")

    def _resolve_member(self, parent: Any, name: str, item: WorkflowItem, context: Any) -> Any:
        """Разрешить поле или функцию из явно ограниченного набора n8n/JavaScript."""
        if isinstance(parent, _InvalidDateTime):
            if name == "isValid":
                return False
            if name == "invalidReason":
                return parent.reason
            if name in {"toISO", "toISODate", "toFormat", "toMillis"}:
                return _Function(f"InvalidDateTime.{name}", parent)
            return _UNDEFINED
        if parent is _UNDEFINED or parent is None:
            return _UNDEFINED
        if isinstance(parent, Mapping):
            if name in parent:
                return parent[name]
            if name in {
                "compact",
                "removeField",
                "hasField",
                "toJsonString",
                "isEmpty",
                "isNotEmpty",
            }:
                return _Function(f"Object.{name}", dict(parent))
            return _UNDEFINED
        if isinstance(parent, WorkflowItem):
            if name == "json":
                return parent.json_data
            if name == "binary":
                return parent.binary or {}
            return _UNDEFINED
        if isinstance(parent, _NodeReference):
            if name == "isExecuted":
                history = getattr(context, "node_outputs", {}) if context is not None else {}
                return parent.name in history
            if name == "item":
                history = self._node_history(context, parent.name)
                try:
                    index = item.lineage[parent.name]
                    return history[index]
                except (KeyError, IndexError) as error:
                    raise UnsupportedNodeConfigurationError(
                        f"Для expression $('{parent.name}').item нет однозначной связи с текущим item."
                    ) from error
            if name in {"first", "last", "all", "itemMatching"}:
                return _Function(f"Node.{name}", parent.name)
            return _UNDEFINED
        if isinstance(parent, _Namespace):
            allowed = {
                "JSON": {
                    "stringify": _Function("JSON.stringify"),
                    "parse": _Function("JSON.parse"),
                },
                "Object": {
                    key: _Function(f"Object.{key}")
                    for key in ("keys", "values", "entries", "fromEntries")
                },
                "Array": {"isArray": _Function("Array.isArray")},
                "Math": {
                    key: _Function(f"Math.{key}")
                    for key in (
                        "abs",
                        "ceil",
                        "floor",
                        "max",
                        "min",
                        "pow",
                        "random",
                        "round",
                        "sqrt",
                        "trunc",
                        "sign",
                    )
                },
                "Date": {"now": _Function("Date.now")},
                "DateTime": {
                    key: _Function(f"DateTime.{key}")
                    for key in ("fromISO", "fromFormat", "fromMillis", "fromSeconds", "fromSQL")
                },
                "Object.prototype": {"hasOwnProperty": _Function("Object.hasOwnProperty")},
            }
            if parent.name == "$":
                return _Function("Node.reference")
            if parent.name == "$input":
                if name == "item":
                    return item
                if name in {"first", "last", "all"}:
                    return _Function(f"Input.{name}")
            if parent.name == "Object" and name == "prototype":
                return _Namespace("Object.prototype")
            try:
                return allowed[parent.name][name]
            except KeyError:
                return _UNDEFINED
        if isinstance(parent, _RegexValue) and name in {"test", "exec"}:
            return _Function(f"RegExp.{name}", parent)
        if (
            isinstance(parent, _Function)
            and parent.name == "Object.hasOwnProperty"
            and name == "call"
        ):
            return _Function("Object.hasOwnProperty.call")
        if isinstance(parent, _Function) and parent.name in {"Number", "String", "Boolean"}:
            namespace_methods = {
                "Number": {"isFinite", "isInteger", "isNaN"},
                "String": {"raw"},
                "Boolean": set(),
            }
            if name in namespace_methods.get(parent.name, set()):
                return _Function(f"{parent.name}.{name}")
        if isinstance(parent, _SetValue):
            if name in {"size", "length"}:
                return len(parent.values)
            if name in {"has", "values", "keys", "entries"}:
                return _Function(f"Set.{name}", parent)
        if isinstance(parent, list):
            if name in {"first", "last", "all"}:
                return _Function(f"Array.{name}", parent)
            if name in {
                "at",
                "concat",
                "filter",
                "find",
                "findIndex",
                "flat",
                "flatMap",
                "includes",
                "indexOf",
                "join",
                "map",
                "reduce",
                "slice",
                "some",
                "every",
                "sort",
                "reverse",
                "push",
                "pop",
                "shift",
                "unshift",
                "sum",
                "average",
                "unique",
                "compact",
                "difference",
                "intersection",
                "union",
                "chunk",
                "itemMatching",
                "isEmpty",
                "isNotEmpty",
                "append",
                "toJsonString",
            }:
                return _Function(f"Array.{name}", parent)
        if isinstance(parent, (list, str)) and name == "length":
            return len(parent)
        if isinstance(parent, str):
            if name in {
                "at",
                "charAt",
                "charCodeAt",
                "concat",
                "endsWith",
                "includes",
                "indexOf",
                "match",
                "padEnd",
                "padStart",
                "repeat",
                "replace",
                "replaceAll",
                "slice",
                "split",
                "startsWith",
                "substring",
                "toLowerCase",
                "toUpperCase",
                "trim",
                "trimEnd",
                "trimStart",
                "localeCompare",
                "parseJson",
                "toNumber",
                "toDateTime",
                "toJsonString",
                "extractDomain",
                "extractEmail",
                "extractUrl",
                "extractUrlPath",
                "isEmpty",
                "isNotEmpty",
                "isEmail",
                "isNumeric",
                "isUrl",
                "isDomain",
                "base64Encode",
                "base64Decode",
                "urlEncode",
                "urlDecode",
                "format",
            }:
                return _Function(f"String.{name}", parent)
        if isinstance(parent, (int, float)) and not isinstance(parent, bool):
            if name in {"toFixed", "toString", "isEven", "isOdd", "toNumber", "format"}:
                return _Function(f"Number.{name}", parent)
        if isinstance(parent, datetime):
            if name == "isValid":
                return True
            if name in {
                "toFormat",
                "toISO",
                "toISODate",
                "toMillis",
                "toSeconds",
                "toUTC",
                "setZone",
                "minus",
                "plus",
                "startOf",
                "endOf",
                "diff",
                "toSQL",
                "year",
                "month",
                "day",
                "hour",
                "minute",
                "second",
                "weekday",
                "weekNumber",
                "monthLong",
                "monthShort",
            }:
                if name in {"year", "month", "day", "hour", "minute", "second"}:
                    return getattr(parent, name)
                if name == "weekday":
                    return parent.isoweekday()
                if name == "weekNumber":
                    return parent.isocalendar().week
                if name == "monthLong":
                    return parent.strftime("%B")
                if name == "monthShort":
                    return parent.strftime("%b")
                return _Function(f"DateTime.{name}", parent)
        if isinstance(parent, timedelta) and name in {
            "milliseconds",
            "seconds",
            "minutes",
            "hours",
            "days",
            "as",
        }:
            if name == "milliseconds":
                return parent.total_seconds() * 1000
            if name == "seconds":
                return parent.total_seconds()
            if name == "minutes":
                return parent.total_seconds() / 60
            if name == "hours":
                return parent.total_seconds() / 3600
            if name == "days":
                return parent.total_seconds() / 86400
            return _Function(f"Duration.{name}", parent)
        if isinstance(parent, _Duration):
            if name == "milliseconds":
                return parent.seconds * 1000
            if name == "seconds":
                return parent.seconds
            if name == "minutes":
                return parent.seconds / 60
            if name == "hours":
                return parent.seconds / 3600
            if name == "days":
                return parent.seconds / 86400
            if name == "weeks":
                return parent.seconds / 604800
            if name == "as":
                return _Function("Duration.as", parent)
        return _UNDEFINED

    def _call(
        self,
        function: Any,
        arguments: list[Any],
        item: WorkflowItem,
        context: Any,
        *,
        local_values: dict[str, Any] | None = None,
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
            input_values = getattr(context, "input_items", None) if context is not None else None
            input_items = tuple(input_values) if input_values is not None else (item,)
            return self._select_item(function.name.removeprefix("Input."), input_items, arguments)
        if function.name.startswith("Node."):
            node_name = str(function.receiver)
            selector = function.name.removeprefix("Node.")
            runs = getattr(context, "node_output_runs", {}) if context is not None else {}
            if arguments and selector in {"first", "last", "all"} and node_name in runs:
                branch_index = int(self._number(arguments[0]))
                run_index = int(self._number(arguments[1])) if len(arguments) > 1 else 0
                try:
                    selected_branch = runs[node_name][run_index][branch_index]
                except (IndexError, TypeError):
                    selected_branch = None
                selected_items = tuple(selected_branch or ())
                return self._select_item(selector, selected_items, [], item=item)
            history = self._node_history(context, node_name)
            return self._select_item(selector, history, arguments, item=item)
        if function.name == "Arrow":
            return self._invoke_arrow(
                function.receiver,
                arguments,
                item,
                context,
                local_values=local_values,
            )
        if function.name == "String" and len(arguments) <= 1:
            return "" if not arguments else self._js_string(arguments[0])
        if function.name == "Number" and len(arguments) <= 1:
            return 0 if not arguments else self._number(arguments[0])
        if function.name == "Boolean" and len(arguments) <= 1:
            return False if not arguments else self._js_truthy(arguments[0])
        if function.name == "parseInt" and arguments:
            return self._parse_int(arguments[0], arguments[1] if len(arguments) > 1 else 10)
        if function.name == "parseFloat" and arguments:
            return self._parse_float(arguments[0])
        if function.name == "Set":
            source = self._spread_values(arguments[0]) if arguments else []
            return _SetValue(tuple(self._unique(source)))
        if function.name in {"encodeURIComponent", "decodeURIComponent"} and len(arguments) == 1:
            return (
                quote(self._js_string(arguments[0]), safe="~()*!.'-_")
                if function.name == "encodeURIComponent"
                else unquote(self._js_string(arguments[0]))
            )
        if function.name == "Date.now" and not arguments:
            return int(datetime.now().timestamp() * 1000)
        if function.name.startswith("Math."):
            return self._call_math(function.name.removeprefix("Math."), arguments)
        if function.name.startswith("DateTime."):
            operation = function.name.removeprefix("DateTime.")
            if operation in {
                "toFormat",
                "toISO",
                "toISODate",
                "toMillis",
                "toSeconds",
                "toUTC",
                "setZone",
                "minus",
                "plus",
                "startOf",
                "endOf",
                "diff",
                "toSQL",
            }:
                return self._call_datetime_method(operation, function.receiver, arguments)
            return self._create_datetime(operation, arguments, context)
        if function.name.startswith("InvalidDateTime."):
            operation = function.name.removeprefix("InvalidDateTime.")
            return "Invalid DateTime" if operation in {"toFormat", "toISODate"} else math.nan
        if function.name.startswith("Duration."):
            return self._call_duration(
                function.name.removeprefix("Duration."), function.receiver, arguments
            )
        if function.name == "Array.isArray" and len(arguments) == 1:
            return isinstance(arguments[0], list)
        if function.name == "Number.isFinite" and len(arguments) == 1:
            return (
                isinstance(arguments[0], (int, float))
                and not isinstance(arguments[0], bool)
                and math.isfinite(arguments[0])
            )
        if function.name == "Number.isInteger" and len(arguments) == 1:
            return (
                isinstance(arguments[0], (int, float))
                and not isinstance(arguments[0], bool)
                and float(arguments[0]).is_integer()
            )
        if function.name == "Number.isNaN" and len(arguments) == 1:
            return isinstance(arguments[0], float) and math.isnan(arguments[0])
        if function.name == "Object.hasOwnProperty.call" and len(arguments) >= 2:
            target, key = arguments[0], self._js_string(arguments[1])
            return isinstance(target, Mapping) and key in target
        if function.name.startswith("Object."):
            return self._call_object(
                function.name.removeprefix("Object."), arguments, function.receiver
            )
        if function.name.startswith("RegExp."):
            return self._call_regex(
                function.name.removeprefix("RegExp."), function.receiver, arguments
            )
        if function.name.startswith("String."):
            return self._call_string(
                function.name.removeprefix("String."), function.receiver, arguments
            )
        if function.name.startswith("Number."):
            return self._call_number(
                function.name.removeprefix("Number."), function.receiver, arguments
            )
        if function.name.startswith("Set."):
            return self._call_set(function.name.removeprefix("Set."), function.receiver, arguments)
        if function.name.startswith("Array."):
            return self._call_array(
                function.name.removeprefix("Array."),
                function.receiver,
                arguments,
                item,
                context,
                local_values,
            )
        if function.name == "n8n.if" and len(arguments) in {2, 3}:
            return (
                arguments[1]
                if self._js_truthy(arguments[0])
                else (arguments[2] if len(arguments) == 3 else _UNDEFINED)
            )
        if function.name == "n8n.ifEmpty" and len(arguments) == 2:
            return arguments[1] if self._is_empty(arguments[0]) else arguments[0]
        if function.name == "n8n.fromAI" and arguments:
            return self._from_ai(arguments, context)
        if function.name == "n8n.jmespath" and len(arguments) == 2:
            return self._jmespath(arguments[0], self._js_string(arguments[1]))
        if function.name == "DateTime.toFormat" and len(arguments) == 1:
            if not isinstance(arguments[0], str):
                raise UnsupportedNodeConfigurationError("$now.toFormat() ожидает строковый шаблон.")
            return _format_luxon_datetime(function.receiver, arguments[0])
        if function.name == "JSON.stringify" and arguments:
            indent = arguments[2] if len(arguments) > 2 else None
            if isinstance(indent, int):
                indent = min(max(indent, 0), 10)
            return json.dumps(
                self._public_value(arguments[0]),
                ensure_ascii=False,
                separators=(",", ":") if indent is None else None,
                indent=indent,
            )
        if function.name == "JSON.parse" and len(arguments) == 1 and isinstance(arguments[0], str):
            return json.loads(arguments[0])
        raise UnsupportedNodeConfigurationError(
            f"Функция {function.name!r} получила неподдерживаемые аргументы: {arguments!r}."
        )

    def _invoke_arrow(
        self,
        closure: _Closure,
        arguments: list[Any],
        item: WorkflowItem,
        context: Any,
        *,
        local_values: dict[str, Any] | None = None,
    ) -> Any:
        """Выполнить стрелочную функцию callback в локальной области её переменных."""
        scope = dict(closure.local_values)
        if local_values:
            scope.update(local_values)
        for index, pattern in enumerate(closure.parameters):
            value = arguments[index] if index < len(arguments) else _UNDEFINED
            self._bind_pattern(pattern, value, scope)
        return self._evaluate_node(closure.body, item, 0, context, scope)

    @classmethod
    def _bind_pattern(cls, pattern: _BindingPattern, value: Any, scope: dict[str, Any]) -> None:
        """Связать параметр callback с простым именем или destructuring pattern."""
        if pattern.name is not None:
            scope[pattern.name] = value
        elif pattern.array_items:
            values = value if isinstance(value, (list, tuple)) else []
            for index, child in enumerate(pattern.array_items):
                cls._bind_pattern(
                    child, values[index] if index < len(values) else _UNDEFINED, scope
                )
        else:
            source = value if isinstance(value, Mapping) else {}
            for key, child in pattern.object_items:
                cls._bind_pattern(child, source.get(key, _UNDEFINED), scope)

    def _call_array(
        self,
        operation: str,
        receiver: Any,
        arguments: list[Any],
        item: WorkflowItem,
        context: Any,
        local_values: dict[str, Any] | None,
    ) -> Any:
        """Выполнить распространённые Array и n8n array-helper методы."""
        values = list(receiver or ())
        callback = arguments[0] if arguments else None

        def call_callback(function: Any, *callback_args: Any) -> Any:
            if isinstance(function, _Function) and function.name in {"String", "Number", "Boolean"}:
                value = callback_args[0] if callback_args else _UNDEFINED
                if function.name == "String":
                    return self._js_string(value)
                if function.name == "Number":
                    return self._number(value)
                return self._js_truthy(value)
            if not isinstance(function, _Function) or function.name != "Arrow":
                raise UnsupportedNodeConfigurationError(
                    f"Array.{operation} ожидает callback вида (item, index) => expression."
                )
            return self._invoke_arrow(
                function.receiver,
                list(callback_args),
                item,
                context,
                local_values=local_values,
            )

        if operation in {"first", "last"}:
            if arguments:
                raise UnsupportedNodeConfigurationError(
                    f"Array.{operation} не принимает аргументы."
                )
            if not values:
                raise UnsupportedNodeConfigurationError(
                    f"Array.{operation} не может выбрать элемент из пустого массива."
                )
            return values[0] if operation == "first" else values[-1]
        if operation == "all":
            return values
        if operation == "isEmpty":
            return not values
        if operation == "isNotEmpty":
            return bool(values)
        if operation == "toJsonString":
            return json.dumps(self._public_value(values), ensure_ascii=False, separators=(",", ":"))
        if operation == "append":
            return [*values, *arguments]
        if operation == "join":
            separator = self._js_string(arguments[0]) if arguments else ","
            return separator.join(self._js_string(value) for value in values)
        if operation == "includes":
            return bool(arguments) and any(
                self._js_loose_equal(value, arguments[0]) for value in values
            )
        if operation == "indexOf":
            return next(
                (
                    i
                    for i, value in enumerate(values)
                    if arguments and self._js_loose_equal(value, arguments[0])
                ),
                -1,
            )
        if operation == "at":
            index = int(self._number(arguments[0])) if arguments else 0
            index = index if index >= 0 else len(values) + index
            return values[index] if 0 <= index < len(values) else _UNDEFINED
        if operation == "slice":
            start = (
                int(self._number(arguments[0]))
                if arguments and arguments[0] is not _UNDEFINED
                else 0
            )
            end = (
                int(self._number(arguments[1]))
                if len(arguments) > 1 and arguments[1] is not _UNDEFINED
                else len(values)
            )
            return values[start:end]
        if operation == "concat":
            result = list(values)
            for value in arguments:
                result.extend(value if isinstance(value, list) else [value])
            return result
        if operation == "map":
            return [
                call_callback(callback, value, index, values) for index, value in enumerate(values)
            ]
        if operation == "filter":
            return [
                value
                for index, value in enumerate(values)
                if self._js_truthy(call_callback(callback, value, index, values))
            ]
        if operation == "find":
            return next(
                (
                    value
                    for index, value in enumerate(values)
                    if self._js_truthy(call_callback(callback, value, index, values))
                ),
                _UNDEFINED,
            )
        if operation == "findIndex":
            return next(
                (
                    index
                    for index, value in enumerate(values)
                    if self._js_truthy(call_callback(callback, value, index, values))
                ),
                -1,
            )
        if operation in {"some", "every"}:
            outcomes = (
                self._js_truthy(call_callback(callback, value, index, values))
                for index, value in enumerate(values)
            )
            return any(outcomes) if operation == "some" else all(outcomes)
        if operation == "reduce":
            if len(arguments) > 1:
                accumulator, offset = arguments[1], 0
            elif values:
                accumulator, offset = values[0], 1
            else:
                raise UnsupportedNodeConfigurationError(
                    "Array.reduce: пустой массив требует начальное значение."
                )
            for index in range(offset, len(values)):
                accumulator = call_callback(callback, accumulator, values[index], index, values)
            return accumulator
        if operation == "sort":
            result = list(values)
            if callback is None:
                return sorted(result, key=self._js_string)

            def compare(left: Any, right: Any) -> int:
                difference = self._number(call_callback(callback, left, right))
                return -1 if difference < 0 else 1 if difference > 0 else 0

            return sorted(result, key=cmp_to_key(compare))
        if operation == "reverse":
            return list(reversed(values))
        if operation == "flat":
            depth = int(self._number(arguments[0])) if arguments else 1
            return self._flatten(values, depth)
        if operation == "flatMap":
            mapped = [
                call_callback(callback, value, index, values) for index, value in enumerate(values)
            ]
            return self._flatten(mapped, 1)
        if operation in {"push", "unshift"}:
            result = list(values)
            if operation == "push":
                result.extend(arguments)
            else:
                result[0:0] = arguments
            return len(result)
        if operation in {"pop", "shift"}:
            if not values:
                return _UNDEFINED
            return values[-1] if operation == "pop" else values[0]
        if operation in {"sum", "average"}:
            numbers = [self._number(value) for value in values]
            if not numbers:
                return 0 if operation == "sum" else _UNDEFINED
            return sum(numbers) if operation == "sum" else statistics.mean(numbers)
        if operation in {"unique", "union", "intersection", "difference"}:
            other = arguments[0] if arguments and isinstance(arguments[0], list) else []
            if operation == "unique":
                return self._unique(values)
            if operation == "union":
                return self._unique([*values, *other])
            other_keys = {self._stable_key(value) for value in other}
            if operation == "intersection":
                return self._unique(
                    [value for value in values if self._stable_key(value) in other_keys]
                )
            return [value for value in values if self._stable_key(value) not in other_keys]
        if operation == "compact":
            return [value for value in values if not self._is_empty(value)]
        if operation == "chunk":
            size = int(self._number(arguments[0])) if arguments else 0
            if size <= 0:
                raise UnsupportedNodeConfigurationError("Array.chunk требует размер больше нуля.")
            return [values[index : index + size] for index in range(0, len(values), size)]
        if operation == "itemMatching":
            if not arguments:
                raise UnsupportedNodeConfigurationError("Array.itemMatching требует индекс item.")
            index = int(self._number(arguments[0]))
            return values[index] if 0 <= index < len(values) else _UNDEFINED
        raise UnsupportedNodeConfigurationError(f"Метод массива {operation!r} не реализован.")

    @staticmethod
    def _flatten(values: list[Any], depth: int) -> list[Any]:
        """Развернуть вложенные массивы на заданную глубину."""
        flattened: list[Any] = []
        for value in values:
            if depth > 0 and isinstance(value, list):
                flattened.extend(N8nExpressionEvaluator._flatten(value, depth - 1))
            else:
                flattened.append(value)
        return flattened

    @staticmethod
    def _stable_key(value: Any) -> str:
        """Получить стабильный ключ сравнения для JSON-значений и массивов."""
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(value)

    @classmethod
    def _unique(cls, values: Sequence[Any]) -> list[Any]:
        """Удалить дубликаты, сохраняя первый исходный элемент и порядок."""
        result: list[Any] = []
        seen: set[str] = set()
        for value in values:
            key = cls._stable_key(value)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    def _call_object(self, operation: str, arguments: list[Any], receiver: Any) -> Any:
        """Выполнить статические Object helpers и расширения n8n для объектов."""
        if operation == "keys" and arguments and isinstance(arguments[0], Mapping):
            return list(arguments[0])
        if operation == "values" and arguments and isinstance(arguments[0], Mapping):
            return list(arguments[0].values())
        if operation == "entries" and arguments and isinstance(arguments[0], Mapping):
            return [[key, value] for key, value in arguments[0].items()]
        if operation == "fromEntries" and arguments and isinstance(arguments[0], list):
            return {
                str(pair[0]): pair[1]
                for pair in arguments[0]
                if isinstance(pair, (list, tuple)) and len(pair) >= 2
            }
        if operation == "compact" and isinstance(receiver, Mapping):
            return {key: value for key, value in receiver.items() if not self._is_empty(value)}
        if operation == "removeField" and isinstance(receiver, Mapping) and arguments:
            keys = arguments[0] if isinstance(arguments[0], list) else arguments
            return {key: value for key, value in receiver.items() if key not in keys}
        if operation == "hasField" and isinstance(receiver, Mapping) and arguments:
            return self._js_string(arguments[0]) in receiver
        if operation == "toJsonString" and isinstance(receiver, Mapping):
            return json.dumps(
                self._public_value(receiver), ensure_ascii=False, separators=(",", ":")
            )
        if operation == "isEmpty" and isinstance(receiver, Mapping):
            return not receiver
        if operation == "isNotEmpty" and isinstance(receiver, Mapping):
            return bool(receiver)
        raise UnsupportedNodeConfigurationError(
            f"Object.{operation} получил неподдерживаемые аргументы."
        )

    def _call_regex(self, operation: str, receiver: _RegexValue, arguments: list[Any]) -> Any:
        """Вычислить RegExp.test/exec для явно записанного безопасного шаблона."""
        if not arguments:
            raise UnsupportedNodeConfigurationError(f"RegExp.{operation} требует строку.")
        match = self._compile_regex(receiver).search(self._js_string(arguments[0]))
        if operation == "test":
            return match is not None
        if operation == "exec":
            return [match.group(0), *match.groups()] if match else None
        raise UnsupportedNodeConfigurationError(f"Метод RegExp.{operation} не реализован.")

    @staticmethod
    def _compile_regex(expression: _RegexValue) -> re.Pattern[str]:
        """Преобразовать часто используемые Unicode-свойства JS regex в Python regex."""
        pattern = expression.pattern.replace(r"[\p{L}\p{N}_]", r"[\w]")
        pattern = pattern.replace(r"[\p{L}\p{N}]", r"[\w]")
        pattern = pattern.replace(r"\p{L}", r"[^\W\d_]").replace(r"\p{N}", r"\d")
        pattern = pattern.replace(r"\p{M}", r"\u0300-\u036f")
        flags = 0
        if "i" in expression.flags:
            flags |= re.IGNORECASE
        if "m" in expression.flags:
            flags |= re.MULTILINE
        if "s" in expression.flags:
            flags |= re.DOTALL
        try:
            return re.compile(pattern, flags)
        except re.error as error:
            raise UnsupportedNodeConfigurationError(
                f"Некорректное регулярное выражение /{expression.pattern}/{expression.flags}: {error}."
            ) from error

    def _call_string(self, operation: str, receiver: str, arguments: list[Any]) -> Any:
        """Выполнить стандартные JS string methods и часто используемые n8n helpers."""
        value = receiver
        first = arguments[0] if arguments else _UNDEFINED
        if operation == "toUpperCase":
            return value.upper()
        if operation == "toLowerCase":
            return value.lower()
        if operation == "trim":
            return value.strip()
        if operation == "trimStart":
            return value.lstrip()
        if operation == "trimEnd":
            return value.rstrip()
        if operation in {"startsWith", "endsWith", "includes"}:
            search = self._js_string(first)
            position = int(self._number(arguments[1])) if len(arguments) > 1 else 0
            if operation == "startsWith":
                return value.startswith(search, position)
            if operation == "endsWith":
                return value.endswith(search, 0, position if len(arguments) > 1 else len(value))
            return search in value
        if operation == "indexOf":
            return value.find(
                self._js_string(first), int(self._number(arguments[1])) if len(arguments) > 1 else 0
            )
        if operation == "at":
            index = int(self._number(first))
            index = index if index >= 0 else len(value) + index
            return value[index] if 0 <= index < len(value) else _UNDEFINED
        if operation in {"slice", "substring"}:
            start = int(self._number(first)) if first is not _UNDEFINED else 0
            end = int(self._number(arguments[1])) if len(arguments) > 1 else len(value)
            if operation == "substring":
                start, end = max(0, start), max(0, end)
                start, end = min(start, end), max(start, end)
            return value[start:end]
        if operation == "charAt":
            index = int(self._number(first)) if first is not _UNDEFINED else 0
            return value[index] if 0 <= index < len(value) else ""
        if operation == "charCodeAt":
            index = int(self._number(first)) if first is not _UNDEFINED else 0
            return ord(value[index]) if 0 <= index < len(value) else math.nan
        if operation == "concat":
            return value + "".join(self._js_string(arg) for arg in arguments)
        if operation == "split":
            if first is _UNDEFINED:
                return [value]
            if isinstance(first, _RegexValue):
                return self._compile_regex(first).split(value)
            return value.split(
                self._js_string(first),
                int(self._number(arguments[1])) if len(arguments) > 1 else -1,
            )
        if operation in {"match", "replace", "replaceAll"}:
            return self._call_string_regex(operation, value, arguments)
        if operation in {"padStart", "padEnd"}:
            width = max(0, int(self._number(first)))
            fill = self._js_string(arguments[1]) if len(arguments) > 1 else " "
            if not fill:
                return value
            needed = max(0, width - len(value))
            padding = (fill * ((needed // len(fill)) + 1))[:needed]
            return padding + value if operation == "padStart" else value + padding
        if operation == "repeat":
            return value * max(0, int(self._number(first)))
        if operation == "localeCompare":
            other = self._js_string(first)
            return (value > other) - (value < other)
        if operation == "parseJson":
            return json.loads(value)
        if operation in {"toNumber", "toJsonString"}:
            return (
                self._number(value)
                if operation == "toNumber"
                else json.dumps(value, ensure_ascii=False)
            )
        if operation == "toDateTime":
            parsed = self._create_datetime("fromISO", [value], None)
            return parsed
        if operation == "format":
            return self._call_number("format", self._number(value), arguments)
        if operation == "extractDomain":
            match = re.search(r"(?:https?://)?(?:[^/@]+@)?([^/:?#]+)", value, re.IGNORECASE)
            return match.group(1).lower() if match else _UNDEFINED
        if operation == "extractEmail":
            match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, re.IGNORECASE)
            return match.group(0) if match else _UNDEFINED
        if operation == "extractUrl":
            match = re.search(r"https?://[^\s<>\"']+", value, re.IGNORECASE)
            return match.group(0) if match else _UNDEFINED
        if operation == "extractUrlPath":
            url = self._call_string("extractUrl", value, [])
            return urlsplit(url).path if url is not _UNDEFINED else _UNDEFINED
        if operation == "isEmpty":
            return value.strip() == ""
        if operation == "isNotEmpty":
            return value.strip() != ""
        if operation == "isEmail":
            return re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
        if operation == "isNumeric":
            try:
                number = self._number(value)
                return math.isfinite(number)
            except (ValueError, TypeError):
                return False
        if operation == "isUrl":
            return urlsplit(value).scheme in {"http", "https"} and bool(urlsplit(value).netloc)
        if operation == "isDomain":
            return re.fullmatch(r"(?:[A-Z0-9-]+\.)+[A-Z]{2,}", value, re.IGNORECASE) is not None
        if operation == "base64Encode":
            return base64.b64encode(value.encode()).decode()
        if operation == "base64Decode":
            try:
                return base64.b64decode(value, validate=True).decode()
            except (ValueError, UnicodeDecodeError):
                return base64.b64decode(value + "=" * (-len(value) % 4)).decode(errors="replace")
        if operation == "urlEncode":
            return quote(value, safe="")
        if operation == "urlDecode":
            return unquote(value)
        raise UnsupportedNodeConfigurationError(f"Строковый метод {operation!r} не реализован.")

    def _call_string_regex(self, operation: str, value: str, arguments: list[Any]) -> Any:
        """Обработать match/replace с JavaScript-подобным флагом g и группами $1."""
        pattern = arguments[0] if arguments else _UNDEFINED
        if isinstance(pattern, _RegexValue):
            compiled = self._compile_regex(pattern)
            global_replace = "g" in pattern.flags
        else:
            compiled = re.compile(re.escape(self._js_string(pattern)))
            global_replace = operation == "replaceAll"
        if operation == "match":
            if "g" in getattr(pattern, "flags", ""):
                matches = [match.group(0) for match in compiled.finditer(value)]
                return matches or None
            match = compiled.search(value)
            return [match.group(0), *match.groups()] if match else None
        replacement = self._js_string(arguments[1]) if len(arguments) > 1 else "undefined"

        def replace_match(match: re.Match[str]) -> str:
            result = replacement.replace("$&", match.group(0)).replace("$$", "$")
            for index, group in enumerate(match.groups(), 1):
                result = result.replace(f"${index}", group or "")
            return result

        count = 0 if global_replace else 1
        return compiled.sub(replace_match, value, count=count)

    def _call_number(self, operation: str, receiver: int | float, arguments: list[Any]) -> Any:
        """Выполнить методы Number и форматирование денежного значения."""
        if operation == "toFixed":
            precision = min(100, max(0, int(self._number(arguments[0])) if arguments else 0))
            return f"{receiver:.{precision}f}"
        if operation == "toString":
            radix = int(self._number(arguments[0])) if arguments else 10
            if radix == 10:
                return self._js_string(receiver)
            if radix == 16 and float(receiver).is_integer():
                return format(int(receiver), "x")
            raise UnsupportedNodeConfigurationError(
                "Number.toString поддерживает только radix 10 и 16."
            )
        if operation == "isEven":
            return int(receiver) % 2 == 0
        if operation == "isOdd":
            return int(receiver) % 2 != 0
        if operation == "toNumber":
            return receiver
        if operation == "format":
            locale = self._js_string(arguments[0]) if arguments else "en-US"
            options = (
                arguments[1] if len(arguments) > 1 and isinstance(arguments[1], Mapping) else {}
            )
            currency = options.get("currency")
            minimum = options.get("minimumFractionDigits")
            maximum = options.get("maximumFractionDigits")
            precision = int(
                maximum
                if maximum is not None
                else 2
                if currency
                else 0
                if float(receiver).is_integer()
                else 2
            )
            precision = min(max(precision, 0), 20)
            formatted = f"{receiver:,.{precision}f}"
            if locale.lower().startswith(("ru", "be")):
                formatted = (
                    formatted.replace(",", "\u0000").replace(".", ",").replace("\u0000", "\u00a0")
                )
            if currency:
                symbol = {"BYN": "BYN", "USD": "$", "EUR": "€", "RUB": "₽"}.get(
                    str(currency), str(currency)
                )
                formatted = f"{formatted} {symbol}" if symbol.isalpha() else f"{symbol}{formatted}"
            if minimum is not None and int(minimum) > precision:
                formatted = f"{self._number(receiver):,.{int(minimum)}f}"
            return formatted
        raise UnsupportedNodeConfigurationError(f"Числовой метод {operation!r} не реализован.")

    def _call_math(self, operation: str, arguments: list[Any]) -> Any:
        """Выполнить безопасный список функций Math."""
        values = [self._number(value) for value in arguments]
        if operation == "random":
            return random.random()
        if any(math.isnan(value) for value in values):
            return math.nan
        if operation == "max":
            return max(values, default=-math.inf)
        if operation == "min":
            return min(values, default=math.inf)
        if not values:
            raise UnsupportedNodeConfigurationError(f"Math.{operation} требует числовой аргумент.")
        functions = {
            "abs": math.fabs,
            "ceil": math.ceil,
            "floor": math.floor,
            "round": lambda value: math.floor(value + 0.5),
            "pow": math.pow,
            "sqrt": math.sqrt,
            "trunc": math.trunc,
            "sign": lambda value: 0 if value == 0 else math.copysign(1, value),
        }
        try:
            return functions[operation](*values)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise UnsupportedNodeConfigurationError(
                f"Math.{operation} не удалось вычислить."
            ) from error

    def _call_set(self, operation: str, receiver: _SetValue, arguments: list[Any]) -> Any:
        """Выполнить базовые операции JavaScript Set."""
        if operation == "has" and arguments:
            key = self._stable_key(arguments[0])
            return any(self._stable_key(value) == key for value in receiver.values)
        if operation in {"values", "keys"}:
            return list(receiver.values)
        if operation == "entries":
            return [[value, value] for value in receiver.values]
        raise UnsupportedNodeConfigurationError(f"Set.{operation} получил неверные аргументы.")

    def _create_datetime(self, operation: str, arguments: list[Any], context: Any) -> datetime:
        """Создать datetime для поддержанных Luxon DateTime.from* вызовов."""
        if not arguments:
            raise UnsupportedNodeConfigurationError(f"DateTime.{operation} требует значение даты.")
        value = arguments[0]
        timezone_name = self._context_mapping(context, "services").get("timezone", "UTC")
        timezone = ZoneInfo(timezone_name) if isinstance(timezone_name, str) else ZoneInfo("UTC")
        try:
            if operation == "fromISO":
                parsed = datetime.fromisoformat(self._js_string(value).replace("Z", "+00:00"))
            elif operation == "fromMillis":
                parsed = datetime.fromtimestamp(self._number(value) / 1000, timezone)
            elif operation == "fromSeconds":
                parsed = datetime.fromtimestamp(self._number(value), timezone)
            elif operation == "fromSQL":
                parsed = datetime.fromisoformat(self._js_string(value).replace(" ", "T", 1))
            elif operation == "fromFormat":
                if len(arguments) < 2 or not isinstance(arguments[1], str):
                    raise ValueError("fromFormat требует шаблон Luxon вторым аргументом.")
                parsed = datetime.strptime(
                    self._js_string(value), self._strptime_pattern(arguments[1])
                )
            else:
                raise UnsupportedNodeConfigurationError(f"DateTime.{operation} не реализован.")
        except (ValueError, TypeError, OverflowError) as error:
            return _InvalidDateTime(str(error))
        return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed

    def _call_datetime_method(self, operation: str, value: datetime, arguments: list[Any]) -> Any:
        """Вычислить используемые в выражениях Luxon методы даты/времени."""
        if operation == "toFormat":
            return (
                _format_luxon_datetime(value, self._js_string(arguments[0]))
                if arguments
                else _format_luxon_datetime(value, "")
            )
        if operation == "toISO":
            result = value.isoformat(timespec="milliseconds")
            return result[:-6] + "Z" if result.endswith("+00:00") else result
        if operation == "toISODate":
            return value.date().isoformat()
        if operation == "toMillis":
            return int(value.timestamp() * 1000)
        if operation == "toSeconds":
            return value.timestamp()
        if operation == "toUTC":
            return value.astimezone(ZoneInfo("UTC"))
        if operation == "setZone":
            if not arguments:
                raise UnsupportedNodeConfigurationError(
                    "DateTime.setZone требует имя часового пояса."
                )
            try:
                return value.astimezone(ZoneInfo(self._js_string(arguments[0])))
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise UnsupportedNodeConfigurationError(
                    f"DateTime.setZone: неизвестный часовой пояс {arguments[0]!r}."
                ) from error
        if operation in {"plus", "minus"}:
            if not arguments:
                raise UnsupportedNodeConfigurationError(f"DateTime.{operation} требует интервал.")
            return self._adjust_datetime(
                value,
                arguments[0],
                arguments[1] if len(arguments) > 1 else None,
                direction=1 if operation == "plus" else -1,
            )
        if operation in {"startOf", "endOf"}:
            if not arguments:
                raise UnsupportedNodeConfigurationError(
                    f"DateTime.{operation} требует единицу времени."
                )
            unit = self._js_string(arguments[0])
            start = self._start_of(value, unit)
            if operation == "startOf":
                return start
            next_start = self._next_period(start, unit)
            return next_start - timedelta(microseconds=1)
        if operation == "diff":
            if not arguments or not isinstance(arguments[0], datetime):
                raise UnsupportedNodeConfigurationError("DateTime.diff требует другую дату.")
            return _Duration((value - arguments[0]).total_seconds())
        if operation == "toSQL":
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        raise UnsupportedNodeConfigurationError(f"Метод DateTime.{operation} не реализован.")

    @staticmethod
    def _adjust_datetime(
        value: datetime,
        raw_amount: Any,
        raw_unit: Any = None,
        *,
        direction: int,
    ) -> datetime:
        """Применить Luxon years/months календарно, а мелкие интервалы как timedelta."""
        if isinstance(raw_amount, Mapping):
            parts = dict(raw_amount)
        elif isinstance(raw_amount, (int, float)) and raw_unit is not None:
            parts = {N8nExpressionEvaluator._js_string(raw_unit): raw_amount}
        else:
            raise UnsupportedNodeConfigurationError(
                "DateTime.plus/minus ожидает объект интервала или число и единицу."
            )

        def amount(*keys: str) -> float:
            return sum(float(parts.get(key, 0) or 0) for key in keys) * direction

        months = (
            amount("years", "year") * 12
            + amount("quarters", "quarter") * 3
            + amount("months", "month")
        )
        month_delta = int(months)
        month_index = value.year * 12 + value.month - 1 + month_delta
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        adjusted = value.replace(year=year, month=month, day=day)
        fractional_month = months - month_delta
        days = amount("weeks", "week") * 7 + amount("days", "day") + fractional_month * 30.436875
        return adjusted + timedelta(
            days=days,
            hours=amount("hours", "hour"),
            minutes=amount("minutes", "minute"),
            seconds=amount("seconds", "second"),
            milliseconds=amount("milliseconds", "millisecond"),
        )

    @staticmethod
    def _strptime_pattern(pattern: str) -> str:
        """Преобразовать распространённые Luxon date tokens в Python strptime."""
        tokens = {
            "yyyy": "%Y",
            "yy": "%y",
            "MMMM": "%B",
            "MMM": "%b",
            "MM": "%m",
            "M": "%m",
            "LL": "%m",
            "L": "%m",
            "dd": "%d",
            "d": "%d",
            "HH": "%H",
            "H": "%H",
            "hh": "%I",
            "h": "%I",
            "mm": "%M",
            "m": "%M",
            "ss": "%S",
            "s": "%S",
            "SSS": "%f",
            "ZZ": "%z",
            "Z": "%z",
        }
        pieces: list[str] = []
        position = 0
        ordered_tokens = sorted(tokens, key=len, reverse=True)
        while position < len(pattern):
            if pattern[position] == "'":
                end = pattern.find("'", position + 1)
                if end < 0:
                    raise ValueError("Незакрытый литерал в шаблоне даты.")
                pieces.append(pattern[position + 1 : end])
                position = end + 1
                continue
            matched = next(
                (token for token in ordered_tokens if pattern.startswith(token, position)), None
            )
            if matched:
                pieces.append(tokens[matched])
                position += len(matched)
            elif pattern[position].isalpha():
                raise ValueError(f"Токен даты {pattern[position]!r} не поддержан при разборе.")
            else:
                pieces.append(pattern[position])
                position += 1
        return "".join(pieces)

    @staticmethod
    def _date_delta(value: Any, unit: Any = None) -> timedelta:
        """Преобразовать Luxon интервал или пару amount/unit в timedelta."""
        if isinstance(value, Mapping):
            parts = value
        elif isinstance(value, (int, float)) and unit is not None:
            parts = {N8nExpressionEvaluator._js_string(unit): value}
        else:
            raise UnsupportedNodeConfigurationError(
                "DateTime interval должен быть объектом или парой число/единица."
            )
        aliases = {
            "years": "days",
            "year": "days",
            "quarters": "days",
            "quarter": "days",
            "months": "days",
            "month": "days",
            "weeks": "days",
            "week": "days",
            "days": "days",
            "day": "days",
            "hours": "hours",
            "hour": "hours",
            "minutes": "minutes",
            "minute": "minutes",
            "seconds": "seconds",
            "second": "seconds",
            "milliseconds": "milliseconds",
            "millisecond": "milliseconds",
        }
        totals: dict[str, float] = {
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 0,
            "milliseconds": 0,
        }
        for raw_unit, raw_amount in parts.items():
            key = N8nExpressionEvaluator._js_string(raw_unit).lower()
            amount = N8nExpressionEvaluator._number(raw_amount)
            if key not in aliases:
                raise UnsupportedNodeConfigurationError(
                    f"Единица времени {key!r} не поддерживается."
                )
            if key in {"year", "years"}:
                totals["days"] += amount * 365.2425
            elif key in {"quarter", "quarters"}:
                totals["days"] += amount * 91.3106
            elif key in {"month", "months"}:
                totals["days"] += amount * 30.436875
            elif key in {"week", "weeks"}:
                totals["days"] += amount * 7
            else:
                totals[aliases[key]] += amount
        return timedelta(**totals)

    @staticmethod
    def _start_of(value: datetime, unit: str) -> datetime:
        """Вернуть начало дня/недели/месяца/года либо меньшего периода."""
        units = {
            "year": value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
            "month": value.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            "week": (value - timedelta(days=value.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
            "day": value.replace(hour=0, minute=0, second=0, microsecond=0),
            "hour": value.replace(minute=0, second=0, microsecond=0),
            "minute": value.replace(second=0, microsecond=0),
            "second": value.replace(microsecond=0),
        }
        try:
            return units[unit]
        except KeyError as error:
            raise UnsupportedNodeConfigurationError(
                f"DateTime.startOf: единица {unit!r} не поддерживается."
            ) from error

    @staticmethod
    def _next_period(value: datetime, unit: str) -> datetime:
        """Перейти к началу следующего календарного периода."""
        if unit == "year":
            return value.replace(year=value.year + 1)
        if unit == "month":
            return (value.replace(day=28) + timedelta(days=4)).replace(day=1)
        delta = {
            "week": timedelta(days=7),
            "day": timedelta(days=1),
            "hour": timedelta(hours=1),
            "minute": timedelta(minutes=1),
            "second": timedelta(seconds=1),
        }
        try:
            return value + delta[unit]
        except KeyError as error:
            raise UnsupportedNodeConfigurationError(
                f"DateTime.endOf: единица {unit!r} не поддерживается."
            ) from error

    def _call_duration(self, operation: str, duration: timedelta, arguments: list[Any]) -> Any:
        """Преобразовать timedelta в Luxon-подобные единицы."""
        seconds = duration.total_seconds()
        if operation == "milliseconds":
            return seconds * 1000
        if operation == "seconds":
            return seconds
        if operation == "minutes":
            return seconds / 60
        if operation == "hours":
            return seconds / 3600
        if operation == "days":
            return seconds / 86400
        if operation == "as" and arguments:
            unit = self._js_string(arguments[0]).lower()
            factors = {
                "milliseconds": 1 / 1000,
                "seconds": 1,
                "minutes": 60,
                "hours": 3600,
                "days": 86400,
                "weeks": 604800,
            }
            if unit in factors:
                return seconds / factors[unit]
        raise UnsupportedNodeConfigurationError(f"Duration.{operation} не поддерживается.")

    def _from_ai(self, arguments: list[Any], context: Any) -> Any:
        """Прочитать значение инструмента `$fromAI` из AI-контекста или взять default."""
        key = self._js_string(arguments[0])
        services = self._context_mapping(context, "services")
        expression_context = services.get("expression_context", {})
        values = expression_context.get("from_ai", services.get("from_ai", {}))
        if isinstance(values, Mapping) and key in values:
            result = values[key]
        elif len(arguments) > 3:
            result = arguments[3]
        else:
            return _UNDEFINED
        if len(arguments) > 2:
            kind = self._js_string(arguments[2]).lower()
            if kind == "number":
                return self._number(result)
            if kind == "boolean":
                return self._js_truthy(result)
            if kind == "json" and isinstance(result, str):
                return json.loads(result)
            if kind in {"string", "date"}:
                return self._js_string(result)
        return result

    def _jmespath(self, data: Any, query: str) -> Any:
        """Вычислить распространённые JMESPath path/projection/filter/function формы."""
        return self._jmespath_query(data, query.strip())

    def _jmespath_query(self, data: Any, query: str) -> Any:
        """Разобрать поля, projections, filters, slices, multiselect hashes и функции."""
        query = query.strip()
        if not query:
            return data
        function_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", query, re.DOTALL)
        if function_match:
            name, raw_arguments = function_match.groups()
            arguments = self._split_jmespath_arguments(raw_arguments)
            if name == "sort_by" and len(arguments) == 2:
                source = self._jmespath_query(data, arguments[0])
                sort_expression = arguments[1].strip()
                if sort_expression.startswith("&"):
                    sort_expression = sort_expression[1:]
                if isinstance(source, list):
                    return sorted(
                        source, key=lambda entry: self._jmespath_query(entry, sort_expression)
                    )
                return []
            values = [self._jmespath_query(data, argument) for argument in arguments]
            if name == "length" and len(values) == 1:
                return len(values[0]) if isinstance(values[0], (list, str, Mapping)) else _UNDEFINED
            if name in {"max", "min", "sum"} and len(values) == 1:
                sequence = values[0] if isinstance(values[0], list) else []
                return (
                    max(sequence)
                    if name == "max" and sequence
                    else min(sequence)
                    if name == "min" and sequence
                    else sum(sequence)
                    if name == "sum"
                    else None
                )
            raise UnsupportedNodeConfigurationError(f"JMESPath-функция {name!r} не реализована.")

        pieces = self._split_jmespath_path(query)
        if len(pieces) > 1:
            head = self._jmespath_segment(data, pieces[0])
            tail = ".".join(pieces[1:])
            if isinstance(head, list):
                return [
                    self._jmespath_query(value, tail)
                    for value in head
                    if value is not None and value is not _UNDEFINED
                ]
            return self._jmespath_query(head, tail)
        return self._jmespath_segment(data, query)

    def _jmespath_segment(self, data: Any, segment: str) -> Any:
        """Вычислить один путь, индекс, slice, projection или filter."""
        segment = segment.strip()
        if segment.startswith("{") and segment.endswith("}"):
            result = {}
            for field in self._split_jmespath_arguments(segment[1:-1]):
                key, separator, value_expression = field.partition(":")
                if not separator:
                    raise UnsupportedNodeConfigurationError(
                        "JMESPath multiselect hash требует key: expression."
                    )
                result[key.strip()] = self._jmespath_query(data, value_expression.strip())
            return result

        field_match = re.match(r"^([A-Za-z_][\w-]*)(.*)$", segment, re.DOTALL)
        if field_match:
            key, selectors = field_match.groups()
            value = data.get(key, _UNDEFINED) if isinstance(data, Mapping) else _UNDEFINED
        else:
            value, selectors = data, segment
        while selectors:
            selector_match = re.match(r"^\[([^\]]*)\](.*)$", selectors, re.DOTALL)
            if selector_match is None:
                raise UnsupportedNodeConfigurationError(
                    f"JMESPath-селектор {selectors!r} не распознан."
                )
            selector, selectors = selector_match.groups()
            if selector == "":
                value = list(value) if isinstance(value, (list, tuple)) else []
            elif selector.startswith("?"):
                predicate = selector[1:].strip()
                value = (
                    [entry for entry in value if self._jmespath_predicate(entry, predicate)]
                    if isinstance(value, list)
                    else []
                )
            elif ":" in selector:
                start, _, stop = selector.partition(":")
                low = int(start) if start else None
                high = int(stop) if stop else None
                value = value[low:high] if isinstance(value, list) else []
            else:
                index = int(selector)
                value = (
                    value[index]
                    if isinstance(value, list) and -len(value) <= index < len(value)
                    else _UNDEFINED
                )
        return value

    def _jmespath_predicate(self, entry: Any, predicate: str) -> bool:
        """Сопоставить одно поле с JMESPath literal через сравнение."""
        match = re.fullmatch(r"([\w.-]+)\s*(==|!=|>=|<=|>|<)\s*(.+)", predicate, re.DOTALL)
        if not match or not isinstance(entry, Mapping):
            return False
        path, operator, raw = match.groups()
        actual = self._jmespath_query(entry, path)
        if raw.startswith("`") and raw.endswith("`"):
            try:
                expected = json.loads(raw[1:-1])
            except json.JSONDecodeError:
                expected = raw[1:-1]
        elif raw.startswith("'") and raw.endswith("'"):
            expected = raw[1:-1]
        elif raw in {"true", "false", "null"}:
            expected = {"true": True, "false": False, "null": None}[raw]
        else:
            try:
                expected = float(raw) if "." in raw else int(raw)
            except ValueError:
                expected = raw
        if operator in {"==", "!="}:
            result = self._js_loose_equal(actual, expected)
            return result if operator == "==" else not result
        try:
            return {
                ">=": actual >= expected,
                "<=": actual <= expected,
                ">": actual > expected,
                "<": actual < expected,
            }[operator]
        except TypeError:
            return False

    @staticmethod
    def _split_jmespath_arguments(source: str) -> list[str]:
        """Разбить query-аргументы по запятым верхнего уровня."""
        return N8nExpressionEvaluator._split_nested(source, ",")

    @staticmethod
    def _split_jmespath_path(source: str) -> list[str]:
        """Разбить query-путь по точкам верхнего уровня."""
        return N8nExpressionEvaluator._split_nested(source, ".")

    @staticmethod
    def _split_nested(source: str, delimiter: str) -> list[str]:
        """Разбить строку, игнорируя разделители внутри brackets/quotes."""
        parts: list[str] = []
        start = 0
        depth = 0
        quote_char: str | None = None
        escaped = False
        for index, character in enumerate(source):
            if quote_char:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote_char:
                    quote_char = None
                continue
            if character in {"'", '"', "`"}:
                quote_char = character
            elif character in "([{":
                depth += 1
            elif character in ")]}" and depth:
                depth -= 1
            elif character == delimiter and depth == 0:
                parts.append(source[start:index].strip())
                start = index + 1
        parts.append(source[start:].strip())
        return [part for part in parts if part]

    @staticmethod
    def _select_item(
        selector: str,
        items: tuple[WorkflowItem, ...],
        arguments: list[Any],
        *,
        item: WorkflowItem | None = None,
    ) -> Any:
        """Resolve the common first/last/all selectors for node and input items."""
        if selector == "itemMatching":
            if not arguments:
                raise UnsupportedNodeConfigurationError("itemMatching требует индекс item.")
            index = int(N8nExpressionEvaluator._number(arguments[0]))
            try:
                return items[index]
            except IndexError:
                return _UNDEFINED
        if arguments and selector in {"first", "last", "all"}:
            if len(arguments) > 2 or any(
                not isinstance(argument, (int, float)) or isinstance(argument, bool)
                for argument in arguments
            ):
                raise UnsupportedNodeConfigurationError(
                    f"{selector} принимает только индекс ветки и номер запуска."
                )
        elif arguments:
            raise UnsupportedNodeConfigurationError(
                f"Селектор {selector} не принимает дополнительные аргументы."
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
        if (
            value is _UNDEFINED
            or value is None
            or value is False
            or value == 0
            or value == ""
            or (isinstance(value, float) and math.isnan(value))
        ):
            return False
        return True

    @staticmethod
    def _js_string(value: Any) -> str:
        """Преобразовать простое JSON-значение в строку в стиле JavaScript."""
        if value is _UNDEFINED:
            return "undefined"
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Mapping):
            return "[object Object]"
        if isinstance(value, _SetValue):
            value = list(value.values)
        if isinstance(value, list):
            return ",".join(
                ""
                if item is None or item is _UNDEFINED
                else N8nExpressionEvaluator._js_string(item)
                for item in value
            )
        return str(value)

    @staticmethod
    def _number(value: Any) -> int | float:
        """Преобразовать поддержанное числовое значение или вернуть ошибку."""
        if value is _UNDEFINED:
            return math.nan
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return 0
            try:
                if stripped.lower().startswith(("0x", "+0x", "-0x")):
                    return int(stripped, 16)
                number = float(stripped)
                return int(number) if math.isfinite(number) and number.is_integer() else number
            except ValueError:
                return math.nan
        if isinstance(value, list):
            return N8nExpressionEvaluator._number(N8nExpressionEvaluator._js_string(value))
        return math.nan

    @staticmethod
    def _context_mapping(context: Any, name: str) -> dict[str, Any]:
        """Получить именованную mapping-часть runtime context или пустой словарь."""
        if context is None:
            return {}
        value = (
            context.get(name, {}) if isinstance(context, Mapping) else getattr(context, name, {})
        )
        return dict(value) if isinstance(value, Mapping) else {}

    @classmethod
    def _public_value(cls, value: Any) -> Any:
        """Преобразовать JS undefined и коллекции к JSON-совместимым Python-значениям."""
        if value is _UNDEFINED:
            return None
        if isinstance(value, _SetValue):
            return [cls._public_value(item) for item in value.values]
        if isinstance(value, Mapping):
            return {
                key: cls._public_value(item)
                for key, item in value.items()
                if item is not _UNDEFINED
            }
        if isinstance(value, list):
            return [cls._public_value(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, timedelta):
            return value.total_seconds() * 1000
        return value

    @classmethod
    def _spread_values(cls, value: Any) -> list[Any]:
        """Получить значения, которые можно безопасно развернуть через `...`."""
        if isinstance(value, _SetValue):
            return list(value.values)
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            return list(value)
        if isinstance(value, Mapping):
            return list(value)
        raise UnsupportedNodeConfigurationError("Spread требует массив, строку, объект или Set.")

    @staticmethod
    def _js_typeof(value: Any) -> str:
        """Вернуть результат JavaScript typeof для поддерживаемых значений."""
        if value is _UNDEFINED:
            return "undefined"
        if value is None or isinstance(value, (Mapping, list, _SetValue)):
            return "object"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (_Function, _Namespace)):
            return "function"
        return "object"

    @classmethod
    def _js_loose_equal(cls, left: Any, right: Any) -> bool:
        """Приблизить JavaScript `==` для nullish, boolean и числовых строк."""
        if left is right:
            return True
        if (left is None or left is _UNDEFINED) and (right is None or right is _UNDEFINED):
            return True
        if type(left) is type(right):
            return left == right
        if isinstance(left, bool):
            return cls._js_loose_equal(int(left), right)
        if isinstance(right, bool):
            return cls._js_loose_equal(left, int(right))
        if isinstance(left, (int, float)) and isinstance(right, str):
            return left == cls._number(right)
        if isinstance(right, (int, float)) and isinstance(left, str):
            return cls._number(left) == right
        return False

    @staticmethod
    def _is_empty(value: Any) -> bool:
        """Проверить nullish/пустые строки и коллекции для n8n ifEmpty/compact."""
        return value is _UNDEFINED or value is None or value == "" or value == [] or value == {}

    @classmethod
    def _parse_int(cls, value: Any, radix: Any = 10) -> int | float:
        """Реализовать распространённую форму JavaScript parseInt."""
        text = cls._js_string(value).strip()
        base = int(cls._number(radix)) if radix is not _UNDEFINED else 10
        if base == 0:
            base = 16 if text.lower().startswith("0x") else 10
        try:
            match = re.match(r"^[+-]?(?:0[xX])?[0-9a-zA-Z]+", text)
            return int(match.group(0), base) if match else math.nan
        except ValueError:
            return math.nan

    @classmethod
    def _parse_float(cls, value: Any) -> int | float:
        """Реализовать JavaScript parseFloat, принимающий числовой префикс строки."""
        match = re.match(
            r"^[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)",
            cls._js_string(value).strip(),
        )
        if not match:
            return math.nan
        number = float(match.group(0))
        return int(number) if math.isfinite(number) and number.is_integer() else number
