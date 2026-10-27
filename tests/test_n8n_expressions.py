"""Проверки безопасного ограниченного parser-а выражений n8n."""

import copy
import re
from pathlib import Path

import pytest

from n8n_to_python.converters.errors import UnsupportedNodeConfigurationError
from n8n_to_python.converters.expressions import N8nExpressionEvaluator
from n8n_to_python.runtime.executors import NodeExecutionContext
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


def test_expression_formats_now_with_luxon_date_tokens() -> None:
    """Format `$now` with the Luxon tokens used in workflow filenames and subjects."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={})
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        services={"timezone": "UTC"},
    )

    before = datetime.now(ZoneInfo("UTC"))
    formatted = evaluator.evaluate(
        '={{ $now.toFormat("yyyy-LL-dd HH:mm:ss") }}', item, context=context
    )
    after = datetime.now(ZoneInfo("UTC"))
    parsed = datetime.strptime(formatted, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    assert before.replace(microsecond=0) <= parsed <= after.replace(microsecond=0)
    assert evaluator.evaluate(
        'Report {{ $now.toFormat("dd.LL.yyyy") }}', item, context=context
    ).startswith("Report ")


def test_expression_rejects_unknown_luxon_format_tokens() -> None:
    """Explain when a pattern requests a Luxon token outside the implemented subset."""
    evaluator = N8nExpressionEvaluator()

    with pytest.raises(UnsupportedNodeConfigurationError, match="qqqq"):
        evaluator.evaluate('={{ $now.toFormat("yyyy-LL-dd qqqq") }}', WorkflowItem(json={}))


def test_expression_formats_full_weekday_and_reports_valid_luxon_date() -> None:
    """Поддержать Luxon cccc и DateTime.isValid из справочника expressions."""
    evaluator = N8nExpressionEvaluator()
    item = WorkflowItem(json={})

    assert evaluator.evaluate('={{ $now.toFormat("cccc") }}', item)
    assert evaluator.evaluate('={{ DateTime.fromISO("2026-10-27").isValid }}', item) is True


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


def test_expression_reads_current_input_and_linked_previous_node_items() -> None:
    """Resolve `$input` and linked/selected outputs from a named earlier node."""
    evaluator = N8nExpressionEvaluator()
    first = WorkflowItem(
        json={"value": "first"},
        binary={"document": {"fileName": "first.pdf", "mimeType": "application/pdf"}},
    )
    second = WorkflowItem(
        json={"value": "second"},
        binary={"document": {"fileName": "second.pdf", "mimeType": "application/pdf"}},
    )
    current = WorkflowItem(json={"current": True}, lineage={"Source Node": 1})
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        input_items=(first, second),
        node_outputs={"Source Node": (first, second)},
    )

    assert (
        evaluator.evaluate("={{ $('Source Node').item.json.value }}", current, context=context)
        == "second"
    )
    assert (
        evaluator.evaluate(
            "={{ $('Source Node').item.binary.document.fileName }}", current, context=context
        )
        == "second.pdf"
    )
    assert (
        evaluator.evaluate("={{ $('Source Node').first().json.value }}", current, context=context)
        == "first"
    )
    assert (
        evaluator.evaluate("={{ $('Source Node').last().json.value }}", current, context=context)
        == "second"
    )
    assert (
        evaluator.evaluate(
            "={{ $('Source Node').all().first().json.value }}", current, context=context
        )
        == "first"
    )
    assert (
        evaluator.evaluate(
            "={{ $('Source Node').all().last().binary.document.fileName }}",
            current,
            context=context,
        )
        == "second.pdf"
    )
    assert evaluator.evaluate("={{ $input.item.json.value }}", second, context=context) == "second"
    assert (
        evaluator.evaluate(
            "={{ $input.all()[0].binary.document.fileName }}", current, context=context
        )
        == "first.pdf"
    )
    assert (
        evaluator.evaluate(
            "={{ $input.all()[0].json.value + ':' + $input.all().length }}",
            current,
            context=context,
        )
        == "first:2"
    )
    assert (
        evaluator.evaluate("={{ $input.all().first().json.value }}", current, context=context)
        == "first"
    )
    assert (
        evaluator.evaluate("={{ $input.all().last().json.value }}", current, context=context)
        == "second"
    )


def test_expression_rejects_first_or_last_on_empty_all_items() -> None:
    """Fail with a useful message when a chained selector has no input items."""
    evaluator = N8nExpressionEvaluator()
    current = WorkflowItem(json={})
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        input_items=(),
        node_outputs={"Empty Source": ()},
    )

    with pytest.raises(UnsupportedNodeConfigurationError, match="пустого массива"):
        evaluator.evaluate("={{ $('Empty Source').all().first().json }}", current, context=context)


def test_expression_supports_execution_id_and_http_request_body_expressions() -> None:
    """Вычислить имя файла и JSON body, как в HTTP Request пользовательского workflow."""
    evaluator = N8nExpressionEvaluator()
    current = WorkflowItem(json={"output": "Краткий отчёт"})
    settings = WorkflowItem(
        json={
            "mail_from": "report@example.test",
            "mail_to": "first@example.test, second@example.test",
            "mail_subject": "Отчёт",
        }
    )
    zip_file = WorkflowItem(json={"zip_base64": "UEsDBA=="})
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        services={"expression_context": {"execution": {"id": "run-42"}}},
        input_items=(current,),
        node_outputs={
            "Settings": (settings,),
            "ZIP - Save attachment base64": (zip_file,),
        },
    )

    assert (
        evaluator.evaluate("n8n-csv-report-{{ $execution.id }}", current, context=context)
        == "n8n-csv-report-run-42"
    )
    body = evaluator.evaluate(
        "={{ JSON.stringify({ from: $('Settings').first().json.mail_from, "
        "to: $('Settings').first().json.mail_to.split(',').map(email => email.trim()).filter(Boolean), "
        "subject: $('Settings').first().json.mail_subject, text: $json.output, "
        "attachments: [{ filename: 'tasks.zip', "
        "content: $('ZIP - Save attachment base64').first().json.zip_base64 }] }) }}",
        current,
        context=context,
    )
    assert body == (
        '{"from":"report@example.test","to":["first@example.test",'
        '"second@example.test"],"subject":"Отчёт","text":"Краткий отчёт",'
        '"attachments":[{"filename":"tasks.zip","content":"UEsDBA=="}]}'
    )


def test_all_expression_reference_examples_and_recipes_evaluate_without_errors() -> None:
    """Проверить все 533 примера локального справочника в насыщенном runtime-контексте."""
    project_root = Path(__file__).parents[1]
    references = tuple(project_root.glob("N8N_EXPRESSIONS*.md"))
    if not references:
        pytest.skip("Локальный справочник expressions не включён в репозиторий.")
    reference = references[0].read_text(encoding="utf-8")
    examples = re.findall(
        r"^\*\*(\d{3})\. (.*?)\*\*\s*^```(?:javascript|js)?\s*$(.*?)^```\s*$",
        reference,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert len(examples) == 533, "Ожидались все пронумерованные примеры справочника."
    numbered_titles = {
        code.strip()[2:-2].strip(): f"{number} {title}" for number, title, code in examples
    }

    posts = [
        {
            "id": "post-1",
            "post_id": "post-1",
            "user_id": 1,
            "author": "alice",
            "name": "Ada",
            "title": "Заголовок <пример> &",
            "text": "Банк опубликовал полезный пост",
            "topic": "Технологии",
            "lang": "ru",
            "status": "published",
            "likes": 1200,
            "views": 5000,
            "reposts": 40,
            "shares": 20,
            "created_at": "2026-10-26T12:30:00+03:00",
            "url": "https://example.test/posts/1",
            "comments": [{"text": "Комментарий", "author": "bob"}],
        },
        {
            "id": "post-2",
            "post_id": "post-2",
            "user_id": 2,
            "author": "bob",
            "name": "Grace",
            "title": "Второй пост",
            "text": "Другой текст",
            "topic": "Технологии",
            "lang": "en",
            "status": "draft",
            "likes": 25,
            "views": 100,
            "reposts": 2,
            "shares": 3,
            "created_at": "2026-10-25T08:15:00Z",
            "url": "https://example.test/posts/2",
            "comments": [],
        },
    ]
    json_data = {
        "id": 123,
        "user_id": 7,
        "post_id": "post-1",
        "target_id": "post-1",
        "name": "Ada Lovelace",
        "title": "Заголовок <пример> &",
        "text": "Банк: тестовый текст https://example.test/path",
        "author": "alice",
        "email": "ada@example.test",
        "token": "secret-token",
        "password": "secret-password",
        "status": "published",
        "lang": "ru",
        "active": True,
        "likes": 1200,
        "views": 5000,
        "income": 1500.25,
        "price": "1 500,25",
        "amount": 1500.25,
        "count": 4,
        "value": "12.5",
        "url": "https://example.test/posts/1/",
        "urls": ["https://example.test/a", "https://example.test/b"],
        "phone": "+375291234567",
        "filename": "report.xlsx",
        "binaryPropertyName": "data",
        "date": "2026-10-26T12:30:00+03:00",
        "timestamp": 1793000000,
        "timestamp_ms": 1793000000000,
        "start": "2026-10-26T00:00:00Z",
        "end": "2026-10-27T00:00:00Z",
        "source_url": "https://example.test/api",
        "tags": ["n8n", "python", "n8n"],
        "ids": [1, 2, 3],
        "values": [1, 2, 2, "", None, 5],
        "numbers": [1, 2, 3, 4, 5],
        "operations": ["sum", "filter"],
        "posts": posts,
        "users": [
            {"id": 1, "name": "Ada", "username": "ada", "active": True},
            {"id": 2, "name": "Grace", "username": "grace", "active": False},
        ],
        "profile": {"id": 123, "name": "Ada", "status": "active", "password": "secret"},
        "metadata": {"source": "fixture", "labels": ["test", "n8n"]},
        "records": [
            {"id": 1, "error": {"message": "первая ошибка", "name": "Error"}},
            {"id": 2, "error": None},
        ],
        "results": [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
        "analyses": [{"id": "post-1", "allow": True}],
        "data": {"results": [{"id": 1, "value": "ok"}], "items": posts},
        "output": "Отчёт модели",
        "summary": {"total": 2, "done": 1, "remaining": 1},
        "error": {"message": "Синтетическая ошибка", "name": "Error"},
        "message": {"text": "Пример сообщения", "chat": {"id": 100}, "from": {"id": 7}},
        "callback_query": {
            "id": "callback-1",
            "data": "approve:42",
            "message": {"message_id": 12, "chat": {"id": 100}},
            "from": {"id": 7, "username": "ada"},
        },
        "update_id": 77,
        "statusCode": 200,
        "next_page": None,
        "binaryProperty": "data",
        "mail_from": "report@example.test",
        "mail_to": "one@example.test,two@example.test",
        "mail_subject": "Отчёт",
        "api_base_url": "https://example.test/api",
        "author_name": "Ada",
        "post_limit": 15,
    }
    binary = {
        "data": {
            "fileName": "report.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "fileExtension": "xlsx",
            "fileSize": "10 KB",
        }
    }
    first = WorkflowItem(json=json_data, binary=binary)
    second = WorkflowItem(json={**json_data, "id": 456}, binary=binary)
    node_names = set(re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)", reference))
    lineage = {node_name: 1 for node_name in node_names}
    current = WorkflowItem(json=json_data, binary=binary, lineage=lineage)
    node_outputs = {node_name: (first, second) for node_name in node_names}
    node_output_runs = {node_name: (((first, second),),) for node_name in node_names}
    javascript_blocks = re.findall(
        r"^```(?:javascript|js)\s*$(.*?)^```\s*$",
        reference,
        flags=re.MULTILINE | re.DOTALL,
    )
    expression_sources = list(
        dict.fromkeys(
            expression.strip()
            for block in javascript_blocks
            for expression in re.findall(r"\{\{\s*(.*?)\s*\}\}", block, re.DOTALL)
        )
    )
    assert len(expression_sources) > 533, "Ожидались ещё сквозные рецепты после примеров 001–533."
    context = NodeExecutionContext(
        input_port=None,
        invocation_number=1,
        state={},
        services={
            "timezone": "Europe/Minsk",
            "execution": {"id": "run-42", "mode": "manual"},
            "workflow": {"id": "workflow-1", "name": "Expression examples"},
            "vars": {"api_base_url": "https://example.test/api", "author": "Ada"},
            "env": {"API_KEY": "test-key"},
            "page_count": 1,
            "response": {"statusCode": 200, "body": {"next_page": None}},
            "request": {"method": "GET", "url": "https://example.test/api"},
            "from_ai": {"since": "2026-10-01", "filters": {"active": True}},
        },
        input_items=(first, second),
        node_outputs=node_outputs,
        node_output_runs=node_output_runs,
    )
    evaluator = N8nExpressionEvaluator()
    failures = []
    for source in expression_sources:
        label = numbered_titles.get(source, "сквозной рецепт")
        expression = f"={{ {source} }}"
        item_json = copy.deepcopy(json_data)
        item_json["a"] = {"left": 1} if "$json.a ?? {}" in source else [1, 2]
        item_json["b"] = {"right": 2} if "$json.b ?? {}" in source else [3, 4]
        item = current.model_copy(update={"json_data": item_json})
        try:
            evaluator.evaluate(expression, item, context=context)
        except Exception as error:  # noqa: BLE001 - report each unsupported guide entry together.
            failures.append(f"{label}: {type(error).__name__}: {error} ({source})")
    assert not failures, "Не вычислились примеры справочника:\n" + "\n".join(failures)
