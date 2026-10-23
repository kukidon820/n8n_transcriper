from typing import Protocol

class WorkflowSource(Protocol):
    async def read(self) -> bytes:
        ...