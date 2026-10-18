"""Unit-тесты конфигурации (src/config.py): fail-fast валидаторы настроек."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def make_settings(**kwargs) -> Settings:
    """Settings без чтения .env (детерминированно), с явными значениями."""
    return Settings(_env_file=None, **kwargs)


class TestLogLevelValidation:
    def test_valid_levels_accepted_and_uppercased(self) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"
        assert make_settings(log_level="info").log_level == "INFO"
        assert make_settings(log_level="CRITICAL").log_level == "CRITICAL"

    def test_invalid_level_fails_fast(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(log_level="VERBOSE")


class TestLogFormatValidation:
    def test_valid_formats(self) -> None:
        assert make_settings(log_format="text").log_format == "text"
        assert make_settings(log_format="JSON").log_format == "json"

    def test_invalid_format_fails_fast(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(log_format="xml")


class TestGuardrailBounds:
    def test_max_log_lines_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(max_log_lines=0)
        with pytest.raises(ValidationError):
            make_settings(max_log_lines=-5)

    def test_max_context_chars_has_sane_minimum(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(max_context_chars=10)
        assert make_settings(max_context_chars=1000).max_context_chars == 1000


class TestNewSettingsDefaults:
    def test_defaults(self) -> None:
        s = make_settings()
        assert s.log_format == "text"
        assert s.llm_offline is False

    def test_llm_offline_flag(self) -> None:
        assert make_settings(llm_offline="true").llm_offline is True
