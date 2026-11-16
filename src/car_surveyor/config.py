"""Runtime settings, read from the environment and from `.env.secret`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Models that reject a request with thinking turned off.
THINKING_ONLY_MODEL_PREFIXES = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable",
    "claude-mythos",
)


class Settings(BaseSettings):
    """Where the pipeline keeps its data and how it identifies itself."""

    model_config = SettingsConfigDict(
        env_prefix="CAR_SURVEYOR_", env_file=".env.secret", extra="ignore"
    )

    data_dir: Path = Path("data")
    registry_path: Path = Path("sources.yaml")
    # Names the project and where to read about it. It never names a browser.
    user_agent: str = "car-surveyor/0.1 (+https://github.com/AhmedAyad89/car-surveyor)"

    # The model that reads documents, how hard it works, and whether it thinks
    # first. The model and whether it thinks are part of what an extraction is
    # keyed on, through the extractor's label. Haiku without thinking is the
    # cheap default; thinking can be turned off only at effort `high` or below,
    # and Opus 5.5, Sonnet 5.5 and Fable models do not allow it off at all.
    extraction_model: str = "claude-haiku-5-5"
    extraction_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    extraction_is_thinking: bool = False
    # Read from ANTHROPIC_API_KEY, in the environment or in `.env.secret`. Unset, the
    # SDK falls back to an `ant auth login` profile.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    @model_validator(mode="after")
    def check_extraction_thinking(self) -> Settings:
        """Refuse a model and thinking setting the API would reject on every call."""
        if self.extraction_is_thinking:
            return self
        if self.extraction_effort in ("xhigh", "max"):
            raise ValueError("thinking can be turned off only at extraction effort high or below")
        if self.extraction_model.startswith(THINKING_ONLY_MODEL_PREFIXES):
            raise ValueError(
                f"{self.extraction_model} cannot run without thinking; "
                "set CAR_SURVEYOR_EXTRACTION_IS_THINKING=true"
            )
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "surveyor.duckdb"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def feedback_dir(self) -> Path:
        """What the diff may tell an agent: statuses and counts, never a reference value."""
        return self.data_dir / "feedback"

    @property
    def inbox_dir(self) -> Path:
        """Files downloaded by hand, for documents no permitted client can fetch."""
        return self.data_dir / "inbox"
