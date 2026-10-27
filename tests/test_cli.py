"""Подтверждение команд Typer реальными локальными workflow и subprocess-вызовами."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def run_cli(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Запустить установленную точку входа как отдельный пользовательский процесс."""
    environment = os.environ.copy()
    project_src = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (project_src, environment.get("PYTHONPATH")))
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "n8n_to_python.cli.main", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


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
    assert "Этот тип узла пока не поддерживается." in result.stderr
    assert "Проверка не пройдена" in result.stderr


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
    assert "Этот тип узла пока не поддерживается." in result.stderr
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
