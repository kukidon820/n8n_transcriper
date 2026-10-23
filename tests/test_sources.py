"""Проверки локального и Git-источников workflow без подмены зависимостей."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from n8n_to_python.infrastructure.sources.git_url import GitWorkflowSource
from n8n_to_python.infrastructure.sources.local_file import LocalFileWorkflowSource

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"
# Это реальный сетевой интеграционный тест; нужны интернет и доступный GitHub.
# Ответ проверяется как скачанный файл, а не как валидный экспорт n8n.
GITHUB_BLOB_URL = "https://github.com/kukidon820/BookNest-Flask-/blob/work_is_done/test.json"


@pytest.mark.asyncio
async def test_local_source_reads_file_bytes() -> None:
    """Проверить, что источник возвращает точные байты локальной фикстуры."""
    workflow_path = FIXTURES_DIRECTORY / "workflow.json"
    expected = workflow_path.read_bytes()
    source = LocalFileWorkflowSource(
        path=Path("workflow.json"),
        search_dir=FIXTURES_DIRECTORY,
    )

    result = await source.read()

    assert result == expected
    assert source.final_bytes == expected


@pytest.mark.asyncio
async def test_local_source_rejects_empty_file(tmp_path: Path) -> None:
    """Проверить отказ источника от пустого JSON-файла."""
    empty_file = tmp_path / "empty.json"
    empty_file.write_bytes(b"")
    source = LocalFileWorkflowSource(path=empty_file)

    with pytest.raises(ValueError):
        await source.read()


@pytest.mark.asyncio
async def test_local_source_rejects_non_json_file() -> None:
    """Проверить, что расширение, отличное от `.json`, отклоняется."""
    source = LocalFileWorkflowSource(path=FIXTURES_DIRECTORY / "not_json.txt")

    with pytest.raises(ValueError):
        await source.read()


@pytest.mark.asyncio
async def test_local_source_rejects_missing_file() -> None:
    """Проверить ошибку, если относительный путь не найден в `search_dir`."""
    source = LocalFileWorkflowSource(
        path=Path("missing.json"),
        search_dir=FIXTURES_DIRECTORY,
    )

    with pytest.raises(FileNotFoundError):
        await source.read()


def test_github_blob_url_is_converted_to_raw_url() -> None:
    """Проверить преобразование GitHub-страницы просмотра в raw-ссылку."""
    source = GitWorkflowSource(url=GITHUB_BLOB_URL)

    assert source._get_download_url() == (
        "https://raw.githubusercontent.com/kukidon820/BookNest-Flask-/work_is_done/test.json"
    )


def test_raw_github_url_is_left_unchanged() -> None:
    """Проверить, что готовая ссылка на raw-файл не меняется."""
    url = "https://raw.githubusercontent.com/example/repository/main/workflow.json"
    source = GitWorkflowSource(url=url)

    assert source._get_download_url() == url


def test_git_source_rejects_non_json_output_filename() -> None:
    """Проверить Pydantic-валидацию имени сохраняемого файла."""
    with pytest.raises(ValidationError):
        GitWorkflowSource(url=GITHUB_BLOB_URL, filename="workflow.txt")


@pytest.mark.asyncio
async def test_git_source_rejects_non_json_url() -> None:
    """Проверить, что URL с расширением не `.json` отклоняется до запроса."""
    source = GitWorkflowSource(url="https://example.com/workflow.txt")

    with pytest.raises(ValueError):
        await source.read()


@pytest.mark.asyncio
async def test_git_source_downloads_and_saves_real_github_response(
    tmp_path: Path,
) -> None:
    """Скачать реальный ответ GitHub и сравнить его с сохранёнными байтами.

    Тест требует сети и подтверждает HTTP-загрузку/сохранение, но не проверяет
    ответ через Pydantic как n8n workflow.
    """
    source = GitWorkflowSource(
        url=GITHUB_BLOB_URL,
        output_directory=tmp_path,
        filename="workflow.json",
    )

    content = await source.read()

    saved_file = tmp_path / "workflow.json"
    assert content
    assert saved_file.read_bytes() == content
