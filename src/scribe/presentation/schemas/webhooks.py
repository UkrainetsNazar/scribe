from typing import Self

from pydantic import BaseModel, Field

from scribe.application.use_cases.webhooks import WebhookConfig


class WebhookRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class WebhookResponse(BaseModel):
    url: str | None

    @classmethod
    def from_domain(cls, config: WebhookConfig) -> Self:
        return cls(url=config.url)


class WebhookSecretResponse(BaseModel):
    """The secret is shown only here: when the webhook is set or the secret is rotated."""

    url: str
    secret: str

    @classmethod
    def from_domain(cls, config: WebhookConfig) -> Self:
        return cls(url=config.url or "", secret=config.secret or "")
