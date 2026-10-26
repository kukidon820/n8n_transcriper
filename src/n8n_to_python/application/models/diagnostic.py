"""Pydantic-контракты для ошибок и предупреждений анализа workflow."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowDiagnostic(BaseModel):
    """Одно понятное замечание по структуре или поддержке узла."""

    model_config = ConfigDict(frozen=True)

    severity: Literal["error", "warning"]
    code: str
    message: str
    node_id: str | None = None
    node_name: str | None = None
    node_type: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Итог проверки workflow после успешной загрузки доменной модели."""

    model_config = ConfigDict(frozen=True)

    workflow_name: str
    node_count: int
    valid: bool
    diagnostics: tuple[WorkflowDiagnostic, ...]
