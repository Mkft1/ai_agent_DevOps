"""Unit-тесты truncate_context: рекурсивное усечение контекста для LLM."""

import json

from src.utils.text_utils import (
    TRUNCATION_MARKER,
    estimate_json_size,
    estimate_tokens,
    truncate_context,
)


class TestTruncateContext:
    """Проверки поведения truncate_context по ТЗ Шага «Graceful Degradation»."""

    def test_long_string_truncated_with_marker(self) -> None:
        """Длинные строки обрезаются с маркером '... [truncated]'."""
        context = {"pod_logs_tail": "x" * 5000, "incident": {"namespace": "default"}}
        result = truncate_context(context, max_chars=1000)

        assert TRUNCATION_MARKER in result["pod_logs_tail"]
        assert len(result["pod_logs_tail"]) <= 1000
        # Короткие значения не трогаются.
        assert result["incident"]["namespace"] == "default"

    def test_small_context_unchanged(self) -> None:
        """Если JSON уже в лимите — контекст возвращается без изменений."""
        context = {"a": "short", "b": {"c": 42}}
        result = truncate_context(context, max_chars=16000)
        assert result == context
        assert TRUNCATION_MARKER not in json.dumps(result)

    def test_result_is_json_serializable_and_within_limit(self) -> None:
        """После обрезки JSON сериализуем и не превышает max_chars."""
        context = {
            "pods": [{"name": f"pod-{i}", "labels": {"k": "v" * 300}} for i in range(50)],
            "pod_logs_tail": "\n".join(f"line {i}: " + "log" * 100 for i in range(500)),
            "metrics": {"cpu_cores": 0.5, "memory_bytes": 123},
        }
        limit = 4000
        result = truncate_context(context, max_chars=limit)

        dumped = json.dumps(result, ensure_ascii=False, default=str)  # не падает
        assert len(dumped) <= limit
        assert TRUNCATION_MARKER in dumped

    def test_recursion_into_nested_lists_and_dicts(self) -> None:
        """Обрезка применяется рекурсивно во вложенных структурах."""
        context = {
            "events": [
                {"message": "boom " * 400, "count": 3},
                {"message": "ok"},
            ],
            "target_pod": {"status": "Running " * 300},
        }
        result = truncate_context(context, max_chars=800)

        assert TRUNCATION_MARKER in result["events"][0]["message"]
        assert result["events"][1]["message"] == "ok"
        assert TRUNCATION_MARKER in result["target_pod"]["status"]
        # Не-строковые примитивы сохраняются как есть.
        assert result["events"][0]["count"] == 3

    def test_multiline_logs_keep_tail(self) -> None:
        """Для многострочных логов сохраняется ХВОСТ (последние строки)."""
        lines = [f"line-{i:04d} " + "m" * 60 for i in range(200)]
        context = {"pod_logs_tail": "\n".join(lines)}
        result = truncate_context(context, max_chars=600)

        truncated = result["pod_logs_tail"]
        assert truncated.startswith(TRUNCATION_MARKER)
        assert "line-0199" in truncated  # последняя строка сохранена
        assert "line-0000" not in truncated  # начало отброшено

    def test_source_context_not_mutated(self) -> None:
        """Исходный словарь не мутируется — создаётся копия со строками-заменами."""
        original_value = "y" * 3000
        context = {"logs": original_value}
        truncate_context(context, max_chars=500)
        assert context["logs"] == original_value

    def test_estimate_json_size_fallback_never_raises(self) -> None:
        """Оценка размера не падает даже на не-сериализуемых объектах."""

        class Weird:
            pass

        assert estimate_json_size({"obj": Weird()}) > 0


class TestEstimateTokens:
    """Грубая оценка токенов (1 токен ~ 4 символа)."""

    def test_empty_string_is_zero(self):
        assert estimate_tokens("") == 0

    def test_rough_four_chars_per_token(self):
        assert estimate_tokens("a" * 400) == 100

    def test_short_text_at_least_one_token(self):
        assert estimate_tokens("ab") == 1

    def test_estimate_scales_with_prompt_size(self):
        small = estimate_tokens("error: oom killed")
        big = estimate_tokens("oom killed container " * 500)
        assert big > small
