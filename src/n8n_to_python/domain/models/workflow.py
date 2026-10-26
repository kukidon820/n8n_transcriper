"""Pydantic-модели для структуры экспортированного workflow n8n."""

from collections import Counter
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowNode(BaseModel):
    """Описывает один узел из массива `nodes` экспорта n8n.

    Дополнительные поля сохраняются, потому что формат n8n содержит
    метаданные, которые модель пока явно не описывает. В JSON версия узла
    называется `typeVersion`; в Python для неё используется `type_version`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=False)

    id: str | None = None
    name: str
    type: str
    type_version: float = Field(alias="typeVersion")
    position: tuple[float, float]
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkflowConnection(BaseModel):
    """Описывает одно ребро: от исходного узла к целевому узлу.

    `type` задаёт тип входа целевого узла, а `index` — его индекс для этого
    типа. Верхнюю границу индекса нельзя определить без схемы входов узла.
    """

    model_config = ConfigDict(extra="allow")

    node: str
    type: str
    index: int = Field(ge=0, strict=True)


# Слот выхода может быть null, если у узла этот выход не подключён.
OutputConnections = list[WorkflowConnection] | None

# Список выходов узла: например, два выхода IF или три выхода Switch.
NodeOutputs = list[OutputConnections]

# Тип соединения (например, main) и выходы этого типа.
ConnectionTypes = dict[str, NodeOutputs]

# Имя исходного узла и его типы соединений.
WorkflowConnections = dict[str, ConnectionTypes]


class Workflow(BaseModel):
    """Описывает workflow целиком: имя, узлы, связи и дополнительные поля.

    В `connections` имя исходного узла и тип порта ведут к выходам, которые
    содержат типизированные объекты `WorkflowConnection`. Названия портов
    остаются строками, поэтому подходят и обычные, и AI-соединения n8n.
    Проверка целостности также гарантирует уникальные имена/ID и существование
    узлов-источников и целей. Циклы графа разрешены.
    """

    model_config = ConfigDict(extra="allow")
    name: str
    nodes: list[WorkflowNode]
    connections: WorkflowConnections

    @model_validator(mode="after")
    def validate_graph_integrity(self) -> Self:
        """Проверить уникальность узлов и ссылки из `connections`."""
        name_counts = Counter(node.name for node in self.nodes)
        duplicate_names = sorted(name for name, count in name_counts.items() if count > 1)
        if duplicate_names:
            raise ValueError(f"Имена узлов должны быть уникальными: {duplicate_names}")

        id_counts = Counter(node.id for node in self.nodes if node.id is not None)
        duplicate_ids = sorted(node_id for node_id, count in id_counts.items() if count > 1)
        if duplicate_ids:
            raise ValueError(f"ID узлов должны быть уникальными: {duplicate_ids}")

        node_names = set(name_counts)
        for source_name, connection_types in self.connections.items():
            if source_name not in node_names:
                raise ValueError(f"Связи ссылаются на отсутствующий исходный узел: {source_name!r}")

            for outputs in connection_types.values():
                for output in outputs:
                    # null обозначает незаполненный выход; проверять в нём нечего.
                    if output is None:
                        continue
                    for connection in output:
                        if connection.node not in node_names:
                            raise ValueError(
                                f"Узел {source_name!r} связан с отсутствующей целью "
                                f"{connection.node!r}"
                            )

        return self
