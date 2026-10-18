"""Unit-тесты логирования: JSON-форматтер и корреляция request_id."""

from __future__ import annotations

import json
import logging

from src.utils.logger import JsonFormatter, request_id_var


def _record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="src.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=None,
    )


class TestJsonFormatter:
    def test_emits_valid_json_with_core_fields(self) -> None:
        line = JsonFormatter().format(_record("probe message"))
        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "src.test"
        assert payload["msg"] == "probe message"
        assert payload["ts"]  # ISO-таймстемп присутствует

    def test_request_id_appears_when_set(self) -> None:
        token = request_id_var.set("req-123")
        try:
            payload = json.loads(JsonFormatter().format(_record()))
        finally:
            request_id_var.reset(token)
        assert payload["request_id"] == "req-123"

    def test_no_request_id_field_outside_request(self) -> None:
        payload = json.loads(JsonFormatter().format(_record()))
        assert "request_id" not in payload

    def test_exception_is_included(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = _record("failed")
            rec.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(rec))
        assert "ValueError: boom" in payload["exception"]
