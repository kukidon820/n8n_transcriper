"""Validated delivery receipt returned after sending one n8n Email item."""

from pydantic import BaseModel, ConfigDict, Field


class EmailEnvelope(BaseModel):
    """SMTP envelope sent to the remote mail server."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_email: str = Field(alias="from")
    to: tuple[str, ...]


class EmailDeliveryResult(BaseModel):
    """Serializable subset of the delivery information returned by n8n Email."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    accepted: tuple[str, ...]
    rejected: tuple[str, ...] = ()
    response: str
    envelope: EmailEnvelope
    message_id: str = Field(alias="messageId")
