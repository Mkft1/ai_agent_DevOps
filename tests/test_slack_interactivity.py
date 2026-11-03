"""Тесты Slack Interactive Buttons: Block Kit-аппрув + /webhook/slack-interactivity.

Покрыто:
    * структура Block Kit-сообщения (секция контекста + кнопки Approve/Reject
      c value ``approve:<incident_id>`` / ``reject:<incident_id>``);
    * поле ``incident_id`` модели ``Diagnosis`` (формат, уникальность);
    * эндпоинт ``POST /webhook/slack-interactivity``: approve/reject -> 200 +
      журнал аудита, ошибки парсинга -> 400;
    * валидация ``X-Slack-Signature`` (HMAC-SHA256 v0 + replay-окно 5 минут):
      корректная подпись -> 200, неверная/отсутствующая/устаревшая -> 401;
    * dev-режим без ``SLACK_SIGNING_SECRET`` -> 200 + WARNING;
    * корреляция incident_id в аудите диагностики.

Транспорт Slack-уведомления мокается подменой ``httpx.AsyncClient``
(оркестратор использует httpx, а не requests — мокаем фактический транспорт).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from src.agent.orchestrator import Diagnosis, IncidentOrchestrator
from src.config import Settings
from src.main import _verify_slack_signature, app, get_orchestrator

SLACK_URL = "https://hooks.slack.com/services/T000/B000/XXXX"
SIGNING_SECRET = "test-signing-secret-8f2b4c1d"
INCIDENT_ID = "abc123def456"
DANGEROUS_FIX = "kubectl delete pod payments-api-7d9f4b-x2kq -n default"


# ----------------------------------------------------------------------
# Фейковый Slack-транспорт и оркестратор с заданным suggested_fix.
# ----------------------------------------------------------------------
class FakeSlackClient:
    """Мини-замена httpx.AsyncClient: записывает (url, payload)."""

    calls: list[tuple[str, dict]] = []
    fail_with: Exception | None = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        if FakeSlackClient.fail_with is not None:
            raise FakeSlackClient.fail_with
        FakeSlackClient.calls.append((url, json))
        return MagicMock(status_code=200, raise_for_status=lambda: None)


@pytest.fixture()
def fake_slack(monkeypatch: pytest.MonkeyPatch):
    import src.agent.orchestrator as orch_module

    FakeSlackClient.calls = []
    FakeSlackClient.fail_with = None
    monkeypatch.setattr(orch_module.httpx, "AsyncClient", FakeSlackClient)
    return FakeSlackClient


def _orchestrator_with_fix(suggested_fix: str, slack_url: str | None = SLACK_URL):
    """Оркестратор с моками инструментов и LLM, возвращающим заданный fix."""
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "root_cause": "Container OOMKilled",
            "evidence": ["restarts=5"],
            "suggested_fix": suggested_fix,
            "severity": "high",
        }
    )
    return IncidentOrchestrator(
        k8s_tools=MagicMock(
            get_pods=MagicMock(return_value=[]), get_events=MagicMock(return_value=[])
        ),
        prometheus_tools=MagicMock(
            get_pod_cpu_usage=MagicMock(return_value=None),
            get_pod_memory_usage=MagicMock(return_value=None),
        ),
        logs_tools=MagicMock(get_pod_logs=MagicMock(return_value="")),
        llm_client=llm,
        settings=Settings(_env_file=None, slack_webhook_url=slack_url),
        rag=MagicMock(find_relevant=MagicMock(return_value=[])),
    )


# ----------------------------------------------------------------------
# Хелперы payload'а Slack Interactive Components.
# ----------------------------------------------------------------------
def slack_payload(action_value: str = f"approve:{INCIDENT_ID}", username: str = "sre-ivan") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": "U0123", "username": username},
        "channel": {"id": "C42"},
        "message": {"ts": "1700000000.000100"},
        "actions": [
            {"action_id": "a1", "block_id": "b1", "type": "button", "value": action_value}
        ],
    }


def form_body(payload: dict) -> bytes:
    """Тело запроса Slack: application/x-www-form-urlencoded, поле payload."""
    return urlencode({"payload": json.dumps(payload)}).encode()


def slack_headers(
    body: bytes, secret: str = SIGNING_SECRET, ts: int | None = None, signature: str | None = None
) -> dict[str, str]:
    """Заголовки X-Slack-Request-Timestamp / X-Slack-Signature (алгоритм Slack)."""
    ts_value = ts if ts is not None else int(time.time())
    if signature is None:
        base = f"v0:{ts_value}:{body.decode()}"
        signature = "v0=" + hmac.new(
            secret.encode(), base.encode(), hashlib.sha256
        ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(ts_value),
        "X-Slack-Signature": signature,
    }


def read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ----------------------------------------------------------------------
# Фикстуры HTTP-слоя.
# ----------------------------------------------------------------------
@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_limiter():
    from src.main import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def signing_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SLACK_SIGNING_SECRET задан; аудит пишется в tmp-файл."""
    import src.config as config_module

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    config_module.get_settings.cache_clear()
    yield audit_path
    config_module.get_settings.cache_clear()


