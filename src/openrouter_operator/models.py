"""The OpenRouterKey custom-resource spec, as a Pydantic model (validated, typed)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResetInterval(StrEnum):
    """OpenRouter budget reset cadence (maps to the API's limit_reset)."""

    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class OpenRouterKeySpec(BaseModel):
    """`.spec` of an OpenRouterKey CR.

    A project declares the key it wants; the operator mints/maintains it on OpenRouter and
    writes the secret value into `secret_name`. Weekly reset is the default on purpose — it
    caps a runaway agent's blast radius to one budget window.

    Two shapes share this spec:

    - **Standing project key** (`ephemeral=false`, the default): one long-lived key per project
      with a recurring `budgetUSD` per `resetInterval`. This is the funding ceiling.
    - **Ephemeral session key** (`ephemeral=true`): a fresh, single-shot key minted per agent
      *session* with a HARD `budgetUSD` cap and **no reset window** (`limit_reset=null`), so one
      runaway session can never spend past its pre-flight estimate. Requires `session` (makes the
      key + secret unique so concurrent sessions don't collide / clobber the standing secret) and
      should carry `expiresAt` so the key self-destructs even if the CR is never deleted. A standing
      key's reset is a soft per-window cap; a session key is the real per-session breaker.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    project: str = Field(min_length=1)
    budget_usd: float = Field(gt=0, alias="budgetUSD")
    reset_interval: ResetInterval = Field(default=ResetInterval.weekly, alias="resetInterval")
    guardrail: str | None = None
    secret_name: str | None = Field(default=None, alias="secretName")
    ephemeral: bool = False
    session: str | None = Field(
        default=None,
        description="Unique session id (e.g. issue-42-round-1). Required when ephemeral.",
    )
    expires_at: datetime | None = Field(default=None, alias="expiresAt")

    @model_validator(mode="after")
    def _require_session_when_ephemeral(self) -> OpenRouterKeySpec:
        if self.ephemeral and not self.session:
            raise ValueError("ephemeral OpenRouterKey requires `session`")
        return self

    def effective_reset(self) -> ResetInterval | None:
        """Reset cadence to mint with — `None` (no reset) for ephemeral session keys, so their
        budget is a one-shot hard cap rather than a recurring window."""
        return None if self.ephemeral else self.reset_interval

    def target_secret_name(self) -> str:
        """k8s Secret to write the minted key into.

        Standing key → `<project>-openrouter`. Ephemeral → a per-session name so it never clobbers
        the shared standing secret. An explicit `secretName` always wins.
        """
        if self.secret_name:
            return self.secret_name
        if self.ephemeral:
            return f"{self.project}-session-{self.session}-openrouter"
        return f"{self.project}-openrouter"

    def key_name(self) -> str:
        """The OpenRouter-side key name (unique per session when ephemeral)."""
        if self.ephemeral:
            return f"{self.project}-session-{self.session}"
        return f"{self.project}-agent"
