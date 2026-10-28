"""Утилиты работы с текстом/контекстом для LLM: точный учёт токенов (tiktoken).

Модуль содержит:
    * :func:`estimate_tokens` — ТОЧНЫЙ подсчёт токенов энкодером
      ``cl100k_base`` (tiktoken, совместим с OpenAI/Qwen-семейством); при
      недоступности tiktoken (offline/air-gapped) — graceful fallback на
      эвристику ``len // 4``;
    * :func:`truncate_context` — итеративное усечение самых длинных строковых
      полей контекста (логи, события), пока сериализованный JSON не уложится
      в ``max_context_tokens``; обрезанные поля помечаются маркером
      :data:`TOKEN_TRUNCATION_MARKER`;
    * :func:`truncate_text_to_tokens` — усечение одной строки по токенам
      (с сохранением хвоста для логов либо головы).

Гарантии:
    * Результат truncate_context всегда JSON-сериализуем (структура
      dict/list сохраняется, обрезаются только значения-строки);
    * Итоговый сериализованный JSON строго <= ``max_context_tokens``
      (последняя гарантия — preview-обёртка).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Маркер старого char-based усечения (совместимость документации/тестов).
TRUNCATION_MARKER = "... [truncated]"

# Маркер token-based усечения (по спеке).
TOKEN_TRUNCATION_MARKER = "... [truncated to fit token limit]"

# Минимум токенов, оставляемых в одном поле при усечении.
_MIN_KEEP_TOKENS = 16

# Примерный лимит контекста для дешёвых моделей (qwen-turbo/plus и т.п.).
TOKEN_SOFT_LIMIT = 8000

# Грубая эвристика fallback'а: для англоязычных текстов 1 токен ~ 4 символа.
CHARS_PER_TOKEN = 4

_ENCODER_NAME = "cl100k_base"
_encoder = None
_encoder_failed = False


def get_encoder():
    """Лениво загрузить tiktoken-энкодер ``cl100k_base``.

    Returns:
        Энкодер tiktoken либо ``None``, если библиотека/сеть недоступны
        (air-gapped): вызывающий код деградирует на эвристику ``len // 4``.
    """
    global _encoder, _encoder_failed
    if _encoder is not None:
        return _encoder
    if _encoder_failed:
        return None
    try:
        import tiktoken

        _encoder = tiktoken.get_encoding(_ENCODER_NAME)
    except Exception as exc:  # noqa: BLE001 - offline/air-gapped деплой
        logger.error(
            "tiktoken encoding %s unavailable (%s) — falling back to chars/4 "
            "token estimate",
            _ENCODER_NAME,
            exc,
        )
        _encoder_failed = True
        return None
    return _encoder


def estimate_tokens(text: str) -> int:
    """Точное число токенов в тексте (tiktoken ``cl100k_base``).

    При недоступности энкодера — эвристика ``len(text) // 4`` (graceful
    degradation: логирование/предупреждения не должны падать).

    Args:
        text: произвольная строка (промт, сериализованный контекст).

    Returns:
        Целое число токенов; для пустой строки — 0.
    """
    if not text:
        return 0
    encoder = get_encoder()
    if encoder is None:
        return max(1, len(text) // CHARS_PER_TOKEN)
    return len(encoder.encode(text))


def truncate_text_to_tokens(
    text: str,
    max_tokens: int,
    marker: str = TOKEN_TRUNCATION_MARKER,
    keep: str = "tail",
) -> str:
    """Урезать строку до ``max_tokens`` токенов (с маркером усечения).

    Args:
        text: исходная строка.
        max_tokens: бюджет токенов на содержимое (маркер добавляется сверх
            бюджета усечения, но итог контролируется вызывающим кодом).
        marker: строка-маркер усечения.
        keep: ``tail`` (логи: хвост важнее) или ``head``.

    Returns:
        Строка, умещающаяся в бюджет, либо исходная (если короче бюджета).
    """
    if not text:
        return text
    encoder = get_encoder()
    if encoder is None:
        # Fallback: режем по символам из расчёта 4 символа на токен.
        budget_chars = max(int(max_tokens) * CHARS_PER_TOKEN, _MIN_KEEP_TOKENS)
        if len(text) <= budget_chars:
            return text
        cut = text[-budget_chars:] if keep == "tail" else text[:budget_chars]
        return (marker + " " + cut) if keep == "tail" else (cut + " " + marker)
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    kept = tokens[-max_tokens:] if keep == "tail" else tokens[:max_tokens]
    # decode может дать замену невалидных байт — это безопасно для JSON.
    cut = encoder.decode(kept, errors="replace")
    return (marker + " " + cut) if keep == "tail" else (cut + " " + marker)


def estimate_json_size(data: Any) -> int:
    """Оценить размер объекта после JSON-сериализации (в символах).

    Args:
        data: любой JSON-сериализуемый объект.

    Returns:
        Длина строки ``json.dumps(...)``; при ошибках сериализации —
        длина ``str(data)`` (fallback, чтобы оценка никогда не падала).
    """
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(data))


def _dump(context: Any) -> str:
    return json.dumps(context, ensure_ascii=False, default=str)


def _iter_strings(node: Any, path: list):
    """Рекурсивно собрать (path, строка) всех строковых значений."""
    if isinstance(node, str):
        yield list(path), node
    elif isinstance(node, dict):
        for key, value in node.items():
            path.append(key)
            yield from _iter_strings(value, path)
            path.pop()
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            path.append(idx)
            yield from _iter_strings(item, path)
            path.pop()


def _set_at_path(node: Any, path: list, value: str) -> None:
    target = node
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value


def truncate_context(context: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Урезать контекст так, чтобы сериализованный JSON был <= ``max_tokens``.

    Алгоритм (итеративный, жадный по токенам):
        1. Если JSON контекста уже укладывается — возвращается как есть.
        2. Иначе находим самое «токеноёмкое» строковое поле (обычно
           ``pod_logs_tail``) и срезаем его ровно на величину переполнения
           (плюс запас на маркер), сохраняя хвост (для логов он важнее).
        3. Повторяем, пока JSON не уложится в лимит (не более 64 проходов).
        4. Последняя гарантия: если структура сама по себе больше лимита —
           возвращаем обёртку ``{"truncated": True, "note": ..., "preview": ...}``,
           где preview усечён по токенам.

    Структура dict/list сохраняется, примитивы не трогаются — результат
    гарантированно JSON-сериализуем.

    Args:
        context: исходный словарь контекста для LLM.
        max_tokens: лимит токенов на сериализованный JSON (из конфига
            ``MAX_CONTEXT_TOKENS``).

    Returns:
        Новый словарь (исходный не мутируется) с усечёнными строками.
    """
    limit = max(int(max_tokens), 64)
    result = json.loads(_dump(context))  # копия через JSON: нормализуем типы

    def fits(candidate: Any) -> bool:
        return estimate_tokens(_dump(candidate)) <= limit

    if fits(result):
        return result

    for _ in range(64):
        strings = list(_iter_strings(result, []))
        if not strings:
            break
        path, text = max(strings, key=lambda item: estimate_tokens(item[1]))
        total = estimate_tokens(_dump(result))
        overshoot = total - limit
        marker_tokens = estimate_tokens(TOKEN_TRUNCATION_MARKER) + 1
        budget = max(_MIN_KEEP_TOKENS, estimate_tokens(text) - overshoot - marker_tokens)
        _set_at_path(result, path, truncate_text_to_tokens(text, budget, keep="tail"))
        if fits(result):
            return result

    if fits(result):
        return result

    # Последняя гарантия: структура больше лимита — preview-обёртка с точным
    # бюджетом: сначала считаем overhead обёртки, потом отдаём остаток preview.
    note = f"context exceeded MAX_CONTEXT_TOKENS={limit}; see 'preview'"
    overhead = estimate_tokens(_dump({"truncated": True, "note": note, "preview": ""}))
    preview_budget = max(_MIN_KEEP_TOKENS, limit - overhead - estimate_tokens(TOKEN_TRUNCATION_MARKER) - 1)
    return {
        "truncated": True,
        "note": note,
        "preview": truncate_text_to_tokens(
            _dump(result), preview_budget, marker=TOKEN_TRUNCATION_MARKER, keep="head"
        ),
    }


__all__ = [
    "CHARS_PER_TOKEN",
    "TOKEN_SOFT_LIMIT",
    "TOKEN_TRUNCATION_MARKER",
    "TRUNCATION_MARKER",
    "estimate_json_size",
    "estimate_tokens",
    "get_encoder",
    "truncate_context",
    "truncate_text_to_tokens",
]
