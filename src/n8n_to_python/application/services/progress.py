"""События прогресса и контекстный менеджер этапов Application."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

ProgressStatus = Literal["started", "succeeded", "failed"]


class ProgressEvent(BaseModel):
    """Типизированное событие о начале или завершении этапа."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: ProgressStatus
    duration_seconds: float | None = Field(default=None, ge=0)
    error_type: str | None = None


class ProgressCallback(Protocol):
    """Синхронный обработчик события прогресса, например CLI-логгер."""

    def __call__(self, event: ProgressEvent) -> None:
        """Отобразить или сохранить одно событие прогресса."""
        ...


class ProgressReporter:
    """Отправляет события и предоставляет `with`-контекст для этапов."""

    def __init__(self, callback: ProgressCallback | Callable[[ProgressEvent], None] | None = None):
        """Сохраняет callback; если его нет, события просто не выводятся."""
        self._callback = callback

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Сообщить о начале, успехе или ошибке этапа и пробросить исходную ошибку."""
        self._emit(ProgressEvent(stage=name, status="started"))
        started_at = perf_counter()

        try:
            yield
        except BaseException as error:
            event = ProgressEvent(
                stage=name,
                status="failed",
                duration_seconds=perf_counter() - started_at,
                error_type=type(error).__name__,
            )
            try:
                self._emit(event)
            except BaseException as callback_error:
                error.add_note(
                    "Callback прогресса также завершился ошибкой "
                    f"{type(callback_error).__name__}: {callback_error}"
                )
            raise
        else:
            self._emit(
                ProgressEvent(
                    stage=name,
                    status="succeeded",
                    duration_seconds=perf_counter() - started_at,
                )
            )

    def _emit(self, event: ProgressEvent) -> None:
        """Передать событие зарегистрированному callback."""
        if self._callback is not None:
            self._callback(event)