@pytest.fixture()
def signing_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Секрета нет (dev-режим); аудит пишётся в tmp-файл."""
    import src.config as config_module

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    config_module.get_settings.cache_clear()
    yield audit_path
    config_module.get_settings.cache_clear()


# ----------------------------------------------------------------------
# Часть 1: Block Kit-уведомление из оркестратора.
# ----------------------------------------------------------------------
class TestBlockKitNotification:
    def test_payload_has_section_and_action_buttons(self, fake_slack) -> None:
        orch = _orchestrator_with_fix(DANGEROUS_FIX)
        result = orch.diagnose({"namespace": "default", "pod_name": "payments-api-7d9f4b-x2kq"})
        assert result["requires_approval"] is True
        assert len(fake_slack.calls) == 1
        url, payload = fake_slack.calls[0]
        assert url == SLACK_URL

        # Fallback-текст (push-уведомления + обратная совместимость формата).
        assert "⚠️ Action Required: Agent suggests dangerous fix:" in payload["text"]
        assert "Approve or Reject?" in payload["text"]

        blocks = payload["blocks"]
        assert [b["type"] for b in blocks] == ["section", "actions"]

        section = blocks[0]["text"]
        assert section["type"] == "mrkdwn"
        for expected in (
            "⚠️ *Dangerous fix detected*",
            "*Namespace:* `default`",
            "*Pod:* `payments-api-7d9f4b-x2kq`",
            "*Root cause:* Container OOMKilled",
            f"*Suggested fix:* `{DANGEROUS_FIX}`",
            f"*Incident ID:* `{result['incident_id']}`",
        ):
            assert expected in section["text"]

        approve, reject = blocks[1]["elements"]
        assert approve["type"] == "button"
        assert approve["text"] == {"type": "plain_text", "text": "✅ Approve"}
        assert approve["style"] == "primary"
        assert approve["value"] == f"approve:{result['incident_id']}"
        assert reject["type"] == "button"
        assert reject["text"] == {"type": "plain_text", "text": "❌ Reject"}
        assert reject["style"] == "danger"
        assert reject["value"] == f"reject:{result['incident_id']}"

    def test_incident_id_is_12_hex_chars_and_unique(self) -> None:
        first, second = Diagnosis(), Diagnosis()
        for diagnosis in (first, second):
            assert len(diagnosis.incident_id) == 12
            int(diagnosis.incident_id, 16)  # валидный hex
        assert first.incident_id != second.incident_id

    def test_explicit_incident_id_survives_roundtrip(self) -> None:
        diagnosis = Diagnosis.model_validate({"incident_id": "fixed0abcdef", "root_cause": "x"})
        assert diagnosis.incident_id == "fixed0abcdef"
        assert diagnosis.model_dump()["incident_id"] == "fixed0abcdef"

    def test_missing_pod_shows_placeholder(self, fake_slack) -> None:
        orch = _orchestrator_with_fix(DANGEROUS_FIX)
        orch.diagnose({"namespace": "default"})
        _, payload = fake_slack.calls[0]
        assert "*Pod:* `n/a`" in payload["blocks"][0]["text"]["text"]

    def test_safe_fix_sends_no_slack_message(self, fake_slack) -> None:
        orch = _orchestrator_with_fix("increase memory limits to 512Mi")
        result = orch.diagnose({"namespace": "default"})
        assert result["requires_approval"] is False
        assert fake_slack.calls == []

    def test_slack_outage_does_not_break_diagnosis(self, fake_slack) -> None:
        fake_slack.fail_with = RuntimeError("slack down")
        orch = _orchestrator_with_fix(DANGEROUS_FIX)
        result = orch.diagnose({"namespace": "default"})
        assert result["requires_approval"] is True
        assert result["root_cause"] == "Container OOMKilled"


# ----------------------------------------------------------------------
# Часть 2: эндпоинт /webhook/slack-interactivity.
# ----------------------------------------------------------------------
class TestSlackInteractivityEndpoint:
    def test_approve_returns_200_and_writes_audit(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload(f"approve:{INCIDENT_ID}"))
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 200
        assert resp.text == "Approved"
        events = [e for e in read_audit(signing_enabled) if e["event_type"] == "slack_approve"]
        assert len(events) == 1
        assert events[0]["incident"]["incident_id"] == INCIDENT_ID
        assert events[0]["incident"]["action"] == "approve"
        assert events[0]["incident"]["slack_user"] == "sre-ivan"
        assert events[0]["incident"]["channel"] == "C42"

    def test_reject_returns_200_and_writes_audit(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload(f"reject:{INCIDENT_ID}"))
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 200
        assert resp.text == "Rejected"
        events = [e for e in read_audit(signing_enabled) if e["event_type"] == "slack_reject"]
        assert len(events) == 1
        assert events[0]["incident"]["incident_id"] == INCIDENT_ID

    def test_missing_payload_field_returns_400(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = b"notpayload=1"
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 400
        assert "payload" in resp.json()["detail"]

    def test_malformed_json_returns_400(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = urlencode({"payload": "{broken json"}).encode()
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 400

    def test_no_actions_returns_400(self, client: TestClient, signing_enabled: Path) -> None:
        payload = slack_payload()
        payload["actions"] = []
        body = form_body(payload)
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 400

    def test_unknown_action_returns_400(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload(f"maybe:{INCIDENT_ID}"))
        resp = client.post(
            "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
        )
        assert resp.status_code == 400
        assert read_audit(signing_enabled) == []  # ничего не залогировано


class TestSlackSignatureValidation:
    def test_missing_signature_returns_401(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload())
        resp = client.post("/webhook/slack-interactivity", content=body)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid Slack signature"

    def test_wrong_signature_returns_401(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload())
        headers = slack_headers(body, signature="v0=" + "0" * 64)
        resp = client.post("/webhook/slack-interactivity", content=body, headers=headers)
        assert resp.status_code == 401

    def test_signature_of_other_secret_returns_401(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        body = form_body(slack_payload())
        headers = slack_headers(body, secret="attacker-secret")
        resp = client.post("/webhook/slack-interactivity", content=body, headers=headers)
        assert resp.status_code == 401

    def test_stale_timestamp_returns_401(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        """Replay-защита: подпись верна, но timestamp старше 5 минут."""
        body = form_body(slack_payload())
        headers = slack_headers(body, ts=int(time.time()) - 600)
        resp = client.post("/webhook/slack-interactivity", content=body, headers=headers)
        assert resp.status_code == 401

    def test_tampered_body_invalidates_signature(
        self, client: TestClient, signing_enabled: Path
    ) -> None:
        signed_body = form_body(slack_payload(f"reject:{INCIDENT_ID}"))
        headers = slack_headers(signed_body)
        tampered_body = form_body(slack_payload(f"approve:{INCIDENT_ID}"))
        resp = client.post(
            "/webhook/slack-interactivity", content=tampered_body, headers=headers
        )
        assert resp.status_code == 401

    def test_dev_mode_without_secret_accepts_and_warns(
        self, client: TestClient, signing_disabled: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        body = form_body(slack_payload(f"approve:{INCIDENT_ID}"))
        with caplog.at_level(logging.WARNING):
            resp = client.post("/webhook/slack-interactivity", content=body)
        assert resp.status_code == 200
        assert resp.text == "Approved"
        assert "SLACK_SIGNING_SECRET is not set" in caplog.text
        assert len(read_audit(signing_disabled)) == 1

    def test_secret_never_appears_in_logs(
        self, client: TestClient, signing_enabled: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        body = form_body(slack_payload())
        with caplog.at_level(logging.DEBUG):
            client.post("/webhook/slack-interactivity", content=body)  # 401, без заголовков
            client.post(
                "/webhook/slack-interactivity", content=body, headers=slack_headers(body)
            )
        assert SIGNING_SECRET not in caplog.text


class TestVerifySlackSignatureUnit:
    def test_valid_signature(self) -> None:
        body = b"payload=%7B%7D"
        ts = str(int(time.time()))
        headers = slack_headers(body, ts=int(ts))
        assert _verify_slack_signature(body, ts, headers["X-Slack-Signature"], SIGNING_SECRET)

    def test_non_numeric_timestamp(self) -> None:
        assert not _verify_slack_signature(b"x", "not-a-number", "v0=abc", SIGNING_SECRET)

    def test_empty_timestamp(self) -> None:
        assert not _verify_slack_signature(b"x", "", "v0=abc", SIGNING_SECRET)

    def test_wrong_version_prefix(self) -> None:
        ts = str(int(time.time()))
        body = b"payload=1"
        digest = hmac.new(
            SIGNING_SECRET.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256
        ).hexdigest()
        assert not _verify_slack_signature(body, ts, f"v1={digest}", SIGNING_SECRET)


class TestSlackConfig:
    def test_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.slack_signing_secret is None
        assert s.slack_app_token is None

    def test_values_from_kwargs(self) -> None:
        s = Settings(
            _env_file=None, slack_signing_secret="sec", slack_app_token="xapp-1-test"
        )
        assert s.slack_signing_secret == "sec"
        assert s.slack_app_token == "xapp-1-test"


class TestAuditCorrelation:
    def test_diagnose_audit_record_contains_incident_id(
        self, client: TestClient, signing_disabled: Path
    ) -> None:
        """incident_id из диагноза попадает в журнал (корреляция с slack_approve)."""
        orch = MagicMock(spec=IncidentOrchestrator)
        orch.adiagnose.return_value = {
            "incident_id": "fixed0abcdef",
            "root_cause": "oom",
            "evidence": [],
            "suggested_fix": "raise limits",
            "severity": "high",
            "raw_data": {},
            "warnings": [],
            "llm_response": "{}",
        }
        orch.format_report.return_value = "[r]"
        app.dependency_overrides[get_orchestrator] = lambda: orch
        try:
            resp = client.post("/diagnose", json={"namespace": "default", "description": "x"})
            assert resp.status_code == 200
            assert resp.json()["diagnosis"]["incident_id"] == "fixed0abcdef"
            events = [e for e in read_audit(signing_disabled) if e["event_type"] == "diagnose"]
            assert len(events) == 1
            assert events[0]["result"]["incident_id"] == "fixed0abcdef"
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)
