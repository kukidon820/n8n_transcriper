"""Generate a standalone project whose n8n execution graph uses LangGraph."""

from ..application.ports.workflow_generator import GenerationBackend
from ..domain.models.workflow import Workflow
from ..runtime.langgraph_agent import (
    AI_AGENT_TYPE,
    LangGraphAIAgentExecutor,
    UnsupportedAIAgentConfigurationError,
)
from .python_core import _EXTENSION_FILES, PythonCoreGenerator


class LangGraphGenerator(PythonCoreGenerator):
    """Build an independent project that uses LangGraph for workflow and AI-agent flow."""

    backend = GenerationBackend.LANGGRAPH

    def _inspect_workflow(self, workflow: Workflow) -> list[str]:
        """Validate supported AI connections and report every other gap explicitly."""
        actions: list[str] = []
        supported_agent_names: set[str] = set()
        auxiliary_names: set[str] = set()
        for node in workflow.nodes:
            if node.type != AI_AGENT_TYPE:
                continue
            try:
                executor = LangGraphAIAgentExecutor.from_workflow(workflow, node)
            except UnsupportedAIAgentConfigurationError as error:
                actions.append(
                    f"AI Agent {node.name!r} ({node.type}, typeVersion {node.type_version}): "
                    f"{error} Точка расширения: `src/n8n_to_python/runtime/langgraph_agent.py`."
                )
            else:
                supported_agent_names.add(node.name)
                auxiliary_names.update(executor.auxiliary_node_names)

        for node in workflow.nodes:
            if node.name in supported_agent_names or node.name in auxiliary_names:
                continue
            for diagnostic in self._converter_registry.inspect_node(node):
                extension_file = _EXTENSION_FILES.get(
                    node.type,
                    "src/n8n_to_python/runtime/langgraph_engine.py",
                )
                actions.append(
                    f"Узел {node.name!r} ({node.type}, typeVersion {node.type_version}): "
                    f"{diagnostic.message} Точка расширения: `{extension_file}`."
                )
        return actions

    @staticmethod
    def _render_pyproject(workflow_name: str) -> str:
        """Add only the LangGraph dependency to the generated backend manifest."""
        return (
            PythonCoreGenerator._render_pyproject(workflow_name)
            .replace(
                'description = "Python Core runtime generated from an n8n workflow"',
                'description = "LangGraph runtime generated from an n8n workflow"',
            )
            .replace(
                '    "pydantic>=2.0",',
                '    "langgraph>=1.0,<2",\n    "pydantic>=2.0",',
            )
        )

    @staticmethod
    def _render_readme(workflow_name: str) -> str:
        """Describe the LangGraph entry point and manual DeepSeek configuration."""
        return f"""# {workflow_name}

Автономный проект LangGraph, созданный из n8n workflow. Runtime находится в `src/n8n_to_python`, данные workflow — в `src/generated_workflow/workflow.json`.

## Установка и запуск

Из корня проекта выполните:

```powershell
uv sync
uv run run-workflow
```

Перед запуском прочитайте `MANUAL_CHANGES.md`. Неподдержанные узлы и настройки намеренно останавливают выполнение, пока вы не добавите нужную реализацию и не закроете действие в `src/generated_workflow/manual_changes.py`.

## AI Agent: DeepSeek Chat Model v1

Для подключённой пары AI Agent v2.2 + DeepSeek Chat Model v1 задайте API key в окружении `DEEPSEEK_API_KEY`. По умолчанию используется `https://api.deepseek.com` и модель `deepseek-flash`; при необходимости задайте `DEEPSEEK_BASE_URL` и `DEEPSEEK_MODEL`. Запросы отправляются через `aiohttp`. API key не записывается в файлы проекта.

Поддержанный Calculator Tool вычисляет только числовые выражения с арифметическими операциями. Simple Memory v1.3 хранит последние сообщения в памяти одного запуска Python-процесса по `sessionKey`; долговременное хранилище не создаётся.

Postgres и Data Table берут подключение из `DATABASE_URL`; Email использует `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` и `SMTP_USE_TLS`. Настройки сборки находятся в `src/generated_workflow/services.py`.

Webhook не запускает постоянный сервер. Передайте входные данные через `initial_items` функции `run_workflow` в `src/generated_workflow/main.py`.
"""

    @staticmethod
    def _render_main_source() -> str:
        """Return a generated entry point wired to the LangGraph runtime."""
        return _LANGGRAPH_MAIN_SOURCE


_LANGGRAPH_MAIN_SOURCE = '''"""Запуск сгенерированного LangGraph workflow."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from n8n_to_python.converters.nodes import default_node_converter_registry
from n8n_to_python.domain.models.workflow import Workflow
from n8n_to_python.runtime.langgraph_agent import build_langgraph_ai_executors
from n8n_to_python.runtime.langgraph_engine import LangGraphExecutionEngine
from n8n_to_python.runtime.models import ExecutionResult, WorkflowItem

from .manual_changes import REQUIRED_MANUAL_CHANGES
from .services import build_services, close_services


def load_workflow() -> Workflow:
    """Загрузить workflow, сохранённый рядом с этим модулем."""
    workflow_path = Path(__file__).with_name("workflow.json")
    return Workflow.model_validate_json(workflow_path.read_bytes())


async def run_workflow(
    *,
    initial_items: Mapping[str, Sequence[WorkflowItem]] | None = None,
    services: Mapping[str, Any] | None = None,
) -> ExecutionResult:
    """Запустить LangGraph и закрыть внешние сервисы после выполнения."""
    if REQUIRED_MANUAL_CHANGES:
        details = "\\n".join(f"- {change}" for change in REQUIRED_MANUAL_CHANGES)
        raise RuntimeError(
            "Сначала выполните ручные доработки из MANUAL_CHANGES.md и обновите "
            "src/generated_workflow/manual_changes.py:\\n" + details
        )

    workflow = load_workflow()
    ai_executors, auxiliary_names = build_langgraph_ai_executors(workflow)
    registry = default_node_converter_registry().build_runtime_registry(
        workflow,
        node_overrides=ai_executors,
        ignored_node_names=auxiliary_names,
    )
    runtime_services = build_services({node.type for node in workflow.nodes})
    if services:
        runtime_services.update(services)
    try:
        return await LangGraphExecutionEngine(registry).run(
            workflow,
            initial_items=initial_items,
            services=runtime_services,
        )
    finally:
        await close_services(runtime_services)


def main() -> None:
    """Запустить workflow и вывести конечные items в JSON."""
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
