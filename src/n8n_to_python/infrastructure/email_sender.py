"""Pydantic-настройки и aiosmtplib реализация EmailSender."""

import os
from email.message import EmailMessage as MimeEmailMessage
from email.utils import make_msgid

import aiosmtplib
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..application.models.email_delivery_result import EmailDeliveryResult, EmailEnvelope
from ..application.models.outgoing_email import OutgoingEmail


class SMTPSettings(BaseModel):
    """SMTP-конфигурация runtime; пароль не включается в представление модели."""

    model_config = ConfigDict(frozen=True, repr=False)

    host: str = Field(min_length=1)
    port: int = Field(default=587, gt=0, le=65535)
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    start_tls: bool = True
    use_tls: bool = False

    @model_validator(mode="after")
    def validate_tls_and_login(self) -> "SMTPSettings":
        """Не допускать конфликтующие TLS-режимы и неполную авторизацию."""
        if self.start_tls and self.use_tls:
            raise ValueError("SMTP_STARTTLS и SMTP_USE_TLS нельзя включать одновременно.")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP_USERNAME и SMTP_PASSWORD нужно задать вместе.")
        return self

    @classmethod
    def from_environment(cls) -> "SMTPSettings":
        """Прочитать SMTP_HOST/PORT/USERNAME/PASSWORD/TLS переменные окружения."""
        host = os.environ.get("SMTP_HOST")
        if not host:
            raise ValueError("Не задана переменная окружения SMTP_HOST.")
        return cls(
            host=host,
            port=os.environ.get("SMTP_PORT", "587"),
            username=os.environ.get("SMTP_USERNAME"),
            password=os.environ.get("SMTP_PASSWORD"),
            start_tls=os.environ.get("SMTP_STARTTLS", "true").lower() == "true",
            use_tls=os.environ.get("SMTP_USE_TLS", "false").lower() == "true",
        )


class AioSMTPEmailSender:
    """Отправить OutgoingEmail через асинхронный SMTP клиент."""

    def __init__(self, settings: SMTPSettings):
        """Сохранить проверенные env-настройки без логирования учётных данных."""
        self._settings = settings

    async def send(self, message: OutgoingEmail) -> EmailDeliveryResult:
        """Собрать MIME-сообщение и отправить его всем To/CC/BCC получателям."""
        mime_message = MimeEmailMessage()
        mime_message["From"] = message.from_email
        mime_message["To"] = ", ".join(message.to)
        mime_message["Subject"] = message.subject
        mime_message["Message-ID"] = make_msgid()
        if message.cc:
            mime_message["Cc"] = ", ".join(message.cc)
        if message.reply_to:
            mime_message["Reply-To"] = message.reply_to

        mime_message.set_content(message.text or "")
        if message.html is not None:
            mime_message.add_alternative(message.html, subtype="html")

        recipients = [*message.to, *message.cc, *message.bcc]
        refused_recipients, response = await aiosmtplib.send(
            mime_message,
            recipients=recipients,
            hostname=self._settings.host,
            port=self._settings.port,
            username=self._settings.username,
            password=self._settings.password,
            start_tls=self._settings.start_tls,
            use_tls=self._settings.use_tls,
        )
        rejected = tuple(refused_recipients)
        return EmailDeliveryResult(
            accepted=tuple(address for address in recipients if address not in rejected),
            rejected=rejected,
            response=response,
            envelope=EmailEnvelope(from_email=message.from_email, to=tuple(recipients)),
            message_id=str(mime_message["Message-ID"]),
        )


def create_email_sender_from_env() -> AioSMTPEmailSender:
    """Собрать EmailSender из проверенных SMTP-переменных окружения."""
    return AioSMTPEmailSender(SMTPSettings.from_environment())
