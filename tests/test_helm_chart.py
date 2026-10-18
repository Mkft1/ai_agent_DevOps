"""Статические тесты Helm-чарта charts/ai-agent (helm не требуется)."""

from __future__ import annotations

from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "charts" / "ai-agent"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestChartMetadata:
    def test_chart_yaml(self) -> None:
        chart = _yaml(CHART / "Chart.yaml")
        assert chart["apiVersion"] == "v2"
        assert chart["name"] == "ai-agent"
        assert chart["version"] == "0.1.0"
        assert chart["type"] == "application"
        assert chart["description"]
        assert chart["kubeVersion"]

    def test_values_cover_spec(self) -> None:
        values = _yaml(CHART / "values.yaml")
        assert values["replicaCount"] >= 1
        assert values["image"]["repository"]
        assert values["resources"]["requests"] and values["resources"]["limits"]
        # env из config.py, включая PROMETHEUS_URL и LLM-ключи в secrets
        assert "PROMETHEUS_URL" in values["env"]
        assert "LOG_LEVEL" in values["env"]
        assert "llmApiKey" in values["secrets"]
        assert values["podSecurityContext"]["runAsNonRoot"] is True


class TestTemplates:
    def test_required_templates_exist(self) -> None:
        names = {p.name for p in (CHART / "templates").iterdir()}
        assert {
            "deployment.yaml",
            "service.yaml",
            "serviceaccount.yaml",
            "rbac.yaml",
            "configmap.yaml",
            "secret.yaml",
            "_helpers.tpl",
            "NOTES.txt",
        } <= names

    def test_deployment_probes_and_security(self) -> None:
        dep = (CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
        values = _yaml(CHART / "values.yaml")
        # probes берут пути из values: liveness=/health, readiness=/ready
        assert ".Values.probes.liveness.path" in dep
        assert ".Values.probes.readiness.path" in dep
        assert values["probes"]["liveness"]["path"] == "/health"
        assert values["probes"]["readiness"]["path"] == "/ready"
        # securityContext'ы приходят из values (toYaml) — проверяем связку
        assert ".Values.podSecurityContext" in dep
        assert ".Values.securityContext" in dep
        assert values["podSecurityContext"]["runAsNonRoot"] is True
        assert values["securityContext"]["readOnlyRootFilesystem"] is True
        assert values["securityContext"]["allowPrivilegeEscalation"] is False

    def test_service_is_clusterip(self) -> None:
        svc = (CHART / "templates" / "service.yaml").read_text(encoding="utf-8")
        assert "{{ .Values.service.type }}" in svc
        assert _yaml(CHART / "values.yaml")["service"]["type"] == "ClusterIP"

    def test_rbac_read_only(self) -> None:
        values = _yaml(CHART / "values.yaml")
        for rule in values["rbac"]["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}
