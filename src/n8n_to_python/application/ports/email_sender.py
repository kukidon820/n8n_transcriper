"""Порт отправки email, не привязанный к SMTP-библиотеке."""

from typing import Protocol

from ..models.outgoing_email import OutgoingEmail


class EmailSender(Protocol):
    """Отправитель, который Infrastructure подключает при сборке runtime."""

    async def send(self, message: OutgoingEmail) -> None:
        """Отправить подготовленное письмо или пробросить ошибку доставки."""
        ...
