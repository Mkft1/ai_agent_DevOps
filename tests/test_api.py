"""Тесты HTTP-API (src/main.py) через fastapi.testclient.TestClient.

Оркестратор и внешние проверки моканируются — реальных обращений к
Kubernetes / Prometheus / LLM нет.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.agent.orchestrator import (
    Diagnosis,
    IncidentOrchestrator,
    NamespaceNotAllowedError,
)
from src.main import app, get_orchestrator

SAMPLE_DIAGNOSIS = {
    "root_cause": "Pod OOMKilled: лимит памяти 128Mi недостаточен",
    "evidence": ["restarts=5", "memory_bytes ~ 130_000_000"],
    "suggested_fix": "Увеличить resources.limits.memory до 256Mi",
    "severity": "high",
    "raw_data": {"incident": {"namespace": "prod", "pod_name": "web-1"}},
    "warnings": [],
    "llm_response": "{}",
}


@pytest.fixture()
def mock_orchestrator() -> MagicMock:
    """Заменить реальный оркестратор моком через dependency override."""
    orch = MagicMock(spec=IncidentOrchestrator)
    orch.diagnose.return_value = dict(SAMPLE_DIAGNOSIS)
    orch.format_report.return_value = "[report] severity=high"
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield orch
    app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture()
def client() -> TestClient:
    # raise_server_exceptions=False — чтобы глобальный exception handler
    # (500 {"detail": ...}) можно было проверить как в реальном сервере.
    return TestClient(app, raise_server_exceptions=False)


class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestDiagnose:
    def test_diagnose_success(self, client: TestClient, mock_orchestrator: MagicMock) -> None:
        payload = {
            "namespace": "prod",
            "pod_name": "web-1",
            "deployment_name": None,
            "description": "Pod restarts alert",
        }
        resp = client.post("/diagnose", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        # Контракт ответа: diagnosis + report.
        assert set(body.keys()) == {"diagnosis", "report"}
        assert body["report"] == "[report] severity=high"
        diag = body["diagnosis"]
        assert diag["root_cause"] == SAMPLE_DIAGNOSIS["root_cause"]
        assert diag["severity"] == "high"
        assert diag["evidence"] == SAMPLE_DIAGNOSIS["evidence"]

        # Эндпоинт не дублирует логику — просто делегирует оркестратору.
        mock_orchestrator.diagnose.assert_called_once()
        incident = mock_orchestrator.diagnose.call_args.args[0]
        assert incident.namespace == "prod"
        assert incident.pod_name == "web-1"
        assert incident.description == "Pod restarts alert"
        mock_orchestrator.format_report.assert_called_once_with(SAMPLE_DIAGNOSIS)

    def test_diagnose_minimal_payload_defaults(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """Можно прислать только description — остальные поля по умолчанию."""
        resp = client.post("/diagnose", json={"description": "cpu high"})
        assert resp.status_code == 200
        incident = mock_orchestrator.diagnose.call_args.args[0]
        assert incident.namespace == "default"
        assert incident.pod_name is None

    def test_diagnose_invalid_body_returns_422(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """Несовместимый тип поля -> pydantic-валидация FastAPI (422), оркестратор не трогается."""
        resp = client.post("/diagnose", json={"namespace": {"bad": "dict"}})
        assert resp.status_code == 422
        mock_orchestrator.diagnose.assert_not_called()

    def test_diagnose_orchestrator_crash_returns_500(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """Исключение внутри diagnose() -> 500 с detail, приложение не падает."""
        mock_orchestrator.diagnose.side_effect = RuntimeError("LLM unavailable")
        resp = client.post("/diagnose", json={"description": "x"})
        assert resp.status_code == 500
        assert "LLM unavailable" in resp.json()["detail"]

    def test_response_model_validates_diagnosis(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """Ответ сериализуется через pydantic-модель DiagnosisResponse."""
        resp = client.post("/diagnose", json={"description": "x"})
        model = Diagnosis.model_validate(resp.json()["diagnosis"])
        assert model.severity == "high"


class TestReady:
    def test_ready_all_ok_returns_200(self, client: TestClient) -> None:
        with patch("src.main._check_kubernetes", return_value="ok"), patch(
            "src.main._check_prometheus", return_value="ok"
        ):
            resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"] == {"kubernetes": "ok", "prometheus": "ok"}

    def test_ready_prometheus_down_returns_503(self, client: TestClient) -> None:
        with patch("src.main._check_kubernetes", return_value="ok"), patch(
            "src.main._check_prometheus", return_value="unavailable: timeout"
        ):
            resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"
        assert resp.json()["checks"]["prometheus"].startswith("unavailable")

    def test_check_prometheus_http_layer(self) -> None:
        """_check_prometheus реально делает GET /-/ready и трактует код статуса."""
        from src.config import get_settings
        from src.main import _check_prometheus

        with patch("src.main.requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            assert _check_prometheus(get_settings()) == "ok"
            url = get.call_args.args[0]
            assert url.endswith("/-/ready")

            get.return_value = MagicMock(status_code=503)
            assert "HTTP 503" in _check_prometheus(get_settings())

            get.side_effect = Exception("connection refused")
            assert "unavailable" in _check_prometheus(get_settings())


class TestCORSAndHandlers:
    def test_cors_headers_allow_all_origins(self, client: TestClient) -> None:
        resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_unhandled_exception_returns_500_detail(self, client: TestClient) -> None:
        """Глобальный exception handler: любая ошибка -> 500 {"detail": str}."""
        with patch("src.main.run_health_check", side_effect=RuntimeError("boom")):
            resp = client.get("/health")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "boom"}


class TestGuardrailsAPI:
    """Guardrail на HTTP-слое: запрещённый namespace -> 403 без вызова инструментов."""

    def test_diagnose_disallowed_namespace_returns_403(self, client):
        orch = MagicMock(spec=IncidentOrchestrator)
        orch.diagnose.side_effect = NamespaceNotAllowedError(
            "Namespace 'kube-system' not allowed."
        )
        app.dependency_overrides[get_orchestrator] = lambda: orch
        try:
            resp = client.post(
                "/diagnose",
                json={"namespace": "kube-system", "description": "d"},
            )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)
        assert resp.status_code == 403
        assert "not allowed" in resp.json()["detail"]
        orch.format_report.assert_not_called()


class TestMetrics:
    """Эндпоинт /metrics: стандартные HTTP-метрики + кастомные метрики агента."""

    def test_metrics_endpoint_returns_200(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_metrics_contains_http_and_custom_metrics(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        # Сгенерировать трафик: диагностика через мок (severity=high, ns=prod).
        client.post("/diagnose", json={"namespace": "prod", "description": "cpu"})
        body = client.get("/metrics").text
        # Стандартные метрики instrumentator'а.
        assert "http_requests_total" in body
        # Кастомные метрики из src/utils/metrics.py объявлены и экспортируются.
        for name in (
            "diagnose_requests_total",
            "diagnose_duration_seconds",
            "k8s_api_errors_total",
            "llm_fallback_total",
        ):
            assert f"# {name}" in body or name in body, name

    def test_diagnose_updates_custom_counters(self, client: TestClient) -> None:
        """Реальный оркестратор (моки инструментов): diagnose() обновляет счётчики."""
        from src.agent.orchestrator import IncidentOrchestrator

        orch = IncidentOrchestrator(
            k8s_tools=MagicMock(), prometheus_tools=MagicMock(),
            logs_tools=MagicMock(), llm_client=MagicMock(),
            rag=MagicMock(),  # RAG выключён: find_relevant -> []
        )
        orch.k8s.get_pods.return_value = []
        orch.prometheus.get_pod_cpu_usage.return_value = None
        orch.prometheus.get_pod_memory_usage.return_value = None
        orch.logs.get_pod_logs.return_value = ""
        orch.llm.generate.return_value = (
            '{"root_cause": "oom", "evidence": [], '
            '"suggested_fix": "f", "severity": "high"}'
        )
        app.dependency_overrides[get_orchestrator] = lambda: orch
        try:
            before = _counter_value(client, "diagnose_requests_total")
            resp = client.post(
                "/diagnose", json={"namespace": "default", "description": "x"}
            )
            assert resp.status_code == 200
            after = _counter_value(client, "diagnose_requests_total")
            assert after > before
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

    def test_metrics_exposes_llm_fallback_counter(self, client: TestClient) -> None:
        from src.utils.metrics import inc_llm_fallback

        before = _counter_value(client, "llm_fallback_total")
        inc_llm_fallback()
        after = _counter_value(client, "llm_fallback_total")
        assert after == before + 1


def _counter_value(client: TestClient, name: str) -> float:
    """Прочитать суммарное значение Counter'а по тексту /metrics."""
    total = 0.0
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("#"):
            continue
        metric_part, _, value = line.rpartition(" ")
        base = metric_part.split("{", 1)[0]
        if base == name:
            total += float(value)
    return total
