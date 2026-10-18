"""Утилиты работы с текстом/контекстом для LLM-агента.

Модуль содержит :func:`truncate_context` — рекурсивное усечение длинных строк
в словаре (например, логов пода), чтобы сериализованный в JSON контекст не
превышал лимит ``MAX_CONTEXT_CHARS`` из конфигурации.

Гарантии:
    * Результат всегда JSON-сериализуем (структура словаря/списков сохраняется,
      обрезаются только значения-строки).
    * Обрезанные строки помечаются маркером :data:`TRUNCATION_MARKER`, чтобы
      LLM и оператор понимали неполноту данных.
"""

from __future__ import annotations

import json
from typing import Any

# Маркер, добавляемый к обрезанной строке (по ТЗ).
TRUNCATION_MARKER = "... [truncated]"

# Минимальная длина строки, ниже которой усечение по словам отключается
# (чтобы не оставлять пустые «огрызки» очень маленьких лимитов).
_MIN_KEEP_CHARS = 32


def _shrink_string(value: str, max_len: int) -> str:
    """Обрезать одну строку до ``max_len`` символов с маркером.

    Если внутри лимита есть переводы строк — сохраняем последние строки
    (для логов хвост важнее головы). Иначе режем по словам, затем жёстко.

    Args:
        value: исходная строка.
        max_len: максимальная итоговая длина (включая маркер).

    Returns:
        Строка длиной не более ``max_len`` либо исходная (если короче лимита).
    """
    if len(value) <= max_len:
        return value
    keep = max(max_len - len(TRUNCATION_MARKER), _MIN_KEEP_CHARS)
    lines = value.splitlines()
    if len(lines) > 1:
        # Логи: оставляем ХВОСТ (последние строки наиболее релевантны алерту).
        kept_parts: list[str] = []
        total = 0
        for line in reversed(lines):
            addition = len(line) + (1 if kept_parts else 0)
            if total + addition > keep:
                break
            kept_parts.insert(0, line)
            total += addition
        if kept_parts:
            return TRUNCATION_MARKER + " " + "\n".join(kept_parts)
        head = lines[-1][:keep]
        return head + TRUNCATION_MARKER
    # Одна длинная строка: режем по словам, если возможно.
    words = value.split(" ")
    prefix = ""
    for word in words:
        candidate = f"{prefix} {word}".strip()
        if len(candidate) + len(TRUNCATION_MARKER) > max_len:
            break
        prefix = candidate
    if prefix:
        return prefix + TRUNCATION_MARKER
    return value[:keep] + TRUNCATION_MARKER


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


def truncate_context(context: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Рекурсивно урезать длинные строки, чтобы JSON контекста влезал в лимит.

    Алгоритм:
        1. Если весь контекст уже короче ``max_chars`` — возвращается как есть.
        2. Иначе строки длиннее порога обрезаются с маркером ``... [truncated]``
           (для многострочных логов сохраняется хвост). Порог подбирается
           адаптивно: чем больше переполнение — тем агрессивнее усечение.
        3. Процесс повторяется несколько проходов, пока размер не устроится в
           лимит или пока строки не станут минимальными.

    Структура dict/list сохраняется, примитивы (int, float, bool, None) не
    трогаются — результат гарантированно JSON-сериализуем.

    Args:
        context: исходный словарь контекста для LLM.
        max_chars: максимальный допустимый размер JSON-строки (из конфига
            ``MAX_CONTEXT_CHARS``).

    Returns:
        Новый словарь (исходный не мутируется) с усечёнными строками.
    """
    limit = max(int(max_chars), 512)

    def _walk(node: Any, threshold: int) -> Any:
        if isinstance(node, str):
            return _shrink_string(node, threshold) if len(node) > threshold else node
        if isinstance(node, dict):
            return {key: _walk(value, threshold) for key, value in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(item, threshold) for item in node]
        return node

    def _fits(candidate: Any) -> bool:
        """Проверка, что candidate сериализуем в JSON и укладывается в лимит."""
        try:
            return len(json.dumps(candidate, ensure_ascii=False, default=str)) <= limit
        except (TypeError, ValueError):
            return False

    if _fits(context):
        return context

    # Адаптивный порог усечения строк: начинаем с половины лимита и уменьшаем
    # вдвое каждый проход. Когда строки достигли минимального порога, но JSON
    # всё ещё велик — дополнительно сокращаем СПИСКИ (хвост наиболее важных
    # элементов). Проходы ограничены (защита от бесконечных циклов).
    min_threshold = _MIN_KEEP_CHARS + len(TRUNCATION_MARKER)
    threshold = max(limit // 2, min_threshold)
    result = _walk(context, threshold)

    while not _fits(result):
        if threshold > min_threshold:
            threshold = max(threshold // 2, min_threshold)
            result = _walk(context, threshold)
            continue
        # Строки уже минимальны — режем списки, сохраняя хвост.
        list_len = max(len(result.get("pods", [])) // 2, 1) if isinstance(result, dict) else 10

        def _shrink_lists(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: _shrink_lists(v) for k, v in node.items()}
            if isinstance(node, list):
                trimmed = [_shrink_lists(item) for item in node[-list_len:]]
                trimmed.insert(0, f"[... {len(node) - list_len} items omitted]")
                return trimmed
            return node

        shrunk = _shrink_lists(result)
        if estimate_json_size(shrunk) >= estimate_json_size(result) or shrunk == result:
            # Уменьшать больше нечего: жёсткая гарантия лимита через preview.
            dumped = json.dumps(result, ensure_ascii=False, default=str)
            return {
                "truncated": True,
                "note": f"context exceeded MAX_CONTEXT_CHARS={limit}; see 'preview'",
                "preview": dumped[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER,
            }
        result = shrunk
        list_len = max(list_len // 2, 1)

    return result


# ----------------------------------------------------------------------
# Оценка размера контекста в токенах (для LLM-провайдера)
# ----------------------------------------------------------------------

# Грубая эвристика: для англоязычных текстов 1 токен ~ 4 символа.
# Точная оценка требует токенизатора модели (tiktoken и т.п.), который мы
# сознательно не добавляем — лёгкая библиотека важнее точности на этом шаге.
CHARS_PER_TOKEN = 4

# Примерный лимит контекста для дешёвых моделей (qwen-turbo/plus и т.п.).
TOKEN_SOFT_LIMIT = 8000


def estimate_tokens(text: str) -> int:
    """Грубо оценить число токенов в тексте.

    Эвристика ``len(text) // CHARS_PER_TOKEN`` (1 токен ~ 4 символа) — без
    внешних тяжёлых зависимостей вроде tiktoken. Используется только для
    логирования и предупреждений, никогда для блокировки запросов.

    Args:
        text: произвольная строка (промт, сериализованный контекст).

    Returns:
        Целое число токенов; для пустой строки возвращает 0.
    """
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)
