"""Runtime settings, read from the environment and from `.env`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Where the pipeline keeps its data and how it identifies itself."""

    model_config = SettingsConfigDict(env_prefix="CAR_SURVEYOR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    registry_path: Path = Path("sources.yaml")
    # Names the project and where to read about it. It never names a browser.
    user_agent: str = "car-surveyor/0.1 (+https://github.com/AhmedAyad89/car-surveyor)"

    # The model that reads documents, and how hard it thinks. Both are part of
    # what an extraction is keyed on, through the model's name.
    extraction_model: str = "claude-opus-5-5"
    extraction_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Read from ANTHROPIC_API_KEY, in the environment or in `.env`. Unset, the
    # SDK falls back to an `ant auth login` profile.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "surveyor.duckdb"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def inbox_dir(self) -> Path:
        """Files downloaded by hand, for documents no permitted client can fetch."""
        return self.data_dir / "inbox"
