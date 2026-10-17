"""Unit-тесты IncidentOrchestrator: все инструменты и LLM под моками."""

from unittest.mock import MagicMock


from src.agent.orchestrator import (
    Diagnosis,
    Incident,
    IncidentOrchestrator,
    LLMClientStub,
)

LLM_ANSWER = (
    '{"root_cause": "OOMKilled: контейнер упирается в memory limit", '
    '"evidence": ["restarts=5", "OOMKilling occurred"], '
    '"suggested_fix": "Поднять memory limit до 512Mi", '
    '"severity": "high"}'
)


def make_orchestrator(
    pods=None,
    events=None,
    cpu=0.25,
    mem=134_217_728,
    logs="2026-10-17 ERROR oom killed",
    llm_answer=LLM_ANSWER,
):
    """Собрать оркестратор на моках вместо реальных клиентов."""
    k8s = MagicMock()
    k8s.get_pods.return_value = pods if pods is not None else [
        {
            "name": "api-7d9f-x2kq",
            "status": "Running",
            "restarts": 5,
            "node": "worker-1",
            "age": 3600,
            "labels": {"app": "api"},
        }
    ]
    k8s.get_events.return_value = events if events is not None else [
        {
            "type": "Warning",
            "reason": "OOMKilling",
            "message": "System OOM encountered",
            "involved_object": "Pod/api-7d9f-x2kq",
            "count": 3,
            "last_timestamp": "2026-10-17T10:00:00Z",
        }
    ]

    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = cpu
    prom.get_pod_memory_usage.return_value = mem

    logs_tools = MagicMock()
    logs_tools.get_pod_logs.return_value = logs

    llm = MagicMock()
    if llm_answer is not None:
        llm.generate.return_value = llm_answer
    else:
        llm.generate.side_effect = RuntimeError("LLM unavailable")

    return (
        IncidentOrchestrator(
            k8s_tools=k8s,
            prometheus_tools=prom,
            logs_tools=logs_tools,
            llm_client=llm,
        ),
        k8s,
        prom,
        logs_tools,
        llm,
    )


class TestFullScenario:
    """Полный сценарий: pod_name задан, все инструменты отвечают."""

    def test_diagnose_returns_parsed_rca(self):
        orch, k8s, prom, logs_tools, llm = make_orchestrator()
        result = orch.diagnose(
            {
                "namespace": "prod",
                "pod_name": "api-7d9f-x2kq",
                "description": "Pod restarts alert",
            }
        )

        assert result["root_cause"].startswith("OOMKilled")
        assert result["severity"] == "high"
        assert len(result["evidence"]) == 2
        assert result["suggested_fix"] == "Поднять memory limit до 512Mi"
        assert result["warnings"] == []

        # Все шаги диагностики выполнены.
        k8s.get_pods.assert_called_once_with(namespace="prod")
        k8s.get_events.assert_called_once_with(namespace="prod")
        prom.get_pod_cpu_usage.assert_called_once_with(
            namespace="prod", pod_name="api-7d9f-x2kq"
        )
        prom.get_pod_memory_usage.assert_called_once_with(
            namespace="prod", pod_name="api-7d9f-x2kq"
        )
        logs_tools.get_pod_logs.assert_called_once_with(
            namespace="prod", pod_name="api-7d9f-x2kq", tail_lines=50
        )

        # Контекст собран и передан в LLM вместе с системным промтом.
        prompt_arg = llm.generate.call_args.kwargs["prompt"]
        assert "Ты — SRE-эксперт" in prompt_arg
        context_arg = llm.generate.call_args.kwargs["context"]
        assert context_arg["target_pod"]["restarts"] == 5
        assert context_arg["metrics"] == {
            "cpu_cores": 0.25,
            "memory_bytes": 134_217_728,
        }
        assert "oom killed" in context_arg["pod_logs_tail_50"]

        # Сырые данные возвращены для отладки.
        assert result["raw_data"]["pods"][0]["name"] == "api-7d9f-x2kq"
        assert result["llm_response"] == LLM_ANSWER

    def test_result_matches_diagnosis_model(self):
        orch, *_ = make_orchestrator()
        result = orch.diagnose({"namespace": "prod", "pod_name": "api-7d9f-x2kq"})
        Diagnosis.model_validate(result)  # не должно бросить

    def test_accepts_incident_model(self):
        orch, *_ = make_orchestrator()
        result = orch.diagnose(
            Incident(namespace="prod", pod_name="api-7d9f-x2kq")
        )
        assert result["severity"] == "high"


