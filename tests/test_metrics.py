"""Unit-тесты FinOps-метрик стоимости LLM: llm_tokens_total / llm_cost_estimate_usd.

Проверяются:
* хелперы инкрементирования (labels type=input|output, guard от неположительных
  значений, нормализация пустого типа к ``unknown``);
* формула стоимости в IncidentOrchestrator._record_llm_cost (1 токен ≈ 4 символа;
  cost = input/1000 * цена_in + output/1000 * цена_out);
* инкремент метрик после полного diagnose (мок LLM с ответом известной длины)
  и отсутствие инкремента при сбое LLM;
* конфигурация цен через env (LLM_COST_PER_1K_{INPUT,OUTPUT}_TOKENS), дефолты
  Qwen-max и fail-fast на отрицательные цены.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

import src.agent.orchestrator as orch_module
from src.agent.orchestrator import IncidentOrchestrator
from src.config import Settings
from src.utils.metrics import (
    LLM_COST_ESTIMATE_USD,
    LLM_TOKENS_TOTAL,
    inc_llm_cost_estimate,
    inc_llm_tokens,
)

# Ответ LLM известной длины — для детерминированной проверки output-токенов.
LLM_ANSWER = (
    '{"root_cause": "OOMKilled", "evidence": ["restarts=5"], '
    '"suggested_fix": "Raise memory limit", "severity": "high"}'
)


def tokens_value(token_type: str) -> float:
    """Текущее значение llm_tokens_total{type=...} (0.0, если метрики ещё нет)."""
    return REGISTRY.get_sample_value("llm_tokens_total", {"type": token_type}) or 0.0


def cost_value() -> float:
    """Текущее значение llm_cost_estimate_usd (sample *_total по конвенции клиента)."""
    return REGISTRY.get_sample_value("llm_cost_estimate_usd_total") or 0.0


def make_orchestrator(
    llm_answer: str | None = LLM_ANSWER, settings: Settings | None = None
) -> tuple[IncidentOrchestrator, MagicMock]:
    """Оркестратор на моках вместо реальных клиентов (паттерн test_orchestrator)."""
    k8s = MagicMock()
    k8s.get_pods.return_value = [
        {
            "name": "api-7d9f-x2kq",
            "status": "Running",
            "restarts": 5,
            "node": "worker-1",
            "age": 3600,
            "labels": {"app": "api"},
        }
    ]
    k8s.get_events.return_value = []

    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = 0.25
    prom.get_pod_memory_usage.return_value = 134_217_728

    logs_tools = MagicMock()
    logs_tools.get_pod_logs.return_value = "2026-10-17 ERROR oom killed"

    llm = MagicMock()
    if llm_answer is not None:
        llm.generate.return_value = llm_answer
    else:
        llm.generate.side_effect = RuntimeError("LLM unavailable")

    rag = MagicMock()
    rag.find_relevant.return_value = []

    if settings is None:
        settings = Settings(
            allowed_namespaces=["default", "prod"],
            llm_cost_per_1k_input_tokens=0.01,
            llm_cost_per_1k_output_tokens=0.03,
            _env_file=None,
        )
    orch = IncidentOrchestrator(
        k8s_tools=k8s,
        prometheus_tools=prom,
        logs_tools=logs_tools,
        llm_client=llm,
        settings=settings,
        rag=rag,
    )
    return orch, llm


class TestLlmTokensMetric:
    """llm_tokens_total: Counter c label type=input|output."""

    def test_input_tokens_increment_with_label(self) -> None:
        before = tokens_value("input")
        inc_llm_tokens("input", 120)
        assert tokens_value("input") == pytest.approx(before + 120)

    def test_output_tokens_increment_with_label(self) -> None:
        before_input = tokens_value("input")
        before_output = tokens_value("output")
        inc_llm_tokens("output", 42)
        assert tokens_value("output") == pytest.approx(before_output + 42)
        # label изолирован: input не изменился.
        assert tokens_value("input") == pytest.approx(before_input)

    def test_non_positive_amount_ignored(self) -> None:
        before_input, before_output = tokens_value("input"), tokens_value("output")
        inc_llm_tokens("input", 0)
        inc_llm_tokens("output", -5)
        assert tokens_value("input") == pytest.approx(before_input)
        assert tokens_value("output") == pytest.approx(before_output)

    def test_empty_type_normalized_to_unknown(self) -> None:
        before = tokens_value("unknown")
        inc_llm_tokens("", 7)
        assert tokens_value("unknown") == pytest.approx(before + 7)

    def test_metric_definitions(self) -> None:
        assert LLM_TOKENS_TOTAL._labelnames == ("type",)
        assert LLM_COST_ESTIMATE_USD._labelnames == ()


class TestLlmCostMetric:
    """llm_cost_estimate_usd: Counter без labels (суммарная стоимость, USD)."""

    def test_cost_counter_increments(self) -> None:
        before = cost_value()
        inc_llm_cost_estimate(0.0025)
        assert cost_value() == pytest.approx(before + 0.0025)

    def test_non_positive_cost_ignored(self) -> None:
        before = cost_value()
        inc_llm_cost_estimate(0.0)
        inc_llm_cost_estimate(-1.5)
        assert cost_value() == pytest.approx(before)


class TestRecordLlmCost:
    """IncidentOrchestrator._record_llm_cost: токены и стоимость LLM-запроса."""

    def test_formula_with_default_prices(self, monkeypatch) -> None:
        # Детерминированные длины: SYSTEM_PROMPT=400 символов -> 100 токенов.
        monkeypatch.setattr(orch_module, "SYSTEM_PROMPT", "s" * 400)
        orch, _ = make_orchestrator()
        b_in, b_out, b_cost = tokens_value("input"), tokens_value("output"), cost_value()

        orch._record_llm_cost("u" * 800, "r" * 200)

        # input = (800 + 400) // 4 = 300; output = 200 // 4 = 50.
        assert tokens_value("input") == pytest.approx(b_in + 300)
        assert tokens_value("output") == pytest.approx(b_out + 50)
        # cost = 300/1000 * 0.01 + 50/1000 * 0.03 = 0.003 + 0.0015.
        expected = 300 / 1000 * 0.01 + 50 / 1000 * 0.03
        assert cost_value() == pytest.approx(b_cost + expected)

    def test_custom_prices_from_settings(self, monkeypatch) -> None:
        monkeypatch.setattr(orch_module, "SYSTEM_PROMPT", "")
        settings = Settings(
            allowed_namespaces=["default"],
            llm_cost_per_1k_input_tokens=0.1,
            llm_cost_per_1k_output_tokens=0.2,
            _env_file=None,
        )
        orch, _ = make_orchestrator(settings=settings)
        b_in, b_out, b_cost = tokens_value("input"), tokens_value("output"), cost_value()

        orch._record_llm_cost("x" * 4000, "y" * 1000)

        # input = 4000 // 4 = 1000; output = 1000 // 4 = 250.
        assert tokens_value("input") == pytest.approx(b_in + 1000)
        assert tokens_value("output") == pytest.approx(b_out + 250)
        expected = 1000 / 1000 * 0.1 + 250 / 1000 * 0.2
        assert cost_value() == pytest.approx(b_cost + expected)

    def test_diagnose_increments_llm_metrics(self) -> None:
        """После полного diagnose токены и стоимость инкрементированы."""
        orch, _ = make_orchestrator()
        b_in, b_out, b_cost = tokens_value("input"), tokens_value("output"), cost_value()

        result = orch.diagnose(
            {
                "namespace": "prod",
                "pod_name": "api-7d9f-x2kq",
                "description": "Pod restarts alert",
            }
        )

        assert result["llm_response"] == LLM_ANSWER
        d_in = tokens_value("input") - b_in
        d_out = tokens_value("output") - b_out
        # output-токены детерминированы: длина ответа LLM известна.
        assert d_out == len(LLM_ANSWER) // 4
        # input-токены: системный промт + контекст диагностики (> 0).
        assert d_in > 0
        # Стоимость соответствует формуле с ценами по умолчанию (Qwen-max).
        assert cost_value() - b_cost == pytest.approx(
            d_in / 1000 * 0.01 + d_out / 1000 * 0.03
        )

    def test_llm_failure_does_not_record_metrics(self) -> None:
        """Сбой LLM -> стоимость не начисляется (запрос не завершился)."""
        orch, _ = make_orchestrator(llm_answer=None)
        b_in, b_out, b_cost = tokens_value("input"), tokens_value("output"), cost_value()

        result = orch.diagnose({"namespace": "prod", "pod_name": "api-7d9f-x2kq"})

        assert any("LLM не ответил" in w for w in result["warnings"])
        assert tokens_value("input") == pytest.approx(b_in)
        assert tokens_value("output") == pytest.approx(b_out)
        assert cost_value() == pytest.approx(b_cost)


class TestCostConfig:
    """Конфигурация цен: дефолты Qwen-max, env-переопределение, fail-fast."""

    def test_defaults_match_qwen_max_prices(self) -> None:
        s = Settings(_env_file=None)
        assert s.llm_cost_per_1k_input_tokens == 0.01
        assert s.llm_cost_per_1k_output_tokens == 0.03

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_COST_PER_1K_INPUT_TOKENS", "0.05")
        monkeypatch.setenv("LLM_COST_PER_1K_OUTPUT_TOKENS", "0.15")
        s = Settings(_env_file=None)
        assert s.llm_cost_per_1k_input_tokens == 0.05
        assert s.llm_cost_per_1k_output_tokens == 0.15

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, llm_cost_per_1k_input_tokens=-0.01)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, llm_cost_per_1k_output_tokens=-1.0)

    def test_zero_price_allowed_for_local_models(self) -> None:
        s = Settings(
            _env_file=None,
            llm_cost_per_1k_input_tokens=0,
            llm_cost_per_1k_output_tokens=0,
        )
        assert s.llm_cost_per_1k_input_tokens == 0.0
        assert s.llm_cost_per_1k_output_tokens == 0.0
