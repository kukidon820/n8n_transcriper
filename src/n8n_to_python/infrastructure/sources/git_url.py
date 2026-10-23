"""Адаптер для загрузки JSON workflow по HTTP(S)-ссылке."""

import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from pydantic import AnyHttpUrl, BaseModel, field_validator

logger = logging.getLogger(__name__)


class GitWorkflowSource(BaseModel):
    """Скачивает JSON из Git-хостинга и при необходимости сохраняет локально.

    Для GitHub принимается как raw-ссылка, так и URL страницы просмотра
    файла вида `/blob/`; последний преобразуется в ссылку на raw-содержимое.
    """

    url: AnyHttpUrl
    final_bytes: bytes | None = None
    output_directory: Path | None = None
    filename: str = "workflow.json"

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        """Запретить путь вместо имени файла и требовать суффикс `.json`."""
        if Path(value).name != value or Path(value).suffix.lower() != ".json":
            raise ValueError("Имя выходного файла должно быть именем файла с расширением .json")
        return value

    def _get_download_url(self) -> str:
        """Преобразовать GitHub `/blob/` URL в URL исходных байтов.

        Другие хосты и уже raw-ссылки возвращаются без изменений.
        """
        original_url = str(self.url)
        parsed_url = urlsplit(original_url)

        if parsed_url.hostname not in {"github.com", "www.github.com"}:
            return original_url

        path_parts = parsed_url.path.strip("/").split("/")
        if len(path_parts) < 5 or path_parts[2] != "blob":
            return original_url

        # В browser URL сегменты идут как owner/repository/blob/ref/file path.
        owner, repository, _, ref, *file_path = path_parts
        raw_path = "/" + "/".join((owner, repository, ref, *file_path))
        return urlunsplit(
            (parsed_url.scheme, "raw.githubusercontent.com", raw_path, parsed_url.query, "")
        )

    async def read(self) -> bytes:
        """Скачать JSON, вернуть его байты и сохранить, если задана папка.

        HTTP-ошибки записываются в лог вместе с исходной ссылкой и затем
        пробрасываются вызывающему коду.
        """
        download_url = self._get_download_url()
        if Path(urlsplit(download_url).path).suffix.lower() != ".json":
            raise ValueError("Ссылка должна вести на файл с расширением .json")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(download_url) as response:
                    response.raise_for_status()
                    content = await response.read()

                    self.final_bytes = content

                    if self.output_directory is not None:
                        target = self.output_directory / self.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                    return content

            except aiohttp.ClientError:
                # Логируем контекст загрузки, не подменяя исходное исключение.
                logger.exception("Не удалось скачать workflow по адресу %s", self.url)
                raise


if __name__ == "__main__":
    # Ручная проверка сетевого адаптера; этот URL не ведёт на fixture n8n.
    import asyncio

    git_workflow = GitWorkflowSource(
        url="https://github.com/kukidon820/BookNest-Flask-/blob/work_is_done/test.json",
        output_directory="downloads",
        filename="workflow.json",
    )
    content = asyncio.run(git_workflow.read())
