"""Тесты security-политик (src/agent/policies.py) и Slack-аппрува оркестратора."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.agent.orchestrator import IncidentOrchestrator, LLMClientStub
from src.agent.policies import DANGEROUS_PATTERNS, check_policy
from src.config import Settings


class TestCheckPolicy:
    @pytest.mark.parametrize(
        "fix",
        [
            "kubectl delete pod bad-pod -n default",
            "DROP TABLE payments;",
            "rm -rf /var/lib/data",
            "kubectl scale 0 deployment web to drain the node",
            "Delete the PVC and recreate it",
        ],
    )
    def test_dangerous_fixes_require_approval(self, fix: str) -> None:
        assert check_policy(fix, Settings(_env_file=None)) is True

    @pytest.mark.parametrize(
        "fix",
        [
            "Increase the container memory limit to 1Gi and roll the deployment",
            "Fix the image tag in the Deployment spec to nginx:1.27",
            "Add a readiness probe and cap the batch size via PAYMENTS_BATCH_MB=128",
            "",
        ],
    )
    def test_safe_fixes_do_not_require_approval(self, fix: str) -> None:
        assert check_policy(fix, Settings(_env_file=None)) is False

    def test_case_insensitive(self) -> None:
        assert check_policy("DeLeTe pod x", Settings(_env_file=None)) is True

    def test_write_actions_enabled_disables_policy(self) -> None:
        """По спеке: аппрув требуется в read-only режиме (ENABLE_WRITE_ACTIONS=false)."""
        settings = Settings(_env_file=None, enable_write_actions=True)
        assert check_policy("kubectl delete pod x", settings) is False

    def test_patterns_constant_is_spec_list(self) -> None:
        assert DANGEROUS_PATTERNS == ("delete", "drop", "rm -rf", "scale 0")


def _orchestrator_with_fix(suggested_fix: str, slack_url: str | None) -> IncidentOrchestrator:
    """Оркестратор с моками инструментов и LLM, возвращающим заданный fix."""
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "root_cause": "test cause",
            "evidence": ["e1"],
            "suggested_fix": suggested_fix,
            "severity": "high",
        }
    )
    return IncidentOrchestrator(
        k8s_tools=MagicMock(get_pods=MagicMock(return_value=[]), get_events=MagicMock(return_value=[])),
        prometheus_tools=MagicMock(
            get_pod_cpu_usage=MagicMock(return_value=None),
            get_pod_memory_usage=MagicMock(return_value=None),
        ),
        logs_tools=MagicMock(get_pod_logs=MagicMock(return_value="")),
        llm_client=llm,
        settings=Settings(_env_file=None, slack_webhook_url=slack_url),
        rag=MagicMock(find_relevant=MagicMock(return_value=[])),
    )


class TestOrchestratorApprovalFlow:
    def test_dangerous_fix_sets_flag_and_notifies_slack(self, monkeypatch) -> None:
        import src.agent.orchestrator as orch_module

        post = MagicMock(return_value=MagicMock(status_code=200, raise_for_status=lambda: None))
        monkeypatch.setattr(orch_module.requests, "post", post)
        orch = _orchestrator_with_fix(
            "kubectl delete pod bad-pod -n default", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        post.assert_called_once()
        args, kwargs = post.call_args
        assert args[0] == "https://hooks.slack.com/services/T/B/X"
        assert "⚠️ Action Required: Agent suggests dangerous fix:" in kwargs["json"]["text"]
        assert "Approve or Reject?" in kwargs["json"]["text"]

    def test_no_webhook_logs_warning_without_crash(self, caplog, monkeypatch) -> None:
        import src.agent.orchestrator as orch_module

        post = MagicMock()
        monkeypatch.setattr(orch_module.requests, "post", post)
        orch = _orchestrator_with_fix("kubectl delete pod bad-pod", None)
        with caplog.at_level("WARNING"):
            result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        post.assert_not_called()  # нет webhook — не падаем, только WARNING
        assert any("SLACK_WEBHOOK_URL" in rec.message for rec in caplog.records)

    def test_slack_failure_does_not_break_diagnosis(self, monkeypatch) -> None:
        import requests as requests_lib

        import src.agent.orchestrator as orch_module

        def boom(*args, **kwargs):
            raise requests_lib.ConnectionError("slack down")

        monkeypatch.setattr(orch_module.requests, "post", boom)
        orch = _orchestrator_with_fix(
            "rm -rf /tmp/x", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        assert result["root_cause"] == "test cause"

    def test_safe_fix_no_approval_no_slack(self, monkeypatch) -> None:
        import src.agent.orchestrator as orch_module

        post = MagicMock()
        monkeypatch.setattr(orch_module.requests, "post", post)
        orch = _orchestrator_with_fix(
            "Increase the memory limit to 1Gi", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is False
        post.assert_not_called()

    def test_report_mentions_approval(self) -> None:
        orch = IncidentOrchestrator(
            llm_client=LLMClientStub(), settings=Settings(_env_file=None)
        )
        report = orch.format_report(
            {"suggested_fix": "kubectl delete pod x", "requires_approval": True}
        )
        assert "Требует аппрува человека" in report


class TestApprovalConfig:
    def test_slack_webhook_default_none(self) -> None:
        assert Settings(_env_file=None).slack_webhook_url is None

    def test_slack_webhook_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
        assert Settings(_env_file=None).slack_webhook_url == "https://hooks.slack.com/services/T/B/X"
