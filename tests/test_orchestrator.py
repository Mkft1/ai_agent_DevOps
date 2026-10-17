"""Unit-тесты IncidentOrchestrator: все инструменты и LLM под моками."""

from unittest.mock import MagicMock

import pytest

from src.agent.orchestrator import (
    SYSTEM_PROMPT,
    Diagnosis,
    Incident,
    IncidentOrchestrator,
    LLMClientStub,
    NamespaceNotAllowedError,
)
from src.config import Settings

# Тестовые настройки: разрешаем namespace'ы default/prod/monitoring (как в fixture).
TEST_SETTINGS = Settings(
    allowed_namespaces=["default", "prod", "monitoring"], _env_file=None
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
    settings=TEST_SETTINGS,
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
        # Первый вызов — основной, второй (если будет) — retry.
        llm.generate.side_effect = None
        llm.generate.return_value = llm_answer
    else:
        llm.generate.side_effect = RuntimeError("LLM unavailable")

    return (
        IncidentOrchestrator(
            k8s_tools=k8s,
            prometheus_tools=prom,
            logs_tools=logs_tools,
            llm_client=llm,
            settings=settings,
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

        # Контекст собран и передан в LLM; системный промт — отдельным сообщением.
        call_kwargs = llm.generate.call_args.kwargs
        assert call_kwargs["system_prompt"] == SYSTEM_PROMPT
        assert "Данные диагностики" in call_kwargs["user_prompt"]
        context_arg = call_kwargs["context"]
        assert context_arg["target_pod"]["restarts"] == 5
        assert context_arg["metrics"] == {
            "cpu_cores": 0.25,
            "memory_bytes": 134_217_728,
        }
        assert "oom killed" in context_arg["pod_logs_tail"]

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

    def test_broken_llm_json_triggers_retry_and_fallback(self):
        # Первый ответ — невалидный JSON, retry-ответ — валидный.
        orch, *_, llm = make_orchestrator(
            llm_answer="извините, я не смог ответить JSON"
        )
        good = (
            '{"root_cause": "r", "evidence": ["e"], '
            '"suggested_fix": "f", "severity": "low"}'
        )
        llm.generate.side_effect = [
            "извините, я не смог ответить JSON",
            good,
        ]
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )
        assert llm.generate.call_count == 2
        # В retry-промте передана ошибка валидации (по ТЗ).
        retry_prompt = llm.generate.call_args_list[1].kwargs["user_prompt"]
        assert "не прошёл валидацию" in retry_prompt
        assert result["root_cause"] == "r"
        assert result["llm_response"] == good

    def test_invalid_json_after_retry_returns_fallback(self):
        orch, *_, llm = make_orchestrator(llm_answer="не JSON совсем")
        result = orch.diagnose(
            {"namespace": "prod", "pod_name": "api-7d9f-x2kq"}
        )
        assert llm.generate.call_count == 2  # основной запрос + один retry
        assert result["severity"] == "unknown"
        assert "валидацию" in result["root_cause"]
        Diagnosis.model_validate(result)  # fallback — тоже валидный диагноз


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

    def test_orchestrator_default_llm_is_real_client(self):
        # По умолчанию оркестратор использует реальный LLMClient (Qwen API),
        # а не заглушку; параметры берутся из config/.env.
        from src.agent.llm_client import LLMClient as RealLLMClient

        orch = IncidentOrchestrator(
            k8s_tools=MagicMock(),
            prometheus_tools=MagicMock(),
            logs_tools=MagicMock(),
            llm_client=RealLLMClient(api_key="test-key"),
        )
        assert isinstance(orch.llm, RealLLMClient)
        assert not isinstance(orch.llm, LLMClientStub)


class TestGuardrails:
    """Ограничения безопасности: ALLOWED_NAMESPACES, MAX_LOG_LINES, read-only."""

    def test_disallowed_namespace_raises_before_any_calls(self):
        """Namespace вне ALLOWED_NAMESPACES — ошибка, запросы не выполняются."""
        orch, k8s, prom, logs_tools, llm = make_orchestrator()
        with pytest.raises(NamespaceNotAllowedError, match="not allowed"):
            orch.diagnose(
                Incident(namespace="kube-system", pod_name="x", description="d")
            )
        # Ни один инструмент не был вызван.
        k8s.get_pods.assert_not_called()
        k8s.get_events.assert_not_called()
        prom.get_pod_cpu_usage.assert_not_called()
        logs_tools.get_pod_logs.assert_not_called()
        llm.generate.assert_not_called()

    def test_allowed_namespaces_come_from_settings(self):
        """Список разрешённых namespace читается из конфига (не хардкод)."""
        strict = Settings(allowed_namespaces=["only-me"], _env_file=None)
        orch, *_ = make_orchestrator(settings=strict)
        with pytest.raises(NamespaceNotAllowedError):
            orch.diagnose(Incident(namespace="default", description="d"))
        # В allow-списке — проходит.
        result = orch.diagnose(Incident(namespace="only-me", description="d"))
        assert "root_cause" in result

    def test_tail_lines_clamped_to_max_log_lines(self):
        """MAX_LOG_LINES ограничивает tail_lines логов (10 -> 5 при лимите 5)."""
        limited = Settings(
            allowed_namespaces=["default", "prod", "monitoring"],
            max_log_lines=5,
            _env_file=None,
        )
        orch, _, _, logs_tools, _ = make_orchestrator(settings=limited)
        orch.diagnose(
            Incident(
                namespace="prod", pod_name="api-7d9f-x2kq", description="d"
            )
        )
        kwargs = logs_tools.get_pod_logs.call_args.kwargs
        assert kwargs["tail_lines"] == 5

    def test_default_settings_limits(self):
        """Дефолты конфига по ТЗ: default/200/False."""
        s = Settings(_env_file=None)
        assert s.allowed_namespaces == ["default"]
        assert s.max_log_lines == 200
        assert s.enable_write_actions is False

    def test_read_only_mode_marked_in_context(self):
        """При ENABLE_WRITE_ACTIONS=False контекст отмечает read-only режим."""
        orch, _, _, _, llm = make_orchestrator()
        orch.diagnose(Incident(namespace="default", description="d"))
        context = llm.generate.call_args.kwargs["context"]
        assert context["mode"] == "read-only"

    def test_no_write_methods_on_orchestrator(self):
        """Агент не имеет write-действий: нет delete/exec/apply методов."""
        forbidden = ("delete", "exec", "apply", "patch", "scale", "restart")
        methods = [m for m in dir(IncidentOrchestrator) if not m.startswith("__")]
        assert not [m for m in methods if any(f in m.lower() for f in forbidden)]
