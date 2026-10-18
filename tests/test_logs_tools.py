"""Unit-тесты для src.agent.tools.logs_tools (Kubernetes API мокается)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.exceptions import ApiException

from src.agent.tools.logs_tools import LogsTools


@pytest.fixture
def tools() -> LogsTools:
    """LogsTools без реального kubeconfig: конфиг и клиент подменены моками."""
    with patch("src.agent.tools.logs_tools.kubernetes.config.load_kube_config"), \
         patch("src.agent.tools.logs_tools.kubernetes.client.CoreV1Api") as core_cls:
        instance = LogsTools(kubeconfig_path="/fake/kubeconfig")
        instance._core = core_cls.return_value
        yield instance


class TestGetPodLogs:
    def test_success(self, tools: LogsTools) -> None:
        """Успешный вызов: возвращает текст лога и передаёт корректные параметры."""
        tools._core.read_namespaced_pod_log.return_value = "line1\nERROR line2"

        logs = tools.get_pod_logs(namespace="default", pod_name="web-1", tail_lines=50)

        assert logs == "line1\nERROR line2"
        tools._core.read_namespaced_pod_log.assert_called_once_with(
            name="web-1",
            namespace="default",
            timestamps=True,
            tail_lines=50,
        )

    def test_api_exception_returns_empty_string(self, tools: LogsTools) -> None:
        """ApiException логируется и не роняет метод — возвращается ''."""
        tools._core.read_namespaced_pod_log.side_effect = ApiException(
            status=404, reason="pod not found"
        )

        with patch("src.agent.tools.logs_tools.logger.error") as log_err:
            logs = tools.get_pod_logs(namespace="default", pod_name="ghost")

        assert logs == ""
        log_err.assert_called_once()

    def test_previous_flag_passed(self, tools: LogsTools) -> None:
        """previous=True прокидывается в API (логи до рестарта контейнера)."""
        tools._core.read_namespaced_pod_log.return_value = "old crash log"

        logs = tools.get_pod_logs("default", "web-1", previous=True)

        assert logs == "old crash log"
        kwargs = tools._core.read_namespaced_pod_log.call_args.kwargs
        assert kwargs["previous"] is True


class TestGetContainerLogs:
    def test_success(self, tools: LogsTools) -> None:
        """Успешное чтение логов конкретного контейнера."""
        tools._core.read_namespaced_pod_log.return_value = "sidecar log line"

        logs = tools.get_container_logs(
            namespace="prod", pod_name="api-1", container="envoy", tail_lines=10
        )

        assert logs == "sidecar log line"
        tools._core.read_namespaced_pod_log.assert_called_once_with(
            name="api-1",
            namespace="prod",
            container="envoy",
            timestamps=True,
            tail_lines=10,
        )

    def test_api_exception_returns_empty_string(self, tools: LogsTools) -> None:
        """ApiException при чтении контейнера -> '' с логированием."""
        tools._core.read_namespaced_pod_log.side_effect = ApiException(
            status=400, reason="container not valid"
        )

        with patch("src.agent.tools.logs_tools.logger.error") as log_err:
            logs = tools.get_container_logs("prod", "api-1", "nonexistent")

        assert logs == ""
        log_err.assert_called_once()


class TestSearchErrors:
    def test_filters_error_lines(self, tools: LogsTools) -> None:
        """Эвристика находит строки с ошибками независимо от регистра."""
        logs = "info: started\nERROR boom\nfine\nTraceback (most recent call last):"

        errors = LogsTools.search_errors(logs)

        assert errors == ["ERROR boom", "Traceback (most recent call last):"]

    def test_empty_input(self, tools: LogsTools) -> None:
        """Пустой вход -> пустой список."""
        assert LogsTools.search_errors("") == []


class TestDegradedModeWithoutKubeconfig:
    """Нет kubeconfig — чтение логов деградирует до пустой строки без исключений."""

    def test_init_does_not_raise_and_methods_return_empty(self) -> None:
        with patch(
            "src.agent.tools.logs_tools.kubernetes.config.load_kube_config",
            side_effect=Exception("kubeconfig not found"),
        ):
            degraded = LogsTools(kubeconfig_path="/nonexistent/config")

        assert degraded.available is False
        assert degraded.get_pod_logs(namespace="default", pod_name="web-1") == ""
        assert (
            degraded.get_container_logs(namespace="default", pod_name="web-1", container="app")
            == ""
        )


class TestInClusterConfigFallback:
    def test_incluster_used_when_kubeconfig_missing(self) -> None:
        with patch(
            "src.agent.tools.logs_tools.kubernetes.config.load_kube_config",
            side_effect=Exception("no kubeconfig"),
        ), patch(
            "src.agent.tools.logs_tools.kubernetes.config.load_incluster_config",
        ) as incluster, patch(
            "src.agent.tools.logs_tools.kubernetes.client.CoreV1Api"
        ) as core_cls:
            tools = LogsTools(kubeconfig_path="/nonexistent")
        incluster.assert_called_once()
        assert tools.available is True
        assert tools._core is core_cls.return_value
