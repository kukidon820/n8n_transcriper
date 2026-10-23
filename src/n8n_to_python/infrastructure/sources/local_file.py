"""Адаптер для асинхронного чтения локального JSON-файла workflow."""

import asyncio
from pathlib import Path

from pydantic import BaseModel, DirectoryPath, Field


class LocalFileWorkflowSource(BaseModel):
    """Читает JSON по абсолютному пути или относительно `search_dir`.

    Если `search_dir` не передан, относительный путь считается от текущей
    рабочей папки процесса. После чтения байты доступны в `final_bytes`.
    """

    path: Path
    search_dir: DirectoryPath = Field(default_factory=Path.cwd)
    final_bytes: bytes = None

    def resolve_path(self) -> Path:
        """Найти файл, проверить его существование и расширение `.json`."""
        if not self.path:
            raise FileNotFoundError("Укажите путь или имя файла!")

        requested = self.path.expanduser()

        if requested.is_absolute():
            candidate = requested
        else:
            candidate = self.search_dir / requested

        candidate = candidate.resolve()

        if not candidate.is_file():
            raise FileNotFoundError(f"Файл не найден: {candidate}")

        if candidate.suffix.lower() != ".json":
            raise ValueError(f"Ожидался JSON-файл: {candidate}")

        return candidate

    async def read(self) -> bytes:
        """Прочитать файл и вернуть его содержимое в виде байтов.

        `Path.read_bytes()` — синхронная операция, поэтому она выполняется
        в рабочем потоке и не блокирует цикл событий asyncio.
        """
        path = self.resolve_path()
        content = await asyncio.to_thread(path.read_bytes)
        if not content:
            raise ValueError("Произошла ошибка при чтения файла")
        self.final_bytes = content

        return self.final_bytes


if __name__ == "__main__":
    # Ручная проверка адаптера при запуске этого файла напрямую.
    loader = LocalFileWorkflowSource(
        path=r"C:\Users\pjsta\PycharmProjects\n8n-transcriper\Упоминания.json"
    )
    content = asyncio.run(loader.read())
    print(len(content))
