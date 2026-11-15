"""Runtime settings, read from the environment and from `.env`."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Where the pipeline keeps its data and how it identifies itself."""

    model_config = SettingsConfigDict(env_prefix="CAR_SURVEYOR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    registry_path: Path = Path("sources.yaml")
    # Names the project and where to read about it. It never names a browser.
    user_agent: str = "car-surveyor/0.1 (+https://github.com/AhmedAyad89/car-surveyor)"

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
