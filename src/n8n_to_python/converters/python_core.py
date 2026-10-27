"""Генерация автономного Python-проекта с локальной копией runtime n8n."""

import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..application.models.generated_project import GeneratedProject
from ..application.ports.workflow_generator import GenerationBackend, WorkflowGenerator
from ..application.services.progress import ProgressReporter
from ..domain.models.workflow import Workflow
from .nodes import NodeConverterRegistry, default_node_converter_registry

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "privatekey",
    "clientsecret",
    "accesskey",
    "cookie",
)

_EXTENSION_FILES = {
    "n8n-nodes-base.code": "src/n8n_to_python/converters/code_node.py",
    "n8n-nodes-base.httpRequest": "src/n8n_to_python/converters/http_request.py",
    "n8n-nodes-base.dataTable": "src/n8n_to_python/converters/integrations.py",
    "n8n-nodes-base.postgres": "src/n8n_to_python/converters/integrations.py",
    "n8n-nodes-base.emailSend": "src/n8n_to_python/converters/integrations.py",
    "n8n-nodes-base.compression": "src/n8n_to_python/converters/file_nodes.py",
    "n8n-nodes-base.extractFromFile": "src/n8n_to_python/converters/file_nodes.py",
}


class PythonCoreGenerator(WorkflowGenerator):
    """Собрать отдельный проект, который запускает workflow через Python Core."""

    backend = GenerationBackend.PYTHON_CORE

    def __init__(
        self,
        *,
        converter_registry: NodeConverterRegistry | None = None,
        package_root: Path | None = None,
    ):
        """Сохранить реестр поддержки и путь к исходникам, которые попадут в проект."""
        self._converter_registry = converter_registry or default_node_converter_registry()
        self._package_root = package_root or Path(__file__).resolve().parents[1]

    async def generate(
        self,
        workflow: Workflow,
        *,
        progress: ProgressReporter,
    ) -> GeneratedProject:
        """Создать файлы проекта в памяти; запись на диск остаётся Infrastructure задачей."""
        with progress.stage(f"{self.backend.value}:analyze-workflow"):
            manual_actions = self._inspect_workflow(workflow)
            workflow_data, redacted_fields, removed_credentials = self._prepare_workflow(workflow)
            manual_actions.extend(
                f"Секретное значение в поле {field_path} заменено маркером. "
                "Добавьте значение вручную после проверки инструкции."
                for field_path in redacted_fields
            )

        with progress.stage(f"{self.backend.value}:render-project"):
            files = self._copy_runtime_sources()
            files.update(
                {
                    "pyproject.toml": self._render_pyproject(workflow.name),
                    "README.md": self._render_readme(workflow.name),
                    "MANUAL_CHANGES.md": self._render_manual_changes(
                        workflow,
                        manual_actions,
                        redacted_fields,
                        removed_credentials,
                    ),
                    "src/generated_workflow/__init__.py": '"""Запускаемый Python Core workflow."""\n',
                    "src/generated_workflow/workflow.json": json.dumps(
                        workflow_data, ensure_ascii=False, indent=2
                    )
                    + "\n",
                    "src/generated_workflow/manual_changes.py": self._render_manual_actions(
                        manual_actions
                    ),
                    "src/generated_workflow/services.py": _SERVICES_SOURCE,
                    "src/generated_workflow/main.py": self._render_main_source(),
                }
            )
        return GeneratedProject(files=files)

    @staticmethod
    def _render_main_source() -> str:
        """Return the generated project's backend-specific execution entry point."""
        return _MAIN_SOURCE

    def _inspect_workflow(self, workflow: Workflow) -> list[str]:
        """Собрать точные ошибки/предупреждения по неподдержанным узлам и конфигурациям."""
        actions: list[str] = []
        for node in workflow.nodes:
            for diagnostic in self._converter_registry.inspect_node(node):
                extension_file = _EXTENSION_FILES.get(
                    node.type, "src/n8n_to_python/converters/nodes.py"
                )
                actions.append(
                    f"Узел {node.name!r} ({node.type}, typeVersion {node.type_version}): "
                    f"{diagnostic.message} Точка расширения: `{extension_file}`."
                )
        return actions

    def _prepare_workflow(
        self,
        workflow: Workflow,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Скопировать workflow-данные, убрав credentials и замаскировав секретные поля."""
        workflow_data = workflow.model_dump(mode="json", by_alias=True, exclude_none=True)
        redacted_fields: list[str] = []
        removed_credentials: list[str] = []
        for node_data in workflow_data["nodes"]:
            credentials = node_data.pop("credentials", None)
            if credentials:
                credential_types = ", ".join(sorted(str(name) for name in credentials))
                removed_credentials.append(
                    f"{node_data['name']} ({node_data['type']} v{node_data['typeVersion']}): "
                    f"{credential_types or 'credential reference'}"
                )
            parameters = node_data.get("parameters", {})
            if node_data["type"] == "n8n-nodes-base.httpRequest":
                parameters = self._redact_http_secrets(
                    parameters,
                    node_data["name"],
                    redacted_fields,
                )
            node_data["parameters"] = self._redact_mapping(
                parameters,
                f"{node_data['name']}.parameters",
                redacted_fields,
            )
        return workflow_data, redacted_fields, removed_credentials

    @classmethod
    def _redact_http_secrets(
        cls,
        parameters: dict[str, Any],
        node_name: str,
        redacted_fields: list[str],
    ) -> dict[str, Any]:
        """Маскировать секреты в именованных HTTP headers, query и URL query."""
        parameters = dict(parameters)
        for group_name in ("headerParameters", "queryParameters"):
            group = parameters.get(group_name)
            if not isinstance(group, dict):
                continue
            group_copy = dict(group)
            pairs = group_copy.get("parameters")
            if isinstance(pairs, list):
                redacted_pairs = []
                for index, pair in enumerate(pairs):
                    if not isinstance(pair, dict):
                        redacted_pairs.append(pair)
                        continue
                    pair_copy = dict(pair)
                    name = str(pair_copy.get("name", ""))
                    if cls._is_sensitive_key(name) and not cls._is_expression(
                        pair_copy.get("value")
                    ):
                        pair_copy["value"] = "<SET_MANUALLY>"
                        redacted_fields.append(
                            f"{node_name}.parameters.{group_name}.parameters[{index}].value"
                        )
                    redacted_pairs.append(pair_copy)
                group_copy["parameters"] = redacted_pairs
            parameters[group_name] = group_copy

        url = parameters.get("url")
        if isinstance(url, str) and not cls._is_expression(url):
            parsed_url = urlsplit(url)
            netloc = parsed_url.netloc
            if "@" in netloc:
                netloc = netloc.rsplit("@", maxsplit=1)[1]
                redacted_fields.append(f"{node_name}.parameters.url.userinfo")
            query: list[tuple[str, str]] = []
            for name, value in parse_qsl(parsed_url.query, keep_blank_values=True):
                if cls._is_sensitive_key(name) and not cls._is_expression(value):
                    value = "<SET_MANUALLY>"
                    redacted_fields.append(f"{node_name}.parameters.url.query.{name}")
                query.append((name, value))
            parameters["url"] = urlunsplit(
                (
                    parsed_url.scheme,
                    netloc,
                    parsed_url.path,
                    urlencode(query),
                    parsed_url.fragment,
                )
            )
        return parameters

    @classmethod
    def _redact_mapping(
        cls,
        value: Any,
        path: str,
        redacted_fields: list[str],
    ) -> Any:
        """Маскировать чувствительные значения по имени поля, сохраняя остальные параметры."""
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                child_path = f"{path}.{key}"
                if cls._is_sensitive_key(str(key)) and not cls._is_expression(item):
                    redacted[key] = "<SET_MANUALLY>"
                    redacted_fields.append(child_path)
                else:
                    redacted[key] = cls._redact_mapping(item, child_path, redacted_fields)
            return redacted
        if isinstance(value, list):
            return [
                cls._redact_mapping(item, f"{path}[{index}]", redacted_fields)
                for index, item in enumerate(value)
            ]
        return value

    @staticmethod
    def _is_sensitive_key(name: str) -> bool:
        """Распознать распространённые названия полей, содержащих credential values."""
        normalized_name = re.sub(r"[^a-z0-9]", "", name.casefold())
        return any(marker in normalized_name for marker in _SENSITIVE_KEY_PARTS)

    @staticmethod
    def _is_expression(value: Any) -> bool:
        """Не маскировать n8n expression: там хранится ссылка, а не literal secret."""
        return isinstance(value, str) and (value.startswith("=") or "{{" in value)

    def _copy_runtime_sources(self) -> dict[str, str]:
        """Включить в результат локальные runtime/converter modules без CLI приложения."""
        runtime_files: dict[str, str] = {}
        for source_path in sorted(self._package_root.rglob("*.py")):
            relative_path = source_path.relative_to(self._package_root)
            if relative_path.parts[0] == "cli":
                continue
            target_path = Path("src") / "n8n_to_python" / relative_path
            runtime_files[target_path.as_posix()] = source_path.read_text(encoding="utf-8")
        if not runtime_files:
            raise FileNotFoundError(f"Исходники Python runtime не найдены: {self._package_root}")
        return runtime_files

    @staticmethod
    def _render_pyproject(workflow_name: str) -> str:
        """Создать uv/Hatchling manifest с зависимостями автономного runtime."""
        project_name = _project_slug(workflow_name)
        return f"""[project]
name = {json.dumps(project_name)}
version = "0.1.0"
description = "Python Core runtime generated from an n8n workflow"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "aiohttp>=3.14.5",
    "aiosmtplib>=5.1.3",
    "asyncpg>=0.32.0",
    "pydantic>=2.0",
    "pypdf>=6.21.2",
    "sqlalchemy[asyncio]>=2.1.6",
    "tzdata>=2026.5",
]

