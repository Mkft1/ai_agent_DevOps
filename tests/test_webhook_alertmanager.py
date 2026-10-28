"""Тесты webhook'а Alertmanager и журнала аудита (src/utils/audit.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.agent.orchestrator import IncidentOrchestrator, NamespaceNotAllowedError
from src.config import Settings
from src.main import app, get_orchestrator

# Реалистичный пейлоад Alertmanager v4 (усечены только шумовые поля).
ALERTMANAGER_PAYLOAD = {
    "version": "4",
    "status": "firing",
    "groupKey": '{}/namespace="default"/alertname="PodOOMKilling"',
    "receiver": "ai-agent",
    "externalURL": "http://alertmanager.monitoring:9093",
    "commonLabels": {"severity": "critical"},
    "commonAnnotations": {},
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "PodOOMKilling",
                "namespace": "default",
                "pod": "payments-api-7d9f4b-x2kq",
                "severity": "critical",
            },
            "annotations": {
                "summary": "Pod payments-api restarts repeatedly, alert: ContainerOOMKilling",
                "description": "Container OOMKilled in pod payments-api-7d9f4b-x2kq",
            },
            "startsAt": "2026-10-19T10:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "abc123def456",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=container_memory>1",
        }
    ],
}

SAMPLE_DIAGNOSIS = {
    "root_cause": "Pod OOMKilled: лимит памяти 128Mi недостаточен",
    "evidence": ["restarts=5"],
    "suggested_fix": "Увеличить resources.limits.memory до 256Mi",
    "severity": "high",
    "raw_data": {},
    "warnings": [],
    "llm_response": "{}",
}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Сброс окон slowapi: модуль шлёт много webhook-запросов подряд."""
    from src.main import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def mock_orchestrator() -> MagicMock:
    orch = MagicMock(spec=IncidentOrchestrator)
    orch.adiagnose.return_value = dict(SAMPLE_DIAGNOSIS)
    orch.format_report.return_value = "[report] severity=high"
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield orch
    app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture()
def audit_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Журнал аудита пишется во временный файл, а не в cwd разработчика."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(
        "src.utils.audit.get_settings",
        lambda: Settings(_env_file=None, audit_log_path=str(path)),
    )
    return path


