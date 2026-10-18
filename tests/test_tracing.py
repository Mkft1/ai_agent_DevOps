"""Тесты OpenTelemetry-трейсинга: провайдер, авто-инструментация, ручные спаны."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.agent.orchestrator import IncidentOrchestrator, LLMClientStub
from src.config import Settings
from src.main import app, setup_tracing


class TestTracingConfig:
    def test_export_disabled_by_default(self) -> None:
        s = Settings(_env_file=None)
        assert s.otel_exporter_otlp_endpoint is None
        assert s.otel_service_name == "ai-agent-devops"

    def test_endpoint_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        assert Settings(_env_file=None).otel_exporter_otlp_endpoint == "http://localhost:4317"

    def test_setup_tracing_returns_sdk_provider(self) -> None:
        provider = setup_tracing(Settings(_env_file=None))
        assert isinstance(provider, TracerProvider)


class TestFastAPIInstrumentation:
    def test_otel_middleware_attached(self) -> None:
        # FastAPIInstrumentor.instrument_app помечает приложение своим маркером
        # (middleware встраивается в ASGI-стек, а не в user_middleware).
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True

    def test_http_request_produces_server_span(self, span_exporter: InMemorySpanExporter) -> None:
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        span_names = {s.name for s in span_exporter.get_finished_spans()}
        assert "GET /health" in span_names


@pytest.fixture()
def span_exporter() -> InMemorySpanExporter:
    """Подключить InMemory-экспортер к глобальному SDK-провайдеру приложения."""
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)  # провайдер ставится в src/main.py
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _mock_orchestrator() -> IncidentOrchestrator:
    k8s = MagicMock()
    k8s.get_pods.return_value = [
        {"name": "web-1", "status": "Running", "restarts": 1, "node": "node-a"}
    ]
    k8s.get_events.return_value = []
    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = 0.25
    prom.get_pod_memory_usage.return_value = 130_000_000
    logs = MagicMock()
    logs.get_pod_logs.return_value = "ERROR OOM\n"
    rag = MagicMock()
    rag.find_relevant.return_value = []
    return IncidentOrchestrator(
        k8s_tools=k8s,
        prometheus_tools=prom,
        logs_tools=logs,
        llm_client=LLMClientStub(),
        settings=Settings(_env_file=None),
        rag=rag,
    )


class TestManualSpans:
    def test_diagnose_produces_tool_and_llm_spans(self, span_exporter: InMemorySpanExporter) -> None:
        orch = _mock_orchestrator()
        result = orch.diagnose({"namespace": "default", "pod_name": "web-1", "description": "x"})
        assert result["severity"]

        spans = {s.name: s for s in span_exporter.get_finished_spans()}
        expected = {
            "incident.diagnose",
            "k8s.get_pods",
            "k8s.get_events",
            "prometheus.pod_cpu",
            "prometheus.pod_memory",
            "logs.pod_tail",
            "rag.search",
            "llm.generate",
        }
        assert expected <= set(spans)

        # Атрибуты: модель LLM и namespace инцидента видны в трейсе.
        assert spans["llm.generate"].attributes["llm.model"] == "qwen-max"
        assert spans["incident.diagnose"].attributes["incident.namespace"] == "default"
        assert spans["k8s.get_pods"].attributes["k8s.pods_found"] == 1

    def test_tool_failure_recorded_on_span(self, span_exporter: InMemorySpanExporter) -> None:
        orch = _mock_orchestrator()
        orch.k8s.get_pods.side_effect = RuntimeError("k8s api down")
        orch.diagnose({"namespace": "default", "pod_name": "web-1"})
        spans = {s.name: s for s in span_exporter.get_finished_spans()}
        pod_span = spans["k8s.get_pods"]
        assert pod_span.status.status_code.name == "ERROR"
        assert pod_span.events  # record_exception добавил span event
