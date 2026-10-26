"""Проверенная модель исходящего SMTP-сообщения для Application-порта."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OutgoingEmail(BaseModel):
    """Секреты SMTP хранятся отдельно, а здесь остаются только данные письма."""

    model_config = ConfigDict(frozen=True)

    from_email: str = Field(min_length=1)
    to: tuple[str, ...] = Field(min_length=1)
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str | None = None
    subject: str
    text: str | None = None
    html: str | None = None

    @model_validator(mode="after")
    def require_body(self) -> "OutgoingEmail":
        """Письмо должно содержать хотя бы одну версию body."""
        if self.text is None and self.html is None:
            raise ValueError("Письмо должно содержать text или html.")
        return self