class TestNoPodNameScenario:
    """Сценарий без pod_name: per-pod шаги пропускаются, агент не падает."""

    def test_skips_per_pod_steps_and_warns(self):
        # Несколько подов без pod_name/deployment_name -> цель неоднозначна.
        many_pods = [
            {"name": f"api-7d9f-x2k{i}", "status": "Running", "restarts": 0,
             "node": "worker-1", "age": 10, "labels": {}}
            for i in range(3)
        ]
        orch, k8s, prom, logs_tools, llm = make_orchestrator(pods=many_pods)
        result = orch.diagnose(
            {"namespace": "prod", "description": "Namespace-wide latency alert"}
        )

        # Prometheus/логи/события не вызывались — per-pod шаги пропущены.
        prom.get_pod_cpu_usage.assert_not_called()
        logs_tools.get_pod_logs.assert_not_called()
        k8s.get_events.assert_not_called()
        # Но список подов собран и контекст ушёл в LLM.
        k8s.get_pods.assert_called_once()
        llm.generate.assert_called_once()

        assert any("pod_name" in w for w in result["warnings"])
        assert result["root_cause"].startswith("OOMKilled")  # ответ LLM всё равно есть

    def test_resolves_pod_from_deployment_name(self):
        orch, _, prom, _, _ = make_orchestrator()
        result = orch.diagnose(
            {"namespace": "prod", "deployment_name": "api", "description": "x"}
        )
        # Под выведен из префикса деплоймента -> метрики собраны.
        prom.get_pod_cpu_usage.assert_called_once_with(
            namespace="prod", pod_name="api-7d9f-x2kq"
        )
        assert result["raw_data"]["target_pod"]["name"] == "api-7d9f-x2kq"


class TestToolFailureScenario:
    """Сценарий с ошибкой инструмента: диагностика продолжается с warnings."""

    def test_prometheus_failure_recorded_as_warning(self):
        orch, _, prom, _, _ = make_orchestrator(cpu=None, mem=None)
        prom.get_pod_cpu_usage.side_effect = ConnectionError("prometheus down")
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )

        assert any("CPU-метрики" in w for w in result["warnings"])
        assert any("Memory-метрики" in w for w in result["warnings"])
        assert result["severity"] == "high"  # RCA получен несмотря на сбой

    def test_logs_empty_string_recorded_as_warning(self):
        orch, *_, logs_tools, _ = make_orchestrator(logs="")
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )
        assert any("Логи пода" in w for w in result["warnings"])

    def test_llm_failure_returns_empty_rca_with_warning(self):
        orch, *_ = make_orchestrator(llm_answer=None)  # llm.generate бросает
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )
        assert result["root_cause"] == ""
        assert result["severity"] == "info"
        assert any("LLM" in w for w in result["warnings"])

    def test_broken_llm_json_is_tolerated(self):
        orch, *_ = make_orchestrator(llm_answer="извините, я не смог ответить JSON")
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )
        assert result["root_cause"] == ""
        assert result["llm_response"] == "извините, я не смог ответить JSON"


class TestFormatReport:
    """Отчёт для Slack/Telegram."""

    def test_report_contains_all_sections(self):
        orch, *_ = make_orchestrator()
        diagnosis = orch.diagnose(
            {
                "namespace": "prod",
                "pod_name": "api-7d9f-x2kq",
                "description": "Pod restarts alert",
            }
        )
        report = orch.format_report(diagnosis)

        assert "🟠" in report  # severity=high
        assert "prod" in report and "api-7d9f-x2kq" in report
        assert "Root cause:" in report and "OOMKilled" in report
        assert "Evidence:" in report and "restarts=5" in report
        assert "Suggested fix:" in report
        assert "Замечания" not in report  # warnings пусты

    def test_report_shows_warnings_when_present(self):
        orch, *_ = make_orchestrator(llm_answer=None)
        diagnosis = orch.diagnose({"namespace": "prod"})
        report = orch.format_report(diagnosis)
        assert "⚠️" in report
        assert "не определён" in report


class TestLLMClientStub:
    """Заглушка LLM выдаёт валидный фиксированный JSON без сети."""

    def test_stub_returns_valid_json(self):
        import json

        raw = LLMClientStub().generate(prompt="p", context={"a": 1})
        data = json.loads(raw)
        assert set(data) == {"root_cause", "evidence", "suggested_fix", "severity"}

    def test_orchestrator_default_llm_is_stub(self):
        orch = IncidentOrchestrator(
            k8s_tools=MagicMock(),
            prometheus_tools=MagicMock(),
            logs_tools=MagicMock(),
        )
        assert isinstance(orch.llm, LLMClientStub)