def read_audit(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestWebhookAlertmanager:
    def test_firing_alert_is_diagnosed(self, client, mock_orchestrator) -> None:
        resp = client.post("/webhook/alertmanager", json=ALERTMANAGER_PAYLOAD)
        assert resp.status_code == 200
        body = resp.json()
        assert body["processed"] == 1
        assert body["skipped"] == 0
        assert len(body["diagnoses"]) == 1
        assert body["diagnoses"][0]["diagnosis"]["root_cause"] == SAMPLE_DIAGNOSIS["root_cause"]
        assert body["diagnoses"][0]["report"] == "[report] severity=high"

        # Маппинг алерта -> Incident: namespace/pod из labels, description из summary.
        incident = mock_orchestrator.adiagnose.call_args.args[0]
        assert incident.namespace == "default"
        assert incident.pod_name == "payments-api-7d9f4b-x2kq"
        assert incident.description == ALERTMANAGER_PAYLOAD["alerts"][0]["annotations"]["summary"]

    def test_description_falls_back_to_annotation_description(self, client, mock_orchestrator) -> None:
        payload = json.loads(json.dumps(ALERTMANAGER_PAYLOAD))
        del payload["alerts"][0]["annotations"]["summary"]
        resp = client.post("/webhook/alertmanager", json=payload)
        assert resp.status_code == 200
        incident = mock_orchestrator.adiagnose.call_args.args[0]
        assert incident.description == "Container OOMKilled in pod payments-api-7d9f4b-x2kq"

    def test_no_namespace_label_defaults_and_no_pod_is_none(self, client, mock_orchestrator) -> None:
        payload = {"version": "4", "status": "firing", "alerts": [
            {"status": "firing", "labels": {"alertname": "NodeDown"},
             "annotations": {}, "startsAt": "2026-10-19T10:00:00Z"}
        ]}
        resp = client.post("/webhook/alertmanager", json=payload)
        assert resp.status_code == 200
        incident = mock_orchestrator.adiagnose.call_args.args[0]
        assert incident.namespace == "default"
        assert incident.pod_name is None
        # Без аннотаций описание строится из alertname (не пустое).
        assert "NodeDown" in incident.description

    def test_resolved_alerts_are_skipped(self, client, mock_orchestrator) -> None:
        payload = json.loads(json.dumps(ALERTMANAGER_PAYLOAD))
        payload["status"] = "resolved"
        payload["alerts"][0]["status"] = "resolved"
        resp = client.post("/webhook/alertmanager", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["processed"] == 0
        assert body["skipped"] == 1
        mock_orchestrator.adiagnose.assert_not_called()

    def test_invalid_json_returns_400(self, client, mock_orchestrator) -> None:
        resp = client.post(
            "/webhook/alertmanager",
            content=b'{"version": "4", "alerts": [',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_schema_violation_returns_400(self, client, mock_orchestrator) -> None:
        resp = client.post("/webhook/alertmanager", json={"version": "4", "alerts": "not-a-list"})
        assert resp.status_code == 400
        assert "detail" in resp.json()

    def test_diagnose_validation_still_422(self, client, mock_orchestrator) -> None:
        """400 — контракт webhook'а; /diagnose сохраняет 422 для битых тел."""
        resp = client.post("/diagnose", json={"namespace": 123})
        assert resp.status_code == 422

    def test_guardrail_rejected_alert_is_skipped_not_fatal(self, client, mock_orchestrator) -> None:
        mock_orchestrator.adiagnose.side_effect = NamespaceNotAllowedError("ns 'prod' not allowed")
        resp = client.post("/webhook/alertmanager", json=ALERTMANAGER_PAYLOAD)
        assert resp.status_code == 200
        body = resp.json()
        assert body["processed"] == 0
        assert body["skipped"] == 1


class TestAuditJournal:
    def test_diagnose_writes_audit_line(self, client, mock_orchestrator, audit_file) -> None:
        resp = client.post("/diagnose", json={"namespace": "default", "pod_name": "web-1"})
        assert resp.status_code == 200
        records = read_audit(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["event_type"] == "diagnose"
        assert rec["incident"]["namespace"] == "default"
        assert rec["incident"]["pod_name"] == "web-1"
        assert rec["result"]["severity"] == "high"
        assert rec["result"]["root_cause"] == SAMPLE_DIAGNOSIS["root_cause"]
        assert rec["ts"]

    def test_webhook_writes_audit_line(self, client, mock_orchestrator, audit_file) -> None:
        resp = client.post("/webhook/alertmanager", json=ALERTMANAGER_PAYLOAD)
        assert resp.status_code == 200
        records = read_audit(audit_file)
        assert [r["event_type"] for r in records] == ["alertmanager_diagnose"]
        assert records[0]["incident"]["pod_name"] == "payments-api-7d9f4b-x2kq"

    def test_guardrail_and_no_firing_events_are_audited(self, client, mock_orchestrator, audit_file) -> None:
        mock_orchestrator.adiagnose.side_effect = NamespaceNotAllowedError("nope")
        client.post("/webhook/alertmanager", json=ALERTMANAGER_PAYLOAD)
        resolved = json.loads(json.dumps(ALERTMANAGER_PAYLOAD))
        resolved["alerts"][0]["status"] = "resolved"
        client.post("/webhook/alertmanager", json=resolved)
        events = [r["event_type"] for r in read_audit(audit_file)]
        assert events == ["guardrail_rejected", "alertmanager_no_firing"]

    def test_audit_failure_does_not_break_request(self, client, mock_orchestrator, tmp_path, monkeypatch) -> None:
        """Ошибка записи аудита (путь недоступен) не роняет HTTP-ответ."""
        blocker = tmp_path / "blocker"
        blocker.write_text("я файл, а не директория", encoding="utf-8")
        monkeypatch.setattr(
            "src.utils.audit.get_settings",
            lambda: Settings(_env_file=None, audit_log_path=str(blocker / "audit.jsonl")),
        )
        resp = client.post("/diagnose", json={"namespace": "default", "pod_name": "web-1"})
        assert resp.status_code == 200


class TestAuditConfig:
    def test_default_audit_path(self) -> None:
        assert Settings(_env_file=None).audit_log_path == "audit.jsonl"

    def test_audit_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_LOG_PATH", "/var/log/agent/audit.jsonl")
        assert Settings(_env_file=None).audit_log_path == "/var/log/agent/audit.jsonl"
