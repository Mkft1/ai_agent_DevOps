"""Тесты security-политик (src/agent/policies.py) и Slack-аппрува оркестратора."""

from __future__ import annotations

import json
import logging
import re
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from src.agent.orchestrator import IncidentOrchestrator, LLMClientStub
from src.agent.policies import (
    CLI_DETECTED_WARNING,
    DANGEROUS_PATTERNS,
    check_policy,
    find_unsafe_cli_mention,
)
from src.config import Settings


def _policy_metric() -> float:
    """Текущее значение policy_approval_required_total (Counter без labels)."""
    return REGISTRY.get_sample_value("policy_approval_required_total") or 0.0


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
        # Уровень 2 (CLI-mention) тоже отключается в write-режиме.
        assert check_policy("helm upgrade payments ./chart", settings) is False

    def test_legacy_patterns_preserved(self) -> None:
        """Исходные 4 паттерна спеки сохранены (rm -rf/scale 0 — regex-варианты)."""
        compiled = {p.pattern for p in DANGEROUS_PATTERNS}
        assert {"delete", "drop", r"rm\s+-rf", r"scale\s+0"} <= compiled

    def test_all_patterns_are_case_insensitive_regex(self) -> None:
        assert all(p.flags & re.IGNORECASE for p in DANGEROUS_PATTERNS)


class TestExtendedPatterns:
    """Уровень 1: синонимы/перефразировки опасных действий из спеки."""

    # (regex из спеки, детектируемая фраза, фраза-промах для этого паттерна)
    @pytest.mark.parametrize(
        ("pattern", "hit", "miss"),
        [
            (r"remove\s+(the\s+)?pod", "Please remove the pod web-1 to force a restart",
             "remove the deployment annotation"),
            (r"terminate\s+(all\s+)?(instance|pod)", "terminate all instances of the service",
             "terminate the TCP connection gracefully"),
            (r"kill\s+(the\s+)?pod", "kill the pod and let the Deployment recreate it",
             "kill the long-running query"),
            (r"set\s+replicas\s+to\s+(0|zero)", "set replicas to 0 during the maintenance window",
             "set replicas to 3 for HA"),
            (r"scale\s+down\s+to\s+(0|zero)", "scale down to zero until the DB is restored",
             "scale down to 2 replicas"),
            (r"replicas\s*[:=]\s*0", "patch the deployment with replicas: 0",
             "patch the deployment with replicas: 3"),
            (r"kubectl\s+apply\s+-f", "kubectl apply -f malicious.yaml",
             "kubectl apply --dry-run=server -f patched.yaml"),
            (r"kubectl\s+create\s+-f", "kubectl create -f /tmp/privileged-pod.yaml",
             "kubectl create namespace payments"),
            (r"drop\s+table", "DROP TABLE payments;", "drop a hint into the log file"),
            (r"truncate\s+table", "TRUNCATE TABLE sessions;", "truncate the log output"),
            (r"chmod\s+777", "chmod 777 /var/lib/app", "chmod 644 /etc/app/config.yaml"),
            (r"chown\s+root", "chown root:root /opt/app/data", "chown app:app /opt/app/data"),
            (r"export\s+.*=\s*.*secret", "export DB_PASSWORD=mysecret before restarting",
             "export LOG_LEVEL=debug"),
            (r"echo\s+.*>\s*/etc/passwd", "echo 'evil::0:0::/root:/bin/sh' > /etc/passwd",
             "echo 'started' > /var/log/app.log"),
        ],
    )
    def test_spec_pattern_compiled_and_effective(
        self, pattern: str, hit: str, miss: str
    ) -> None:
        """Каждый паттерн спеки скомпилирован, ловит синоним и не перебирает."""
        assert pattern in {p.pattern for p in DANGEROUS_PATTERNS}
        compiled = re.compile(pattern, re.IGNORECASE)
        assert compiled.search(hit)
        assert not compiled.search(miss)
        assert check_policy(hit, Settings(_env_file=None)) is True

    @pytest.mark.parametrize(
        "fix",
        [
            "REMOVE POD web-1 now",  # регистронезависимость + форма без артикля
            "Terminate pod web-1 gracefully",
            "set replicas to zero while the DB is migrated",  # словесная форма
            "kubectl scale deployment/web --replicas=0",  # CLI-флаг (superset паттерна)
            "Run kubectl create -f rbac-backdoor.yaml to grant cluster-admin",
            "truncate table audit_log to free space",
            "chmod 777 on the config dir will fix the permission error",
            "chown root /etc/app then restart",
            "export API_TOKEN=$VAULT_SECRET to the container env",
            "echo 'attacker:x:0:0' >> /etc/passwd",
        ],
    )
    def test_synonym_fixes_require_approval_e2e(self, fix: str) -> None:
        assert check_policy(fix, Settings(_env_file=None)) is True


