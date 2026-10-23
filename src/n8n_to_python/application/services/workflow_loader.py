from pydantic import BaseModel
from ..ports.workflow_source import WorkflowSource
from ...domain.models.workflow import Workflow


class WorkflowLoader:

    def __init__(self, source: WorkflowSource):
        self.source = source

    async def load(self) -> Workflow:
        raw_bytes  = await self.source.read()
        return Workflow.model_validate_json(raw_bytes)