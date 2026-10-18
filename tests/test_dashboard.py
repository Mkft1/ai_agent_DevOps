"""Тесты логики дашборда (ui/dashboard.py) без запуска streamlit-рантайма."""

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
           event_type: str = "diagnose") -> dict:
    return {
        "ts": ts,
        "event_type": event_type,
        "incident": {"namespace": ns, "pod_name": "web-1", "description": "d"},
        "result": {
            "root_cause": root_cause,
            "severity": severity,
            "suggested_fix": "kubectl delete pod web-1",
            "warnings": [],
            "requires_approval": approval,
        },
    }


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    lines = [
        json.dumps(_event("2026-10-19T10:00:00Z", "default", "high", "OOMKilled: memory limit")),
        '{"broken json",',  # битая строка (например, оборванная ротация) — пропускается
        json.dumps(_event("2026-10-19T11:00:00Z", "prod", "critical", "delete pod proposed", approval=True)),
        json.dumps(_event("2026-10-19T11:30:00Z", "default", "unknown", "guardrail",
                          event_type="guardrail_rejected")),  # не диагноз — мимо таблицы
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLoadAuditEvents:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert dash.load_audit_events(tmp_path / "nope.jsonl") == []

    def test_reads_diagnosis_events_skips_broken_and_non_diagnosis(self, audit_file: Path) -> None:
        events = dash.load_audit_events(audit_file)
        assert len(events) == 2
        assert {e["incident"]["namespace"] for e in events} == {"default", "prod"}


class TestDataframe:
    def test_columns_and_order(self, audit_file: Path) -> None:
        df = dash.build_dataframe(dash.load_audit_events(audit_file))
        assert list(df.columns) == ["Timestamp", "Namespace", "Severity", "Root Cause"]
        # свежие сверху
        assert df.iloc[0]["Timestamp"] == "2026-10-19T11:00:00Z"
        assert df.iloc[0]["Severity"] == "critical"
        assert df.iloc[1]["Root Cause"] == "OOMKilled: memory limit"

    def test_empty_events_empty_frame(self) -> None:
        df = dash.build_dataframe([])
        assert list(df.columns) == ["Timestamp", "Namespace", "Severity", "Root Cause"]
        assert df.empty


class TestApprovals:
    def test_only_requires_approval_events(self, audit_file: Path) -> None:
        pending = dash.approvals_pending(dash.load_audit_events(audit_file))
        assert len(pending) == 1
        assert pending[0]["incident"]["namespace"] == "prod"
        assert pending[0]["result"]["requires_approval"] is True
