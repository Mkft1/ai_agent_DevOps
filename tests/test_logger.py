"""Unit-тесты логирования: JSON-форматтер и корреляция request_id."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

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


class TestAuditConcurrency:
    """Конкурентная запись в audit.jsonl не перемешивает строки (filelock)."""

    @staticmethod
    def _point_audit_to(monkeypatch: pytest.MonkeyPatch, path) -> None:
        from src.config import Settings
        from src.utils import audit as audit_module

        monkeypatch.setattr(
            audit_module,
            "get_settings",
            lambda: Settings(_env_file=None, audit_log_path=str(path)),
        )

    def test_ten_concurrent_writes_produce_ten_clean_json_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from src.utils.audit import log_audit_event

        path = tmp_path / "audit.jsonl"
        self._point_audit_to(monkeypatch, path)

        async def hammer() -> None:
            await asyncio.gather(
                *[
                    asyncio.to_thread(
                        log_audit_event,
                        f"diagnose-{i}",
                        {"namespace": "default", "pod_name": f"web-{i}", "i": i},
                        {"root_cause": f"cause {i}", "severity": "high"},
                    )
                    for i in range(10)
                ]
            )

        asyncio.run(hammer())

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10  # ровно 10 строк, никаких склеек/обрывов
        seen = set()
        for line in lines:
            record = json.loads(line)  # каждая строка — валидный JSON (нет interleaving)
            assert record["ts"]
            seen.add(record["incident"]["i"])
        assert seen == set(range(10))
        # lock-файл создаётся рядом с журналом
        assert (tmp_path / "audit.jsonl.lock").exists()

    def test_lock_timeout_is_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        """FileLockTimeout не роняет вызывающий код (graceful degradation)."""
        import filelock

        from src.utils import audit as audit_module
        from src.utils.audit import log_audit_event

        path = tmp_path / "audit.jsonl"
        self._point_audit_to(monkeypatch, path)

        class AlwaysBusyLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise filelock.Timeout(str(path.with_suffix(".lock")))

            def __exit__(self, *exc_info):
                return False

        monkeypatch.setattr(audit_module, "FileLock", AlwaysBusyLock)
        with caplog.at_level("ERROR"):
            log_audit_event("diagnose", {"namespace": "default"}, {"severity": "low"})
        assert not path.exists()  # ничего не записано
        assert any("Audit lock timeout" in rec.message for rec in caplog.records)
