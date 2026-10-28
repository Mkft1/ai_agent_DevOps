"""Unit-тесты token-based усечения контекста (tiktoken, src/utils/text_utils.py)."""

from __future__ import annotations

import json

import pytest

from src.utils.text_utils import (
    TOKEN_TRUNCATION_MARKER,
    estimate_json_size,
    estimate_tokens,
    truncate_context,
    truncate_text_to_tokens,
)


def _dump(ctx) -> str:
    return json.dumps(ctx, ensure_ascii=False, default=str)


def _tokens_of(ctx) -> int:
    return estimate_tokens(_dump(ctx))


class TestEstimateTokens:
    def test_exact_via_tiktoken(self) -> None:
        tiktoken = pytest.importorskip("tiktoken")  # offline-деплои не блокируем

        enc = tiktoken.get_encoding("cl100k_base")
        text = "The quick brown fox jumps over the lazy dog"
        assert estimate_tokens(text) == len(enc.encode(text))

    def test_empty(self) -> None:
        assert estimate_tokens("") == 0

    def test_fallback_when_encoder_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.utils.text_utils as tu

        monkeypatch.setattr(tu, "get_encoder", lambda: None)
        assert estimate_tokens("x" * 400) == 100  # len // 4


class TestTruncateTextToTokens:
    def test_keeps_tail_and_marker(self) -> None:
        text = "line head " + "word " * 500 + "LINE TAIL"
        out = truncate_text_to_tokens(text, 50, keep="tail")
        assert out.startswith(TOKEN_TRUNCATION_MARKER)
        assert "LINE TAIL" in out
        assert estimate_tokens(out) <= 50 + estimate_tokens(TOKEN_TRUNCATION_MARKER) + 1

    def test_short_text_unchanged(self) -> None:
        assert truncate_text_to_tokens("short", 100) == "short"


class TestTruncateContext:
    def test_small_context_unchanged(self) -> None:
        context = {"a": "short", "b": {"c": 42}}
        assert truncate_context(context, 4000) == context
        assert TOKEN_TRUNCATION_MARKER not in _dump(truncate_context(context, 4000))

    def test_ten_thousand_token_log_fits_limit_with_marker(self) -> None:
        """По спеке: лог на 10 000 токенов -> результат <= max_context_tokens + маркер."""
        pytest.importorskip("tiktoken")  # точная токенизация нужна для калибровки лога
        log = "word " * 10_000  # ~10 000 токенов cl100k_base
        assert 9_500 <= estimate_tokens(log) <= 10_500
        context = {
            "incident": {"namespace": "default", "pod_name": "web-1"},
            "pod_logs_tail": log,
            "metrics": {"cpu_cores": 0.5},
        }
        limit = 4000
        result = truncate_context(context, limit)

        assert _tokens_of(result) <= limit  # строго в лимите токенов
        assert TOKEN_TRUNCATION_MARKER in _dump(result)
        json.loads(_dump(result))  # JSON остаётся валидным
        # Структура и короткие поля сохранены.
        assert result["incident"]["namespace"] == "default"
        assert result["metrics"]["cpu_cores"] == 0.5

    def test_nested_structures_truncated_and_valid(self) -> None:
        context = {
            "events": [{"message": "boom " * 400, "count": 3}, {"message": "ok"}],
            "target_pod": {"status": "Running " * 300},
        }
        result = truncate_context(context, 300)
        assert _tokens_of(result) <= 300
        assert TOKEN_TRUNCATION_MARKER in result["events"][0]["message"]
        assert result["events"][1]["message"] == "ok"
        json.loads(_dump(result))

    def test_extreme_tiny_limit_still_fits(self) -> None:
        context = {"pod_logs_tail": "word " * 5000, "pods": [{"name": "p" * 200}]}
        result = truncate_context(context, 128)
        assert _tokens_of(result) <= 128
        json.loads(_dump(result))

    def test_original_context_not_mutated(self) -> None:
        context = {"pod_logs_tail": "word " * 5000}
        truncate_context(context, 200)
        assert context["pod_logs_tail"].startswith("word word")
        assert TOKEN_TRUNCATION_MARKER not in context["pod_logs_tail"]

    def test_estimate_json_size_still_char_based(self) -> None:
        assert estimate_json_size({"a": "bbb"}) == len('{"a": "bbb"}')
