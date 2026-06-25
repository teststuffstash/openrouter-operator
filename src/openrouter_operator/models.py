"""The OpenRouterKey custom-resource spec, as a Pydantic model (validated, typed)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    project: str = Field(min_length=1)
    budget_usd: float = Field(gt=0, alias="budgetUSD")
    reset_interval: ResetInterval = Field(default=ResetInterval.weekly, alias="resetInterval")
    guardrail: str | None = None
    secret_name: str | None = Field(default=None, alias="secretName")

    def target_secret_name(self) -> str:
        """k8s Secret to write the minted key into (defaults to `<project>-openrouter`)."""
        return self.secret_name or f"{self.project}-openrouter"

    def key_name(self) -> str:
        """The OpenRouter-side key name."""
        return f"{self.project}-agent"
