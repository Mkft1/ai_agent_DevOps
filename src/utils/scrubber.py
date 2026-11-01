"""Маскирование чувствительных данных (PII/секретов) перед отправкой в LLM.

Логи контейнеров и события Kubernetes регулярно содержат секреты: пароли,
JWT, API-ключи, Bearer-токены, email. Агент диагностики отправляет эти
данные внешнему LLM-провайдеру (Qwen/OpenAI-совместимый API) — без
маскирования это прямая утечка и блокер для Enterprise compliance review.

Модуль — последняя линия защиты приватности данных: оркестратор вызывает
:func:`scrub_sensitive_data` для ``context["pod_logs_tail"]`` и каждого
элемента ``context["events"]`` **до** сериализации контекста в JSON для
LLM. Ответ модели (root_cause / evidence / suggested_fix) НЕ маскируется —
это сгенерированный текст, а не сырые секреты кластера.

Правила маскирования (в порядке применения):
    1. Пароли в URL   ``://user:pass@host`` -> ``://user:[REDACTED:password]@host``
    2. JWT-токены     ``eyJhbGci...``       -> ``[REDACTED:jwt]``
    3. API-ключи      ``sk-`` / ``ghp_`` / ``AKIA`` / ``key-`` -> ``[REDACTED:api_key]``
    4. Bearer-токены  ``Bearer abc...``     -> ``[REDACTED:bearer]``
    5. Email          ``user@domain.tld``   -> ``[REDACTED:email]``
    6. IPv4-адреса    только при ``REDACT_IPS=true`` -> ``[REDACTED:ip]``

Порядок важен: пароль в URL маскируется до email (иначе ``pass@host``
будет посчитан email'ом, а структура URL сломается), а JWT/API-ключи — до
Bearer (иначе тип вложенного секрета теряется). Замена пароля сохраняет
схему/user/host URL — они нужны для диагностики.

IP-адреса по умолчанию НЕ маскируются: они критичны для сетевой
диагностики (``REDACT_IPS=true`` — опциональный режим усиления).

Конфигурация (``.env``, см. src/config.py):
    ENABLE_SCRUBBING=true   вкл/выкл маскирование (false — passthrough)
    REDACT_IPS=false        дополнительно маскировать IPv4

Наблюдаемость: оркестратор пишет счётчики по типам в Prometheus-метрику
``scrubbed_items_total{type=...}`` (см. src/utils/metrics.py).
"""

from __future__ import annotations

import re
from typing import Any

from src.config import get_settings

# Шаблон замены: [REDACTED:<type>] (тип — имя категории секрета).
REDACTED_TEMPLATE = "[REDACTED:{type}]"

# ----------------------------------------------------------------------
# Паттерны чувствительных данных (регулярные выражения из ТЗ)
# ----------------------------------------------------------------------
# JWT: три base64url-сегмента, заголовок всегда начинается с "eyJ" ({"...).
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# API-ключи популярных провайдеров: OpenAI (sk-), GitHub PAT (ghp_),
# AWS Access Key ID (AKIA), универсальный key-<32+ символов>.
API_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"key-[a-zA-Z0-9]{32,}"),
)

# Пароль в URL: scheme://user:PASSWORD@host — группа 1 содержит сам пароль.
URL_PASSWORD_RE = re.compile(r"://[^:\s]+:([^@\s]+)@")

# Bearer-токен (Authorization: Bearer <token>),RFC 3986 unreserved + base64.
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*")

# Email-адрес.
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Валидный IPv4 (октеты 0-255) — только для опционального REDACT_IPS.
IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)


def _redacted(item_type: str) -> str:
    """Плейсхолдер замены для типа секрета: ``[REDACTED:<type>]``."""
    return REDACTED_TEMPLATE.format(type=item_type)


def _merge_counters(total: dict[str, int], part: dict[str, int]) -> None:
    """Слить счётчики ``part`` в ``total`` (суммирование по типам)."""
    for item_type, count in part.items():
        total[item_type] = total.get(item_type, 0) + count


def _resolve_flags(
    enabled: bool | None, redact_ips: bool | None
) -> tuple[bool, bool]:
    """Разрешить None-флаги из конфигурации (ENABLE_SCRUBBING / REDACT_IPS)."""
    if enabled is None or redact_ips is None:
        settings = get_settings()
        if enabled is None:
            enabled = settings.enable_scrubbing
        if redact_ips is None:
            redact_ips = settings.redact_ips
    return bool(enabled), bool(redact_ips)


