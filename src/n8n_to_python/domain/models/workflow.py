from pydantic import BaseModel, ConfigDict, Field
from typing import List, Dict, Any

class WorkflowNode(BaseModel):
    model_config  = ConfigDict(extra="allow", populate_by_name=False)

    name: str
    type: str
    type_version: float = Field(alias="typeVersion")
    position: tuple[float, float]
    parameters: Dict[str, Any] = Field(default_factory=dict)

class WorkflowConnection(BaseModel):
    node: str
    type: str
    index: int = 0


class Workflow(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    nodes: List[WorkflowNode]
    connections: dict[str, Any]


