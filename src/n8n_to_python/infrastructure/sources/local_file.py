from pathlib import Path
from pydantic import BaseModel, DirectoryPath, Field
import asyncio

class LocalFileWorkflowSource(BaseModel):
    path: Path
    search_dir: DirectoryPath = Field(default_factory=Path.cwd)
    final_bytes: bytes = None


    def resolve_path(self) -> Path:
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
        path = self.resolve_path()
        content = await asyncio.to_thread(path.read_bytes)
        if not content:
            raise ValueError("Произошла ошибка при чтения файла")
        self.final_bytes = content

        return self.final_bytes



if __name__ == "__main__":
    loader = LocalFileWorkflowSource(path=r"C:\Users\pjsta\PycharmProjects\n8n-transcriper\Упоминания.json")
    content = asyncio.run(loader.read())
    print(len(content))