"""Подтверждение команд Typer реальными локальными workflow и subprocess-вызовами."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from n8n_to_python.application.models.diagnostic import WorkflowDiagnostic
from n8n_to_python.application.ports.workflow_generator import GenerationBackend
from n8n_to_python.cli.main import _brief_diagnostic_message, _display_diagnostics

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def run_cli(
    *arguments: str,
    cwd: Path,
    python_io_encoding: str = "utf-8",
    capture_encoding: str = "utf-8",
) -> subprocess.CompletedProcess[str]:
    """Запустить установленную точку входа как отдельный пользовательский процесс."""
    environment = os.environ.copy()
    project_src = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (project_src, environment.get("PYTHONPATH")))
    )
    environment["PYTHONIOENCODING"] = python_io_encoding
    return subprocess.run(
        [sys.executable, "-m", "n8n_to_python.cli.main", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding=capture_encoding,
        timeout=60,
        check=False,
    )


def test_cli_replaces_symbols_missing_from_legacy_windows_code_page(tmp_path: Path) -> None:
    """Validate a workflow with an arrow in its title under a strict cp1251 console."""
    workflow = json.loads((FIXTURES / "converters_basic.json").read_text(encoding="utf-8"))
    workflow["name"] = "Workflow → with Unicode title"
    source = tmp_path / "unicode-title.json"
    source.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")

    result = run_cli(
        "validate",
        str(source),
        cwd=tmp_path,
        python_io_encoding="cp1251:strict",
        capture_encoding="cp1251",
    )

    assert result.returncode == 0, result.stderr
    assert "Workflow ? with Unicode title" in result.stdout
    assert "UnicodeEncodeError" not in result.stderr


def test_cli_help_lists_the_three_workflow_commands(tmp_path: Path) -> None:
    """Проверить доступную точку входа и команды без чтения workflow."""
    result = run_cli("--help", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "validate" in result.stdout
    assert "inspect" in result.stdout
    assert "convert" in result.stdout


def test_cli_convert_help_describes_output_backend_and_strict_options(tmp_path: Path) -> None:
    """Показать обязательные параметры генерации через `convert --help`."""
    result = run_cli("convert", "--help", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "--output" in result.stdout
    assert "--backend" in result.stdout
    assert "python-core" in result.stdout
    assert "langgraph" in result.stdout
    assert "--project-name" in result.stdout
    assert "--strict" in result.stdout


def test_cli_validate_help_explains_python_core_check_and_langgraph_conversion(
    tmp_path: Path,
) -> None:
    """Explain which generation mode validate checks and how to select LangGraph."""
    result = run_cli("validate", "--help", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    help_text = " ".join(result.stdout.replace("│", " ").split())
    assert "поддержку узлов" in help_text
    assert "обычный Python" in help_text
    assert "--backend langgraph" in help_text


def test_cli_convert_help_explains_generation_modes_in_russian(tmp_path: Path) -> None:
    """Describe backend choices as generation modes in the user's language."""
    result = run_cli("convert", "--help", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    help_text = " ".join(result.stdout.replace("│", " ").split())
    assert "Способ генерации проекта" in help_text
    assert "обычный Python" in help_text
    assert "библиотеки LangGraph" in help_text


def test_cli_explains_that_node_names_do_not_control_support() -> None:
    """Показать реальный type/version и объяснить, почему переименование не поможет."""
    diagnostic = WorkflowDiagnostic(
        severity="warning",
        code="UNSUPPORTED_NODE_TYPE",
        message="Узел 'CSV - Create file' типа 'n8n-nodes-base.convertToFile' версии 1.1 не поддержан.",
        node_name="CSV - Create file",
        node_type="n8n-nodes-base.convertToFile",
        context={"type_version": 1.1},
    )

    message = _brief_diagnostic_message(diagnostic)

    assert "n8n-nodes-base.convertToFile" in message
    assert "1.1" in message
    assert "переименование узла" in message


def test_langgraph_convert_does_not_show_python_core_ai_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Do not print Python Core-only AI warnings after LangGraph generation succeeds."""
    diagnostic = WorkflowDiagnostic(
        severity="warning",
        code="UNSUPPORTED_NODE_TYPE",
        message="AI Agent is not supported by Python Core.",
        node_name="AI Agent",
        node_type="@n8n/n8n-nodes-langchain.agent",
    )

    _display_diagnostics((diagnostic,), backend=GenerationBackend.LANGGRAPH)

    assert capsys.readouterr().err == ""


def test_cli_explains_supported_node_versions() -> None:
    """Перечислить поддержанные версии и версию, которой нет в адаптере."""
    diagnostic = WorkflowDiagnostic(
        severity="warning",
        code="UNSUPPORTED_NODE_VERSION",
        message="Узел 'HTTP' типа 'n8n-nodes-base.httpRequest' версии 4.2 не поддержан. "
        "Поддержанные версии: [1.0, 4.1].",
        node_name="HTTP",
        node_type="n8n-nodes-base.httpRequest",
        context={"type_version": 4.2},
    )

    message = _brief_diagnostic_message(diagnostic)

    assert "[1.0, 4.1]" in message
    assert "4.2" in message
    assert "имя узла" in message


def test_cli_translates_unexpected_node_parameters_into_russian() -> None:
    """Назвать конкретные поля и объяснить, что именно не прошло проверку."""
    diagnostic = WorkflowDiagnostic(
        severity="error",
        code="INVALID_NODE_CONFIGURATION",
        message=(
            "Узел 'Example' (n8n-nodes-base.example v1) имеет неподдерживаемые параметры: "
            "options.futureFlag: Extra inputs are not permitted; resource: Field required; "
            "name: Input should be a valid string"
        ),
        node_name="Example",
        node_type="n8n-nodes-base.example",
    )

    message = _brief_diagnostic_message(diagnostic)

    assert "options.futureFlag" in message
    assert "не входит в настройки" in message
    assert "обязательное поле отсутствует" in message
    assert "ожидается строка" in message
    assert "Extra inputs are not permitted" not in message
    assert "valid string" not in message


def test_cli_missing_file_reports_path_without_traceback(tmp_path: Path) -> None:
    """Коротко назвать отсутствующий файл и не печатать пользователю traceback."""
    source = tmp_path / "missing-workflow.json"

    result = run_cli("validate", str(source), cwd=tmp_path)

    assert result.returncode == 1
    assert "missing-workflow.json" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_validate_reads_a_real_fixture_and_reports_progress(tmp_path: Path) -> None:
    """Проверить успешную CLI-команду и короткий русский статус на настоящем JSON."""
    result = run_cli("validate", str(FIXTURES / "converters_basic.json"), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Проверяю сценарий..." in result.stderr
    assert "workflow:validate" not in result.stderr
    assert "Узлов: 8" in result.stdout


def test_installed_console_script_validates_a_real_fixture(tmp_path: Path) -> None:
    """Проверить entry point из pyproject после установки проекта через uv."""
    executable_name = "n8n-to-python.exe" if os.name == "nt" else "n8n-to-python"
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), "Установите entry point командой `uv sync --locked`."

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [str(executable), "validate", str(FIXTURES / "converters_basic.json")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "converter basic nodes" in result.stdout
    assert "Узлов: 8" in result.stdout


def test_cli_inspect_shows_graph_without_node_parameters(tmp_path: Path) -> None:
    """Проверить безопасное описание узлов и отсутствие вывода параметров."""
    result = run_cli("inspect", str(FIXTURES / "converters_basic.json"), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Сценарий: converter basic nodes" in result.stdout
    assert "Manual" in result.stdout
    assert "связей: 7" in result.stdout
    assert '"parameters"' not in result.stdout


@pytest.mark.parametrize("backend", ["python-core", "langgraph"])
def test_cli_convert_writes_a_named_standalone_project(
    tmp_path: Path,
    backend: str,
) -> None:
    """Сгенерировать реальный проект каждым backend в новую папку."""
    output = tmp_path / f"generated-{backend}"
    result = run_cli(
        "convert",
        str(FIXTURES / "converters_basic.json"),
        "--output",
        str(output),
        "--backend",
        backend,
        "--project-name",
        "My Test Project",
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (output / "src" / "generated_workflow" / "main.py").is_file()
    assert (output / "src" / "n8n_to_python" / "runtime" / "engine.py").is_file()
    assert "Создан проект" in result.stdout
    manifest = tomllib.loads((output / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["name"] == "my-test-project"
    assert (output / "README.md").read_text(encoding="utf-8").startswith("# My Test Project")


def test_cli_strict_validation_fails_for_unsupported_nodes(tmp_path: Path) -> None:
    """Возвращать ненулевой exit code, когда strict повышает warnings до errors."""
    result = run_cli("validate", str(FIXTURES / "workflow_test.json"), "--strict", cwd=tmp_path)

    assert result.returncode == 1
    assert "Python Core не выполняет AI tools" in result.stderr
    assert "Calculator Tool" in result.stderr
    assert "Для Simple Memory используйте convert --backend langgraph" in result.stderr
    assert "Проверка не пройдена" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_convert_strict_stops_before_writing_unsupported_workflow(tmp_path: Path) -> None:
    """Не создавать результат, если strict-проверка находит неподдерживаемые узлы."""
    output = tmp_path / "strict-output"
    result = run_cli(
        "convert",
        str(FIXTURES / "workflow_test.json"),
        "--output",
        str(output),
        "--strict",
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "Python Core не выполняет AI tools" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_cli_convert_refuses_a_nonempty_output_directory(tmp_path: Path) -> None:
    """Остановить генерацию до изменения уже существующего пользовательского файла."""
    output = tmp_path / "occupied"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("оставить", encoding="utf-8")

    result = run_cli(
        "convert",
        str(FIXTURES / "converters_basic.json"),
        "--output",
        str(output),
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "Папка результата не пуста" in result.stderr
    assert marker.read_text(encoding="utf-8") == "оставить"
