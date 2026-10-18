"""Журнал аудита диагностик (append-only, JSON Lines).

Каждая обработанная диагностика (HTTP ``/diagnose`` или webhook Alertmanager)
дописывается одной JSON-строкой в файл ``AUDIT_LOG_PATH`` (по умолчанию
``audit.jsonl``) — формат JSON Lines удобен для ротации и загрузки в
Loki/CloudWatch/ELK без парсера многострочных записей.

Запись аудита НЕ может уронить HTTP-запрос: любые ошибки файлового ввода
логируются и игнорируются (аудит — побочный эффект, не основной путь).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Из диагноза в журнал пишем только ключевые поля: полный ``raw_data``
# (метры килобайт метрик/логов) раздул бы журнал и дублировал контекст LLM.
_RESULT_FIELDS = ("root_cause", "severity", "suggested_fix", "warnings", "requires_approval")


def audit_log_path() -> Path:
    """Путь файла аудита из настроек (``AUDIT_LOG_PATH``), с expanduser."""
    return Path(os.path.expanduser(get_settings().audit_log_path))


def log_audit_event(event_type: str, incident: dict, result: dict) -> None:
    """Дописать одну JSON-строку (событие аудита) в файл ``audit.jsonl``.

    Args:
        event_type: тип события (``diagnose``, ``alertmanager_diagnose``,
            ``guardrail_rejected``, ``diagnose_failed``, ...).
        incident: входной инцидент (namespace/pod_name/deployment_name/
            description) — как есть, для воспроизводимости.
        result: словарь-диагноз; в журнал попадают только ключевые поля
            (``root_cause``, ``severity``, ``suggested_fix``, ``warnings``).
    """
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "incident": incident,
        "result": {k: result[k] for k in _RESULT_FIELDS if k in result},
    }
    path = audit_log_path()
    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - аудит не должен ронять запрос
        logger.error("Failed to write audit event to %s: %s", path, exc)
