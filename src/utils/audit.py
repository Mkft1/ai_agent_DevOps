"""Журнал аудита диагностик (append-only, JSON Lines, потоко-/процессобезопасный).

Каждая обработанная диагностика (HTTP ``/diagnose`` или webhook Alertmanager)
дописывается одной JSON-строкой в файл ``AUDIT_LOG_PATH`` (по умолчанию
``audit.jsonl``) — формат JSON Lines удобен для ротации и загрузки в
Loki/CloudWatch/ELK без парсера многострочных записей.

Потокобезопасность: операция «открыть файл + записать строку» выполняется под
файловой блокировкой ``FileLock(<audit_log_path>.lock)`` (библиотека filelock) —
параллельные запросы в разных потоках/процессах (uvicorn-workers, threadpool
FastAPI) не могут перемешать строки JSONL. Блокировка ждёт до
``LOCK_TIMEOUT_SECONDS``; превышение таймаута логируется как ошибка аудита и
НЕ роняет HTTP-запрос (аудит — побочный эффект, не основной путь).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from src.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Сколько секунд ждём файловую блокировку прежде чем признать запись невозможной.
LOCK_TIMEOUT_SECONDS = 10.0

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
    lock_path = Path(str(path) + ".lock")
    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        # Атомарность строки: один write() под эксклюзивной блокировкой файла —
        # конкурентные потоки/процессы сериализуются, перемешивание исключено.
        with FileLock(lock_path, timeout=LOCK_TIMEOUT_SECONDS):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except FileLockTimeout as exc:
        logger.error(
            "Audit lock timeout (%ss) on %s — event NOT written: %s",
            LOCK_TIMEOUT_SECONDS,
            path,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 - аудит не должен ронять запрос
        logger.error("Failed to write audit event to %s: %s", path, exc)
