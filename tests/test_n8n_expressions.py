"""Проверки безопасного ограниченного parser-а выражений n8n."""

import pytest

from n8n_to_python.converters.errors import UnsupportedNodeConfigurationError
from n8n_to_python.converters.expressions import N8nExpressionEvaluator
from n8n_to_python.runtime.models import WorkflowItem


def test_expression_reads_nested_json_and_bracket_fields() -> None:
    """Поддержать оба распространённых способа доступа к ключам JSON item."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"customer": {"name": "Ada"}, "postal-code": "N1"})

    assert evaluator.evaluate("={{ $json.customer.name }}", item) == "Ada"
    assert evaluator.evaluate('={{ $json["postal-code"] }}', item) == "N1"


def test_expression_supports_coalescing_functions_and_json_serialization() -> None:
    """Вычислить типичные nullish fallback, конкатенацию и JSON.stringify."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"optional": None, "count": 4, "active": True})

    assert evaluator.evaluate('={{ $json.optional ?? "default" }}', item) == "default"
    assert evaluator.evaluate('={{ "count=" + String($json.count + 1) }}', item) == "count=5"
    assert evaluator.evaluate("={{ JSON.stringify($json) }}", item) == (
        '{"optional":null,"count":4,"active":true}'
    )


def test_expression_supports_conditional_and_short_circuit_operators() -> None:
    """Не вычислять правую часть короткого выражения, если она не нужна."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"enabled": False, "count": 0})

    assert evaluator.evaluate("={{ $json.enabled ? 1 : 2 }}", item) == 2
    assert evaluator.evaluate("={{ $json.count || 9 }}", item) == 9
    assert evaluator.evaluate("={{ $json.enabled && $json.missing }}", item) is False


def test_expression_interpolates_values_inside_regular_text() -> None:
    """Подставить `$json` поле в заголовок или текстовый шаблон."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"token": "secret"})

    assert evaluator.evaluate("Bearer {{ $json.token }}", item) == "Bearer secret"


def test_expression_resolves_nested_json_body_values() -> None:
    """Обработать выражения глубоко внутри JSON body/query структуры."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"user": {"id": 27}})

    assert evaluator.evaluate(
        {"userId": "={{ $json.user.id }}", "flags": ["{{ $itemIndex }}"]}, item
    ) == {"userId": 27, "flags": [0]}


def test_expression_supports_safe_object_literals_and_string_methods() -> None:
    """Собрать новый JSON object и применить разрешённые строковые операции."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={"user": {"name": "  ada lovelace "}})

    assert evaluator.evaluate(
        "={{ { displayName: $json.user.name.trim().toUpperCase() } }}", item
    ) == {"displayName": "ADA LOVELACE"}


def test_expression_rejects_arbitrary_python_or_javascript_execution() -> None:
    """Отказать в вызове функций и переменных вне безопасного списка."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={})

    with pytest.raises(UnsupportedNodeConfigurationError, match="не входит"):
        evaluator.evaluate("={{ process.env.SECRET }}", item)