def _mask_url_passwords(text: str, counters: dict[str, int]) -> str:
    """Заменить пароль в URL на ``[REDACTED:password]``, сохранив структуру.

    Паттерн :data:`URL_PASSWORD_RE` захватывает весь фрагмент
    ``://user:password@``; замена выполняется только для группы 1 (сам
    пароль), чтобы схема, пользователь и хост остались в тексте — они
    нужны LLM для диагностики (например, connectivity-проблем).
    """

    def _repl(match: re.Match[str]) -> str:
        if match.group(1) == _redacted("password"):
            return match.group(0)  # уже замаскировано — идемпотентность счётчиков
        counters["password"] = counters.get("password", 0) + 1
        start, end = match.span(1)
        offset = match.start()
        whole = match.group(0)
        return whole[: start - offset] + _redacted("password") + whole[end - offset :]

    return URL_PASSWORD_RE.sub(_repl, text)


def _sub_counted(
    pattern: re.Pattern[str], item_type: str, text: str, counters: dict[str, int]
) -> str:
    """Заменить все совпадения ``pattern`` на ``[REDACTED:<type>]`` со счётчиком."""
    masked, hits = pattern.subn(_redacted(item_type), text)
    if hits:
        counters[item_type] = counters.get(item_type, 0) + hits
    return masked


def scrub_sensitive_data(
    text: str,
    *,
    enabled: bool | None = None,
    redact_ips: bool | None = None,
) -> tuple[str, dict[str, int]]:
    """Замаскировать чувствительные данные (секреты/PII) в тексте.

    Args:
        text: исходный текст (лог пода, сообщение K8s-события, любая строка).
        enabled: переопределение ``ENABLE_SCRUBBING``; ``None`` — взять из
            конфигурации (:func:`~src.config.get_settings`).
        redact_ips: переопределение ``REDACT_IPS``; ``None`` — из конфигурации.

    Returns:
        Кортеж ``(masked_text, counters)``: очищенный текст и счётчики
        замаскированных элементов по типам, например ``{"jwt": 2, "email": 1}``
        (присутствуют только типы с ненулевыми попаданиями). При выключенном
        маскировании — ``(text, {})`` без изменений (passthrough).

    Функция никогда не бросает исключений на нестроковых/пустых входах
    (graceful degradation HTTP-пути): такие значения проходят как есть.
    """
    if not isinstance(text, str) or not text:
        return text, {}

    scrubbing_enabled, ips_enabled = _resolve_flags(enabled, redact_ips)
    if not scrubbing_enabled:
        return text, {}

    counters: dict[str, int] = {}
    # 1. Пароли в URL — первыми, чтобы pass@host не был посчитан email'ом.
    masked = _mask_url_passwords(text, counters)
    # 2. JWT — до Bearer, чтобы вложенный токен получил точный тип.
    masked = _sub_counted(JWT_RE, "jwt", masked, counters)
    # 3. API-ключи провайдеров.
    for pattern in API_KEY_PATTERNS:
        masked = _sub_counted(pattern, "api_key", masked, counters)
    # 4. Bearer-токены (после JWT/API-ключей: вложенные секреты уже заменены).
    masked = _sub_counted(BEARER_RE, "bearer", masked, counters)
    # 5. Email.
    masked = _sub_counted(EMAIL_RE, "email", masked, counters)
    # 6. IPv4 — только при явном REDACT_IPS=true (по умолчанию нужны диагностике).
    if ips_enabled:
        masked = _sub_counted(IPV4_RE, "ip", masked, counters)
    return masked, counters


def scrub_structure(
    obj: Any,
    *,
    enabled: bool | None = None,
    redact_ips: bool | None = None,
) -> tuple[Any, dict[str, int]]:
    """Рекурсивно замаскировать строки в JSON-подобной структуре (dict/list/str).

    Используется оркестратором для ``context["events"]``: каждое событие —
    dict, строковые поля которого (message/reason/...) могут содержать
    секреты. Ключи dict не обрабатываются — только значения; нестроковые
    значения (int/float/bool/None) проходят без изменений.

    Args:
        obj: произвольная JSON-сериализуемая структура или строка.
        enabled: переопределение ``ENABLE_SCRUBBING`` (None — из конфигурации).
        redact_ips: переопределение ``REDACT_IPS`` (None — из конфигурации).

    Returns:
        ``(masked_obj, counters)`` — структура той же формы с очищенными
        строками и суммарные счётчики по типам.
    """
    kwargs = {"enabled": enabled, "redact_ips": redact_ips}
    if isinstance(obj, str):
        return scrub_sensitive_data(obj, **kwargs)
    if isinstance(obj, dict):
        counters: dict[str, int] = {}
        masked_dict = {}
        for key, value in obj.items():
            clean, stats = scrub_structure(value, **kwargs)
            masked_dict[key] = clean
            _merge_counters(counters, stats)
        return masked_dict, counters
    if isinstance(obj, list):
        counters = {}
        masked_list = []
        for item in obj:
            clean, stats = scrub_structure(item, **kwargs)
            masked_list.append(clean)
            _merge_counters(counters, stats)
        return masked_list, counters
    return obj, {}
