"""Тесты Kubernetes/Kyverno-манифестов (manifests/): валидный YAML + семантика."""

from __future__ import annotations

from pathlib import Path

import yaml

MANIFESTS = Path(__file__).resolve().parents[1] / "manifests"
POLICIES = MANIFESTS / "policies"


def _docs(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


class TestYamlValidity:
    def test_all_manifests_parse(self) -> None:
        files = sorted(MANIFESTS.rglob("*.yaml"))
        assert files, "manifests/ must contain yaml files"
        for f in files:
            docs = _docs(f)
            assert docs, f"{f} is empty"
            for d in docs:
                assert {"apiVersion", "kind", "metadata"} <= set(d), f"{f}: bad doc"


class TestKubeSystemPolicy:
    @staticmethod
    def _policy() -> dict:
        docs = _docs(POLICIES / "kyverno-block-kube-system.yaml")
        return docs[0]

    def test_is_enforcing_cluster_policy(self) -> None:
        p = self._policy()
        assert p["apiVersion"] == "kyverno.io/v1"
        assert p["kind"] == "ClusterPolicy"
        assert p["spec"]["validationFailureAction"] == "Enforce"

    def test_blocks_writes_in_kube_system_for_sa(self) -> None:
        rule = self._policy()["spec"]["rules"][0]
        res = rule["match"]["any"][0]["resources"]
        assert res["namespaces"] == ["kube-system"]
        assert {"CREATE", "UPDATE", "DELETE"} <= set(res["operations"])
        assert rule["match"]["any"][0]["subjects"][0]["kind"] == "ServiceAccount"
        assert "deny" in rule["validate"]

    def test_system_sa_excluded(self) -> None:
        rule = self._policy()["spec"]["rules"][0]
        excluded_ns = {
            s["namespace"]
            for sub in rule["exclude"]["any"]
            for s in sub["subjects"]
        }
        assert {"kube-system", "kube-public", "kube-node-lease"} <= excluded_ns


class TestAgentWritePolicy:
    @staticmethod
    def _policy() -> dict:
        docs = _docs(POLICIES / "kyverno-validate-actions.yaml")
        return docs[0]

    def test_matches_ai_agent_sa_writes(self) -> None:
        rule = self._policy()["spec"]["rules"][0]
        match = rule["match"]["any"][0]
        assert match["subjects"][0]["name"] == "ai-agent"
        assert match["subjects"][0]["kind"] == "ServiceAccount"
        assert {"CREATE", "UPDATE", "DELETE"} <= set(match["resources"]["operations"])

    def test_denies_unlabeled_namespaces(self) -> None:
        rule = self._policy()["spec"]["rules"][0]
        cond = rule["validate"]["deny"]["conditions"]["any"][0]
        assert "namespaceLabels" in cond["key"] and "agent-managed" in cond["key"]
        assert cond["value"] == "true"
        assert "agent-managed=true" in rule["validate"]["message"]


class TestAgentRbac:
    @staticmethod
    def _docs() -> list[dict]:
        return _docs(MANIFESTS / "agent-rbac.yaml")

    def test_service_account_ai_agent(self) -> None:
        sa = [d for d in self._docs() if d["kind"] == "ServiceAccount"][0]
        assert sa["metadata"]["name"] == "ai-agent"
        assert sa["metadata"]["namespace"] == "default"

    def test_role_is_read_only(self) -> None:
        role = [d for d in self._docs() if d["kind"] == "Role"][0]
        assert role["metadata"]["namespace"] == "default"
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}, "write verb in Role!"

    def test_rolebinding_links_sa_and_role(self) -> None:
        docs = self._docs()
        rb = [d for d in docs if d["kind"] == "RoleBinding"][0]
        role = [d for d in docs if d["kind"] == "Role"][0]
        assert rb["roleRef"]["name"] == role["metadata"]["name"]
        assert rb["subjects"][0]["name"] == "ai-agent"
        assert rb["metadata"]["namespace"] == "default"
