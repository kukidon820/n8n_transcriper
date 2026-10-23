"""Pydantic-модели для структуры экспортированного workflow n8n."""

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class WorkflowNode(BaseModel):
    """Описывает один узел из массива `nodes` экспорта n8n.

    Дополнительные поля сохраняются, потому что формат n8n содержит
    метаданные, которые модель пока явно не описывает. В JSON версия узла
    называется `typeVersion`; в Python для неё используется `type_version`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=False)

    name: str
    type: str
    type_version: float = Field(alias="typeVersion")
    position: tuple[float, float]
    parameters: Dict[str, Any] = Field(default_factory=dict)


class WorkflowConnection(BaseModel):
    """Описывает одно ребро: от исходного узла к целевому узлу.

    `index` хранит индекс из экспорта n8n. Его точную семантику нужно
    подтверждать на fixtures конкретных типов соединений.
    """

    node: str
    type: str
    index: int = 0


class Workflow(BaseModel):
    """Описывает workflow целиком: имя, узлы, связи и дополнительные поля.

    Вложенная структура `connections` пока не типизирована до отдельных
    соединений; это ограничение следующего этапа доменной модели.
    """

    model_config = ConfigDict(extra="allow")
    name: str
    nodes: List[WorkflowNode]
    connections: dict[str, Any]
