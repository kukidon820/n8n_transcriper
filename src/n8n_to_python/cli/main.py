"""Точка входа Typer CLI."""

import logging

import typer

from n8n_to_python.cli.logging_config import configure_logging

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="n8n-to-python",
    help="Преобразование n8n workflow в Python-проект.",
    no_args_is_help=True,
)


@app.callback()
def configure_cli() -> None:
    """Сообщить пользователю о запуске CLI."""
    logger.info("Конвертер n8n workflow запущен")


def main() -> None:
    """Настроить логи и запустить Typer CLI из консоли или IDE."""
    configure_logging()
    try:
        app()
    except Exception as error:
        logger.error("Не удалось выполнить команду: %s", error)
        logger.debug("Подробная информация об ошибке", exc_info=True)
        # CLI возвращает ненулевой код, чтобы оболочка видела неуспешный запуск.
        raise typer.Exit(code=1)


if __name__ == "__main__":
    main()
