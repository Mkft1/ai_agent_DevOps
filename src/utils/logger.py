"""Базовое логирование для всего приложения.

Поддерживает два формата (``Settings.log_format`` / env ``LOG_FORMAT``):
    * ``text`` — человекочитаемый формат для локальной разработки;
    * ``json`` — structured logging (одна JSON-строка на запись) для
      агрегаторов уровня Loki / CloudWatch / ELK.

Через :data:`request_id_var` в JSON-логи автоматически попадает
``request_id`` (middleware в ``src/main.py`` ставит его из заголовка
``X-Request-ID`` либо генерирует новый) — логи коррелируют с HTTP-запросами.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Идентификатор текущего HTTP-запроса (X-Request-ID). "-" вне запроса.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Форматтер одной JSON-строки: ts/level/logger/msg + request_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id and request_id != "-":
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_format: str = "text") -> None:
    """Настраивает корневой логгер приложения.

    Args:
        level: уровень логирования (DEBUG/INFO/WARNING/ERROR/CRITICAL).
        log_format: ``text`` (по умолчанию) или ``json`` (structured logging).
    """
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Возвращает именованный логгер."""
    return logging.getLogger(name)
