"""Unit-тесты для src.agent.tools.k8s_tools (клиент Kubernetes мокается)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kubernetes.client.exceptions import ApiException

from src.agent.tools.k8s_tools import K8sTools


@pytest.fixture
def tools() -> K8sTools:
    """K8sTools без реального kubeconfig: конфиг и клиенты подменены моками."""
    with patch("src.agent.tools.k8s_tools.kubernetes.config.load_kube_config") as load_cfg, \
         patch("src.agent.tools.k8s_tools.kubernetes.client.CoreV1Api") as core_cls, \
         patch("src.agent.tools.k8s_tools.kubernetes.client.AppsV1Api") as apps_cls:
        instance = K8sTools(kubeconfig_path="/fake/kubeconfig")
        instance._core = core_cls.return_value
        instance._apps = apps_cls.return_value
        yield instance


def _pod() -> SimpleNamespace:
    from datetime import datetime, timezone

    return SimpleNamespace(
        metadata=SimpleNamespace(name="web-1", labels={"app": "web"}),
        spec=SimpleNamespace(node_name="node-a"),
        status=SimpleNamespace(
            phase="Running",
            start_time=datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc),
            container_statuses=[SimpleNamespace(restart_count=2, state=None)],
        ),
    )


class TestGetPods:
    def test_success(self, tools: K8sTools) -> None:
        """Успешный вызов: поля соответствуют контракту."""
        tools._core.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod()])

        pods = tools.get_pods(namespace="default")

        assert len(pods) == 1
        pod = pods[0]
        assert pod["name"] == "web-1"
        assert pod["status"] == "Running"
        assert pod["restarts"] == 2
        assert pod["node"] == "node-a"
        assert pod["labels"] == {"app": "web"}
        assert isinstance(pod["age"], float)
        tools._core.list_namespaced_pod.assert_called_once_with(namespace="default")

    def test_api_exception_returns_empty_list(self, tools: K8sTools) -> None:
        """ApiException логируется и не роняет метод — возвращается []."""
        tools._core.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

        with patch("src.agent.tools.k8s_tools.logger.error") as log_err:
            pods = tools.get_pods(namespace="prod")

        assert pods == []
        log_err.assert_called_once()


class TestGetEvents:
    def test_success_sorted_and_limited(self, tools: K8sTools) -> None:
        """События сортируются по времени (новые первыми) и ограничиваются limit."""
        from datetime import datetime, timezone

        def ev(i: int) -> SimpleNamespace:
            return SimpleNamespace(
                type="Warning",
                reason="BackOff",
                message=f"msg-{i}",
                involved_object=SimpleNamespace(kind="Pod", name=f"p-{i}"),
                count=i,
                last_timestamp=datetime(2026, 10, 17, tzinfo=timezone.utc).replace(minute=i),
                event_time=None,
                first_timestamp=None,
            )

        items = [ev(1), ev(5), ev(3)]
        tools._core.list_namespaced_event.return_value = SimpleNamespace(items=items)

        events = tools.get_events(namespace="default", limit=2)

        assert len(events) == 2
        assert events[0]["message"] == "msg-5"  # самый свежий первым
        assert events[0]["involved_object"] == "Pod/p-5"
        assert events[0]["count"] == 5
        assert set(events[0]) == {"type", "reason", "message", "involved_object", "count", "last_timestamp"}

    def test_api_exception_returns_empty_list(self, tools: K8sTools) -> None:
        tools._core.list_namespaced_event.side_effect = ApiException(status=500, reason="Internal")

        assert tools.get_events() == []


class TestGetDeployments:
    def test_success(self, tools: K8sTools) -> None:
        dep = SimpleNamespace(
            metadata=SimpleNamespace(name="api"),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[SimpleNamespace(image="nginx:1.25")])
                )
            ),
            status=SimpleNamespace(replicas=3, ready_replicas=2, updated_replicas=3, available_replicas=2),
        )
        tools._apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[dep])

        deps = tools.get_deployments(namespace="default")

        assert deps == [
            {
                "name": "api",
                "replicas": 3,
                "ready_replicas": 2,
                "updated_replicas": 3,
                "available_replicas": 2,
                "image": "nginx:1.25",
            }
        ]

    def test_api_exception_returns_empty_list(self, tools: K8sTools) -> None:
        tools._apps.list_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")

        assert tools.get_deployments() == []


class TestGetNodes:
    def test_success(self, tools: K8sTools) -> None:
        node = SimpleNamespace(
            metadata=SimpleNamespace(
                name="node-a",
                labels={"node-role.kubernetes.io/control-plane": ""},
            ),
            status=SimpleNamespace(
                conditions=[
                    SimpleNamespace(type="Ready", status="True"),
                    SimpleNamespace(type="MemoryPressure", status="False"),
                ],
                capacity={"cpu": "8", "memory": "16Gi"},
                allocatable={},
            ),
        )
        tools._core.list_node.return_value = SimpleNamespace(items=[node])

        nodes = tools.get_nodes()

        assert nodes[0]["name"] == "node-a"
        assert nodes[0]["status"] == "Ready"
        assert nodes[0]["roles"] == ["control-plane"]
        assert nodes[0]["cpu_capacity"] == "8"
        assert nodes[0]["memory_capacity"] == "16Gi"
        assert {"type": "Ready", "status": "True"} in nodes[0]["conditions"]

    def test_api_exception_returns_empty_list(self, tools: K8sTools) -> None:
        tools._core.list_node.side_effect = ApiException(status=403, reason="Forbidden")

        assert tools.get_nodes() == []


def test_init_loads_kubeconfig_from_settings() -> None:
    """Пусть kubeconfig берётся из config.Settings, ~ раскрывается."""
    with patch("src.agent.tools.k8s_tools.kubernetes.config.load_kube_config") as load_cfg, \
         patch("src.agent.tools.k8s_tools.kubernetes.client.CoreV1Api"), \
         patch("src.agent.tools.k8s_tools.kubernetes.client.AppsV1Api"):
        K8sTools(kubeconfig_path="~/custom-config")

    load_cfg.assert_called_once()
    assert load_cfg.call_args.kwargs["config_file"].endswith("custom-config")
    assert "~" not in load_cfg.call_args.kwargs["config_file"]
