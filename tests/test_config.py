"""Tests for where the settings are read from. The keys here are made up."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from car_surveyor.config import Settings


def test_settings_default_to_haiku_without_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert (settings.extraction_model, settings.extraction_is_thinking) == (
        "claude-haiku-5-5",
        False,
    )


@pytest.mark.parametrize(
    ("model", "effort"),
    [("claude-haiku-5-5", "xhigh"), ("claude-opus-5-5", "high"), ("claude-sonnet-5-5", "low")],
)
def test_settings_refuse_thinking_off_where_the_api_would_reject_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, effort: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAR_SURVEYOR_EXTRACTION_MODEL", model)
    monkeypatch.setenv("CAR_SURVEYOR_EXTRACTION_EFFORT", effort)
    with pytest.raises(ValidationError, match="thinking"):
        Settings()
    monkeypatch.setenv("CAR_SURVEYOR_EXTRACTION_IS_THINKING", "true")
    assert Settings().extraction_model == model


def test_settings_read_the_api_key_from_the_secret_file_and_not_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-env\n", encoding="utf-8")
    (tmp_path / ".env.secret").write_text("ANTHROPIC_API_KEY=from-secret\n", encoding="utf-8")

    key = Settings().anthropic_api_key

    assert key is not None
    assert key.get_secret_value() == "from-secret"


def test_settings_have_no_api_key_without_the_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-env\n", encoding="utf-8")

    assert Settings().anthropic_api_key is None
