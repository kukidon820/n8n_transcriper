"""Проверки безопасного сохранения сгенерированных файлов на локальном диске."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from n8n_to_python.application.models.generated_project import GeneratedProject
from n8n_to_python.application.services.progress import ProgressReporter
from n8n_to_python.infrastructure.project_writer import GeneratedProjectFileWriter, ProjectFile


@pytest.mark.asyncio
async def test_writer_creates_project_files_in_a_new_or_empty_directory(tmp_path: Path) -> None:
    """Создать вложенные файлы с UTF-8 текстом и не блокировать async вызывающий код."""
    output = tmp_path / "generated"
    empty_directory = tmp_path / "already-empty"
    empty_directory.mkdir()
    writer = GeneratedProjectFileWriter()
    project = GeneratedProject(
        files={
            "pyproject.toml": 'name = "sample"\n',
            "src/package/main.py": 'print("привет")\n',
        }
    )

    await writer.write(project, output, progress=ProgressReporter())
    await writer.write(
        GeneratedProject(files={"main.py": "pass\n"}),
        empty_directory,
        progress=ProgressReporter(),
    )

    assert (output / "pyproject.toml").read_text(encoding="utf-8") == 'name = "sample"\n'
    assert (output / "src" / "package" / "main.py").read_text(encoding="utf-8") == (
        'print("привет")\n'
    )
    assert (empty_directory / "main.py").is_file()


@pytest.mark.asyncio
async def test_writer_refuses_nonempty_output_without_changing_existing_files(
    tmp_path: Path,
) -> None:
    """Не перезаписывать уже существующее содержимое результата."""
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "user.txt"
    marker.write_text("original", encoding="utf-8")

    with pytest.raises(FileExistsError, match="не пуста"):
        await GeneratedProjectFileWriter().write(
            GeneratedProject(files={"generated.py": "pass\n"}),
            output,
            progress=ProgressReporter(),
        )

    assert marker.read_text(encoding="utf-8") == "original"
    assert not (output / "generated.py").exists()


@pytest.mark.parametrize("unsafe_path", ["../outside.py", "/absolute.py", "C:/outside.py"])
def test_project_file_rejects_absolute_and_parent_paths(unsafe_path: str) -> None:
    """Не разрешать generated-project файлам выходить за output directory."""
    with pytest.raises(ValidationError, match="относительным путём"):
        ProjectFile(path=unsafe_path, content="unsafe")


@pytest.mark.asyncio
async def test_writer_validates_all_paths_before_creating_the_output_directory(
    tmp_path: Path,
) -> None:
    """Не оставлять даже пустую папку, если project содержит path traversal."""
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValidationError):
        await GeneratedProjectFileWriter().write(
            GeneratedProject(files={"safe.py": "pass\n", "../outside.py": "unsafe"}),
            output,
            progress=ProgressReporter(),
        )

    assert not output.exists()


@pytest.mark.asyncio
async def test_writer_rejects_file_and_directory_path_conflicts_before_writing(
    tmp_path: Path,
) -> None:
    """Проверить структуру проекта до начала записи и не оставить частичный результат."""
    output = tmp_path / "conflicted"

    with pytest.raises(ValueError, match="конфликтует"):
        await GeneratedProjectFileWriter().write(
            GeneratedProject(files={"package": "file", "package/main.py": "code"}),
            output,
            progress=ProgressReporter(),
        )

    assert not output.exists()
