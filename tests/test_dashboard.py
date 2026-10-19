"""Тесты дашборда (ui/dashboard.py): данные, таблица, метрики и UI через AppTest."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

UI_PATH = Path(__file__).resolve().parents[1] / "ui" / "dashboard.py"


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("ui_dashboard", UI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load_dashboard()


def _event(ts: str, ns: str, severity: str, root_cause: str, approval: bool = False,
           pod: str | None = "web-1", event_type: str = "diagnose") -> dict:
    return {
        "ts": ts,
        "event_type": event_type,
        "incident": {"namespace": ns, "pod_name": pod, "description": "d"},
        "result": {
            "root_cause": root_cause,
            "severity": severity,
            "suggested_fix": "kubectl delete pod web-1 --force",
            "warnings": [],
            "requires_approval": approval,
        },
    }


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    lines = [
        json.dumps(_event("2026-10-19T10:00:00Z", "default", "high", "OOMKilled: memory limit")),
        '{"broken json",',  # оборванная строка — пропускается
        json.dumps(_event("2026-10-19T11:00:00Z", "prod", "critical", "eviction after node pressure",
                          approval=True)),
        json.dumps(_event("2026-10-19T11:30:00Z", "default", "unknown", "guardrail",
                          event_type="guardrail_rejected")),  # не диагноз
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLoadAuditEvents:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert dash.load_audit_events(tmp_path / "nope.jsonl") == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "audit.jsonl"
        empty.write_text("", encoding="utf-8")
        assert dash.load_audit_events(empty) == []

    def test_skips_broken_and_non_diagnosis(self, audit_file: Path) -> None:
        events = dash.load_audit_events(audit_file)
        assert len(events) == 2
        assert {e["incident"]["namespace"] for e in events} == {"default", "prod"}


class TestTimestampAndTable:
    def test_format_timestamp_readable(self) -> None:
        assert dash.format_timestamp("2026-10-19T10:00:00Z") == "19 Oct, 10:00"
        assert dash.format_timestamp("2026-01-05T08:07:00Z") == "5 Jan, 08:07"

    def test_format_timestamp_garbage_passthrough(self) -> None:
        assert dash.format_timestamp("not-a-date") == "not-a-date"

    def test_columns_severity_emoji_and_order(self, audit_file: Path) -> None:
        df = dash.build_dataframe(dash.load_audit_events(audit_file))
        assert list(df.columns) == ["Time", "Namespace", "Pod", "Severity", "Root Cause"]
        # свежие сверху; severity с цветовым кодом; время читаемое
        assert df.iloc[0]["Time"] == "19 Oct, 11:00"
        assert df.iloc[0]["Severity"] == "🔴 critical"
        assert df.iloc[1]["Severity"] == "🟠 high"
        assert df.iloc[1]["Root Cause"] == "OOMKilled: memory limit"

    def test_empty_dataframe_keeps_columns(self) -> None:
        df = dash.build_dataframe([])
        assert list(df.columns) == ["Time", "Namespace", "Pod", "Severity", "Root Cause"]
        assert df.empty


class TestSummaryAndApprovals:
    def test_summarize_metrics(self, audit_file: Path) -> None:
        s = dash.summarize(dash.load_audit_events(audit_file))
        assert s == {"total": 2, "critical": 1, "pending": 1}

    def test_approvals_pending_filter(self, audit_file: Path) -> None:
        pending = dash.approvals_pending(dash.load_audit_events(audit_file))
        assert len(pending) == 1
        assert pending[0]["result"]["requires_approval"] is True


@pytest.fixture()
def app_env(audit_file: Path, monkeypatch: pytest.MonkeyPatch):
    """AUDIT_LOG_PATH указывает на фикстуру; кэш settings сброшен."""
    import src.config

    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    src.config.get_settings.cache_clear()
    yield
    src.config.get_settings.cache_clear()


class TestStreamlitApp:
    @staticmethod
    def _run():
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(UI_PATH), default_timeout=20)
        at.run()
        return at

    def test_title_metrics_and_table(self, app_env) -> None:
        at = self._run()
        assert not at.exception
        assert at.title[0].value == "AI DevOps Agent - Incident Dashboard"
        labels = [m.label for m in at.metric]
        values = [m.value for m in at.metric]
        assert labels == ["Total Incidents", "Critical Incidents", "Pending Approvals"]
        assert values == ["2", "1", "1"]
        assert len(at.dataframe) == 1

    def test_approval_card_button_success_and_balloons(self, app_env) -> None:
        at = self._run()
        approve = [b for b in at.button if "Approve & Apply" in b.label]
        assert len(approve) == 1
        approve[0].click().run()
        assert not at.exception
        assert at.success[0].value == "Action approved and applied!"
        assert len(at.get("balloons")) == 1

    def test_raw_audit_in_expander(self, app_env) -> None:
        at = self._run()
        assert len(at.expander) == 1
        assert "View Raw Audit Log" in at.expander[0].label

    def test_missing_journal_shows_warning(self, tmp_path: Path, monkeypatch) -> None:
        import src.config

        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "absent.jsonl"))
        src.config.get_settings.cache_clear()
        try:
            at = self._run()
            assert not at.exception
            assert len(at.warning) == 1
        finally:
            src.config.get_settings.cache_clear()
