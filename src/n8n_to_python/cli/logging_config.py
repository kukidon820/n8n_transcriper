"""Настройка общего вывода логов CLI в консоль и файл."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)
# Маркер позволяет при повторном запуске IDE заменить свои handlers,
# не добавляя дублирующие сообщения в консоль и файл.
_HANDLER_MARKER = "n8n_to_python_logging_handler"
_RUSSIAN_LEVEL_NAMES = {
    "DEBUG": "ОТЛАДКА",
    "INFO": "ИНФОРМАЦИЯ",
    "WARNING": "ПРЕДУПРЕЖДЕНИЕ",
    "ERROR": "ОШИБКА",
    "CRITICAL": "КРИТИЧЕСКАЯ ОШИБКА",
}


class RussianLevelFormatter(logging.Formatter):
    """Подставить русское название уровня, сохранив исходную запись для других handlers."""

    def format(self, record: logging.LogRecord) -> str:
        """Форматировать запись с локализованным уровнем."""
        original_level_name = record.levelname
        record.levelname = _RUSSIAN_LEVEL_NAMES.get(original_level_name, original_level_name)
        try:
            return super().format(record)
        finally:
            record.levelname = original_level_name


def configure_logging() -> Path:
    """Настроить пользовательский console-лог и подробный файл.

    В консоль идут сообщения уровня INFO и выше, в файл — DEBUG и выше.
    Возвращается путь к файлу лога, чтобы его можно было показать или проверить.
    """
    log_directory = Path.cwd() / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / "n8n-to-python.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers[:]:
        if getattr(handler, _HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()

    formatter = RussianLevelFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = RussianLevelFormatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    setattr(console_handler, _HANDLER_MARKER, True)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logger.debug("Логирование настроено; файл: %s", log_file)
    return log_file
