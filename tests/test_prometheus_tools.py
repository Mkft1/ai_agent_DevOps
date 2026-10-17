"""Unit-тесты для src.agent.tools.prometheus_tools (HTTP-слой мокается)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.agent.tools.prometheus_tools import PrometheusTools


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    """Фабрика мок-ответа requests."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock(
        side_effect=(
            requests.HTTPError(f"HTTP {status_code}") if status_code >= 400 else None
        )
    )
    return resp


@pytest.fixture
def tools() -> PrometheusTools:
    """PrometheusTools с подменным Session вместо реальной сети."""
    instance = PrometheusTools(base_url="http://prom.test:9090")
    instance._session = MagicMock()
    return instance


SUCCESS_VECTOR = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {"metric": {"pod": "web-1"}, "value": [1728000000, "0.25"]},
        ],
    },
}


class TestQuery:
    def test_success(self, tools: PrometheusTools) -> None:
        """Успешный instant-запрос: возвращает data с result/value."""
        tools._session.get.return_value = _response(SUCCESS_VECTOR)

        data = tools.query("up")

        assert data["resultType"] == "vector"
        assert data["result"][0]["metric"] == {"pod": "web-1"}
        assert data["result"][0]["value"] == [1728000000, "0.25"]
        url = tools._session.get.call_args.args[0]
        params = tools._session.get.call_args.kwargs
        assert url == "http://prom.test:9090/api/v1/query"
        assert params["params"] == {"query": "up"}
        assert params["timeout"] == tools.timeout

    def test_http_error_returns_empty_dict(self, tools: PrometheusTools) -> None:
        """HTTP 500 логируется и не роняет метод — возвращается {}."""
        tools._session.get.return_value = _response({}, status_code=500)

        with patch("src.agent.tools.prometheus_tools.logger.error") as log_err:
            data = tools.query("up")

        assert data == {}
        log_err.assert_called_once()

    def test_connection_error_returns_empty_dict(self, tools: PrometheusTools) -> None:
        """Сетевая ошибка (ConnectionError) -> {} без исключения."""
        tools._session.get.side_effect = requests.exceptions.ConnectionError("down")

        with patch("src.agent.tools.prometheus_tools.logger.error"):
            assert tools.query("up") == {}


class TestQueryRange:
    def test_success(self, tools: PrometheusTools) -> None:
        """Range-запрос передаёт query/start/end/step и возвращает matrix."""
        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {}, "values": [[1, "0.1"], [2, "0.2"]]}],
            },
        }
        tools._session.get.return_value = _response(payload)

        data = tools.query_range("rate(x[5m])", start="2026-10-17T00:00:00Z", end="2026-10-17T01:00:00Z")

        params = tools._session.get.call_args.kwargs["params"]
        assert params == {
            "query": "rate(x[5m])",
            "start": "2026-10-17T00:00:00Z",
            "end": "2026-10-17T01:00:00Z",
            "step": "60s",
        }
        assert data["resultType"] == "matrix"

    def test_timeout_returns_empty_dict(self, tools: PrometheusTools) -> None:
        """Таймаут запроса -> {} с логированием."""
        tools._session.get.side_effect = requests.exceptions.Timeout("timed out")

        with patch("src.agent.tools.prometheus_tools.logger.error") as log_err:
            data = tools.query_range("up", start="a", end="b")

        assert data == {}
        log_err.assert_called_once()


class TestPodMetrics:
    def test_cpu_usage_parses_float_cores(self, tools: PrometheusTools) -> None:
        """get_pod_cpu_usage: rate(container_cpu_usage_seconds_total) -> ядра."""
        tools._session.get.return_value = _response(SUCCESS_VECTOR)

        cpu = tools.get_pod_cpu_usage(namespace="default", pod_name="web-1")

        assert cpu == pytest.approx(0.25)
        promql = tools._session.get.call_args.kwargs["params"]["query"]
        assert "container_cpu_usage_seconds_total" in promql
        assert 'namespace="default"' in promql
        assert 'pod="web-1"' in promql

    def test_memory_usage_parses_bytes(self, tools: PrometheusTools) -> None:
        """get_pod_memory_usage: container_memory_working_set_bytes -> байты."""
        payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [1728000000, "104857600"]}],
            },
        }
        tools._session.get.return_value = _response(payload)

        mem = tools.get_pod_memory_usage(namespace="prod", pod_name="api-2")

        assert mem == pytest.approx(104857600.0)
        promql = tools._session.get.call_args.kwargs["params"]["query"]
        assert "container_memory_working_set_bytes" in promql

    def test_empty_result_returns_none(self, tools: PrometheusTools) -> None:
        """Пустой vector -> None (нет данных о поде)."""
        payload = {"status": "success", "data": {"resultType": "vector", "result": []}}
        tools._session.get.return_value = _response(payload)

        with patch("src.agent.tools.prometheus_tools.logger.warning"):
            assert tools.get_pod_cpu_usage("default", "ghost") is None

    def test_error_response_returns_none(self, tools: PrometheusTools) -> None:
        """Ошибка Prometheus (status=error) -> None."""
        tools._session.get.return_value = _response(
            {"status": "error", "error": "bad PromQL", "errorType": "bad_data"}
        )

        with patch("src.agent.tools.prometheus_tools.logger.error"):
            assert tools.get_pod_memory_usage("default", "web-1") is None