[project.scripts]
run-workflow = "generated_workflow.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/generated_workflow", "src/n8n_to_python"]

[tool.hatch.build.targets.wheel.force-include]
"src/generated_workflow/workflow.json" = "generated_workflow/workflow.json"
"""

    @staticmethod
    def _render_readme(workflow_name: str) -> str:
        """Объяснить установку, запуск и настройку внешних сервисов."""
        return f"""# {workflow_name}

Автономный проект Python Core, созданный из n8n workflow. Runtime и адаптеры находятся в `src/n8n_to_python`; данные workflow — в `src/generated_workflow/workflow.json`.

## Установка и запуск

Из корня проекта выполните:

```powershell
uv sync
uv run run-workflow
```

## Ручная настройка

Перед запуском прочитайте `MANUAL_CHANGES.md`. Если там перечислены неподдержанные узлы или параметры, проект намеренно завершится с ошибкой до выполнения workflow. После реализации/удаления этих функций отметьте их как закрытые в `src/generated_workflow/manual_changes.py`.

Postgres и Data Table берут подключение из `DATABASE_URL`; Email использует SMTP-переменные `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` и `SMTP_USE_TLS`. Их сборка находится в `src/generated_workflow/services.py`. Не вставляйте секреты в `workflow.json`.

Для Webhook передайте payload через аргумент `initial_items` функции `run_workflow` в `src/generated_workflow/main.py`; проект сам не запускает постоянный HTTP-сервер.
"""

    @staticmethod
    def _render_manual_actions(actions: list[str]) -> str:
        """Сгенерировать явный список незакрытых ручных действий для fail-fast запуска."""
        return (
            '"""Неподдержанные возможности, которые нужно исправить вручную."""\n\n'
            f"REQUIRED_MANUAL_CHANGES = {tuple(actions)!r}\n"
        )

    @staticmethod
    def _render_manual_changes(
        workflow: Workflow,
        actions: list[str],
        redacted_fields: list[str],
        removed_credentials: list[str],
    ) -> str:
        """Собрать инструкцию по неподдержанным узлам, секретам и сервисам."""
        lines = [
            "# Ручная доработка проекта",
            "",
            f"Исходный workflow: **{workflow.name}**.",
            "",
            "## Неподдержанные узлы и параметры",
            "",
        ]
        if actions:
            lines.extend(f"- {action}" for action in actions)
            lines.extend(
                [
                    "",
                    "Откройте указанный файл runtime и добавьте обработчик с нужной семантикой. "
                    "Для нового типа/версии зарегистрируйте точную пару в "
                    "`src/n8n_to_python/converters/nodes.py`. После ручной реализации удалите "
                    "закрытое действие из `src/generated_workflow/manual_changes.py`.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "Реестр не обнаружил неподдержанных типов, версий или параметров.",
                    "",
                ]
            )

        lines.extend(["## Секреты и credentials", ""])
        if removed_credentials:
            lines.append("Ссылки n8n credentials удалены из `workflow.json`:")
            lines.extend(f"- {credential}" for credential in removed_credentials)
        else:
            lines.append("Ссылки n8n credentials не найдены или отсутствовали.")
        if redacted_fields:
            lines.extend(
                [
                    "",
                    "Значения этих параметров заменены на `<SET_MANUALLY>` и должны быть "
                    "добавлены вручную после проверки:",
                ]
            )
            lines.extend(f"- `{field_path}`" for field_path in redacted_fields)
        lines.extend(
            [
                "",
                "Не добавляйте секреты в `workflow.json`. Для штатных integrations задайте "
                "переменные окружения; собственный способ подключения можно внести в "
                "`src/generated_workflow/services.py`.",
                "",
                "## Где расширять поведение",
                "",
                "- HTTP Request: `src/n8n_to_python/converters/http_request.py`, класс "
                "`HttpRequestParameters` и `HttpRequestExecutor`.",
                "- Data Table, Postgres, Email: `src/n8n_to_python/converters/integrations.py`; "
                "их runtime services собираются в `src/generated_workflow/services.py`.",
                "- Code, Edit Fields, triggers, IF/Switch, Merge: "
                "`src/n8n_to_python/converters/nodes.py` и соответствующие converter modules.",
                "- Workflow JSON: `src/generated_workflow/workflow.json`.",
                "",
                "После ручных изменений запустите проект командами из `README.md` и проверьте "
                "результат на своём workflow.",
                "",
            ]
        )
        return "\n".join(lines)


def _project_slug(workflow_name: str) -> str:
    """Получить безопасное имя uv-проекта из произвольного workflow name."""
    ascii_name = unicodedata.normalize("NFKD", workflow_name).encode("ascii", "ignore").decode()
    slug = "-".join(re.findall(r"[a-zA-Z0-9]+", ascii_name.lower()))
    return slug or "generated-n8n-workflow"


_SERVICES_SOURCE = '''"""Подключение внешних сервисов через переменные окружения."""

import os
from collections.abc import Mapping
from typing import Any

from n8n_to_python.infrastructure.database import create_postgres_engine_from_env
from n8n_to_python.infrastructure.email_sender import create_email_sender_from_env
from n8n_to_python.infrastructure.postgres_client import SQLAlchemyPostgresClient
from n8n_to_python.infrastructure.stores.sqlalchemy_data_table_store import (
    SQLAlchemyDataTableStore,
)


def build_services(
    node_types: set[str],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Собрать сервисы по переменным окружения и сохранить внедрённые адаптеры."""
    services: dict[str, Any] = dict(overrides or {})
    needs_postgres = (
        "n8n-nodes-base.postgres" in node_types and "postgres_client" not in services
    )
    needs_data_table = (
        "n8n-nodes-base.dataTable" in node_types and "data_table_store" not in services
    )
    if "n8n-nodes-base.emailSend" in node_types and "email_sender" not in services:
        if os.environ.get("SMTP_HOST"):
            services["email_sender"] = create_email_sender_from_env()
    if needs_postgres or needs_data_table:
        engine = create_postgres_engine_from_env()
        if "n8n-nodes-base.postgres" in node_types:
            services.setdefault("postgres_client", SQLAlchemyPostgresClient(engine))
        if "n8n-nodes-base.dataTable" in node_types:
            services.setdefault("data_table_store", SQLAlchemyDataTableStore(engine))
        services["_database_engine"] = engine
    return services