class TestCliGuardrail:
    """Уровень 2: любое упоминание kubectl/helm/oc (кроме read-only) → аппрув."""

    def test_kubectl_apply_requires_approval(self) -> None:
        assert check_policy("kubectl apply -f malicious.yaml", Settings(_env_file=None)) is True

    def test_kubectl_create_requires_approval(self) -> None:
        assert check_policy(
            "kubectl create -f /tmp/pod.yaml", Settings(_env_file=None)
        ) is True

    def test_helm_command_requires_approval(self) -> None:
        assert check_policy(
            "helm upgrade payments ./chart --set image.tag=v2", Settings(_env_file=None)
        ) is True

    def test_oc_command_requires_approval(self) -> None:
        assert check_policy(
            "oc rollout restart deployment/web", Settings(_env_file=None)
        ) is True

    def test_bare_tool_mention_requires_approval(self) -> None:
        """Упоминание без read-only-субкоманды — fail-safe: аппрув."""
        assert check_policy(
            "check the Helm chart values and fix them", Settings(_env_file=None)
        ) is True

    @pytest.mark.parametrize(
        "fix",
        [
            "kubectl rollout restart deployment/web",
            "kubectl exec -it web-1 -- sh",
            "oc adm policy add-scc-to-user privileged -z default",
        ],
    )
    def test_mutating_cli_commands_require_approval(self, fix: str) -> None:
        assert check_policy(fix, Settings(_env_file=None)) is True

    @pytest.mark.parametrize(
        "fix",
        [
            "kubectl get pods -n payments",
            "kubectl describe pod web-1",
            "kubectl logs web-1 --tail=100",
            "KUBECTL GET PODS",  # регистронезависимость
            "oc get pods -n default",
            "oc describe pod web-1",
            "helm get values payments-release",
            "Run kubectl get events, then kubectl describe the deployment",
        ],
    )
    def test_readonly_cli_commands_do_not_require_approval(self, fix: str) -> None:
        """Read-only-команды (get/describe/logs) не триггерят аппрув."""
        assert check_policy(fix, Settings(_env_file=None)) is False

    def test_mixed_safe_and_unsafe_mentions_require_approval(self) -> None:
        assert check_policy(
            "kubectl get pods first, then kubectl rollout undo deployment/web",
            Settings(_env_file=None),
        ) is True

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("kubectl rollout restart deployment/web", "kubectl"),
            ("use Helm to upgrade the release", "helm"),
            ("oc", "oc"),  # bare mention
            ("kubectl get pods", None),
            ("OC DESCRIBE POD web-1", None),
            ("no cli tools mentioned here", None),
            ("read the documentation carefully", None),  # 'oc' внутри слова — не CLI
        ],
    )
    def test_find_unsafe_cli_mention(self, text: str, expected: str | None) -> None:
        assert find_unsafe_cli_mention(text) == expected

    def test_cli_detection_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert check_policy(
                "kubectl rollout restart deployment/web", Settings(_env_file=None)
            ) is True
        assert CLI_DETECTED_WARNING in caplog.text

    def test_warning_logged_even_when_pattern_also_matched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Логируются ВСЕ случаи детекции kubectl/helm/oc, не только уровень 2."""
        with caplog.at_level(logging.WARNING):
            assert check_policy("kubectl delete pod bad-pod", Settings(_env_file=None)) is True
        assert CLI_DETECTED_WARNING in caplog.text

    def test_readonly_cli_does_not_log_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert check_policy("kubectl get pods -n payments", Settings(_env_file=None)) is False
        assert CLI_DETECTED_WARNING not in caplog.text

    def test_pattern_only_hit_does_not_log_cli_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert check_policy("rm -rf /tmp/x", Settings(_env_file=None)) is True
        assert CLI_DETECTED_WARNING not in caplog.text


class TestPolicyApprovalMetric:
    """Метрика policy_approval_required_total (src/utils/metrics.py)."""

    def test_metric_increments_when_approval_required(self) -> None:
        before = _policy_metric()
        assert check_policy("kubectl delete pod bad-pod", Settings(_env_file=None)) is True
        assert _policy_metric() == before + 1

    def test_metric_counts_both_levels(self) -> None:
        before = _policy_metric()
        assert check_policy("drop table payments;", Settings(_env_file=None)) is True  # ур. 1
        assert check_policy(  # ур. 2 (без опасного паттерна)
            "kubectl rollout restart deployment/web", Settings(_env_file=None)
        ) is True
        assert _policy_metric() == before + 2

    def test_metric_not_incremented_for_safe_fix(self) -> None:
        before = _policy_metric()
        assert check_policy("kubectl get pods -n payments", Settings(_env_file=None)) is False
        assert check_policy("Increase the memory limit to 1Gi", Settings(_env_file=None)) is False
        assert _policy_metric() == before

    def test_metric_not_incremented_when_write_actions_enabled(self) -> None:
        settings = Settings(_env_file=None, enable_write_actions=True)
        before = _policy_metric()
        assert check_policy("kubectl delete pod x", settings) is False
        assert _policy_metric() == before


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


class FakeSlackClient:
    """Мини-замена httpx.AsyncClient для тестов Slack-аппрува."""

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
def fake_slack(monkeypatch):
    import src.agent.orchestrator as orch_module

    FakeSlackClient.calls = []
    FakeSlackClient.fail_with = None
    monkeypatch.setattr(orch_module.httpx, "AsyncClient", FakeSlackClient)
    return FakeSlackClient


class TestOrchestratorApprovalFlow:
    def test_dangerous_fix_sets_flag_and_notifies_slack(self, fake_slack) -> None:
        orch = _orchestrator_with_fix(
            "kubectl delete pod bad-pod -n default", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        assert len(fake_slack.calls) == 1
        url, payload = fake_slack.calls[0]
        assert url == "https://hooks.slack.com/services/T/B/X"
        assert "⚠️ Action Required: Agent suggests dangerous fix:" in payload["text"]
        assert "Approve or Reject?" in payload["text"]

    def test_cli_only_fix_sets_flag_and_notifies_slack(self, fake_slack) -> None:
        """Уровень 2 без опасного паттерна: флаг + Slack-аппрув через оркестратор."""
        orch = _orchestrator_with_fix(
            "kubectl rollout restart deployment/web", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        assert len(fake_slack.calls) == 1

    def test_no_webhook_logs_warning_without_crash(self, caplog, fake_slack) -> None:
        orch = _orchestrator_with_fix("kubectl delete pod bad-pod", None)
        with caplog.at_level("WARNING"):
            result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        assert fake_slack.calls == []  # нет webhook — не падаем, только WARNING
        assert any("SLACK_WEBHOOK_URL" in rec.message for rec in caplog.records)

    def test_slack_failure_does_not_break_diagnosis(self, fake_slack) -> None:
        fake_slack.fail_with = ConnectionError("slack down")
        orch = _orchestrator_with_fix(
            "rm -rf /tmp/x", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is True
        assert result["root_cause"] == "test cause"

    def test_safe_fix_no_approval_no_slack(self, fake_slack) -> None:
        orch = _orchestrator_with_fix(
            "Increase the memory limit to 1Gi", "https://hooks.slack.com/services/T/B/X"
        )
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        assert result["requires_approval"] is False
        assert fake_slack.calls == []

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