async def close_services(services: dict[str, Any]) -> None:
    """Закрыть созданный SQLAlchemy engine после выполнения workflow."""
    engine = services.get("_database_engine")
    if engine is not None:
        await engine.dispose()
'''

_MAIN_SOURCE = '''"""Запуск сгенерированного Python Core workflow."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.engine import WorkflowExecutionEngine
from n8n_to_python.runtime.models import ExecutionResult, WorkflowItem

from .manual_changes import REQUIRED_MANUAL_CHANGES
from .services import build_services, close_services


def load_workflow() -> Workflow:
    """Загрузить workflow, который лежит рядом с этим модулем."""
    workflow_path = Path(__file__).with_name("workflow.json")
    return Workflow.model_validate_json(workflow_path.read_bytes())


async def run_workflow(
    *,
    initial_items: Mapping[str, Sequence[WorkflowItem]] | None = None,
    services: Mapping[str, Any] | None = None,
) -> ExecutionResult:
    """Выполнить workflow и завершить собственные соединения с внешними сервисами."""
    if REQUIRED_MANUAL_CHANGES:
        details = "\\n".join(f"- {change}" for change in REQUIRED_MANUAL_CHANGES)
        raise RuntimeError(
            "Сначала выполните ручные доработки из MANUAL_CHANGES.md и обновите "
            "src/generated_workflow/manual_changes.py:\\n" + details
        )

    workflow = load_workflow()
    registry = default_node_converter_registry().build_runtime_registry(workflow)
    runtime_services = build_services({node.type for node in workflow.nodes}, services)
    try:
        return await WorkflowExecutionEngine(registry).run(
            workflow,
            initial_items=initial_items,
            services=runtime_services,
        )
    finally:
        await close_services(runtime_services)


def main() -> None:
    """Запустить workflow и вывести terminal items в JSON."""
    result = asyncio.run(run_workflow())
    payload = [
        {
            "node": terminal.node_name,
            "output_index": terminal.output_index,
            "items": [item.model_dump(mode="json", by_alias=True) for item in terminal.items],
        }
        for terminal in result.terminal_outputs
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''
