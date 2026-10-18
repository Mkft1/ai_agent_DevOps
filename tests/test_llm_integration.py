"""Интеграционные тесты LLM-клиента и оркестратора.

Внешний HTTP (Qwen / OpenAI-совместимый /v1/chat/completions) полностью
замоканирован через ``unittest.mock.patch("requests.post")`` — реальных
вызовов API нет, токены не тратятся.

Проверяются:
    * успешный запрос + парсинг ответа через DiagnosisPayload;
    * fallback при невалидном JSON (после одного retry);
    * ретраи tenacity на 429/5xx (максимум 3 попытки);
    * неретраибельные 4xx;
    * таймаут 30 с по умолчанию.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.agent.llm_client import LLMAuthError, LLMClient, LLMError
from src.agent.orchestrator import (
    SYSTEM_PROMPT,
    Diagnosis,
    DiagnosisPayload,
    IncidentOrchestrator,
)
from src.config import Settings

VALID_ANSWER = json.dumps(
    {
        "root_cause": "OOMKilled: контейнер упирается в memory limit",
        "evidence": ["restarts=5", "OOMKilling event"],
        "suggested_fix": "Поднять memory limit до 512Mi",
        "severity": "high",
    },
    ensure_ascii=False,
)


def http_response(status_code: int = 200, payload: dict | None = None) -> MagicMock:
    """Собрать мок HTTP-ответа requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {
        "choices": [{"message": {"content": VALID_ANSWER}}]
    }
    if status_code >= 400:
        err = requests.HTTPError(f"{status_code} error")
        err.response = resp
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def make_client() -> LLMClient:
    """Клиент с тестовым ключом (реальные секреты не используются)."""
    return LLMClient(
        base_url="https://llm.test/v1", api_key="test-key", model="qwen-max"
    )


def make_orchestrator(llm: LLMClient) -> IncidentOrchestrator:
    """Оркестратор на моках инструментов и реальном (замоканированном) LLM."""
    k8s = MagicMock()
    k8s.get_pods.return_value = [
        {"name": "api-1", "status": "Running", "restarts": 5, "node": "w1"}
    ]
    k8s.get_events.return_value = []
    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = 0.3
    prom.get_pod_memory_usage.return_value = 500_000_000
    logs_tools = MagicMock()
    logs_tools.get_pod_logs.return_value = "ERROR oom killed"
    return IncidentOrchestrator(
        k8s_tools=k8s,
        prometheus_tools=prom,
        logs_tools=logs_tools,
        llm_client=llm,
        # Guardrails: в тестах разрешаем namespace, используемый в фикстурах.
        settings=Settings(allowed_namespaces=["default", "prod"], _env_file=None),
        rag=_disabled_rag(),
    )


def _disabled_rag():
    """RunbookRAG-заглушка: RAG выключён в интеграционных LLM-тестах."""
    rag = MagicMock()
    rag.find_relevant.return_value = []
    return rag


class TestLLMClientHTTP:
    """Низкоуровневые сценарии POST /v1/chat/completions."""

    @patch("src.agent.llm_client.requests.post")
    def test_generate_success_parses_content(self, post: MagicMock):
        post.return_value = http_response()
        client = make_client()
        out = client.generate(system_prompt="sys", user_prompt="usr")

        assert out == VALID_ANSWER
        url = post.call_args.args[0]
        assert url.endswith("/v1/chat/completions")
        assert not url.endswith("//v1")  # base_url с /v1 нормализуется
        kwargs = post.call_args.kwargs
        assert kwargs["timeout"] == 30.0  # таймаут по ТЗ
        messages = kwargs["json"]["messages"]
        assert messages[0] == {"role": "system", "content": "sys"}
        assert messages[1] == {"role": "user", "content": "usr"}
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")

    @patch("src.agent.llm_client.requests.post")
    def test_retry_on_5xx_then_success(self, post: MagicMock):
        post.side_effect = [http_response(503), http_response()]
        client = make_client()
        out = client.generate(system_prompt="s", user_prompt="u")
        assert out == VALID_ANSWER
        assert post.call_count == 2  # первая попытка отбитась, вторая успешна

    @patch("src.agent.llm_client.requests.post")
    def test_retry_stops_after_three_attempts(self, post: MagicMock):
        post.side_effect = [http_response(429), http_response(500), http_response(502)]
        client = make_client()
        with pytest.raises(requests.HTTPError):
            client.generate(system_prompt="s", user_prompt="u")
        assert post.call_count == 3  # максимум 3 попытки (tenacity stop_after_attempt)

    @patch("src.agent.llm_client.requests.post")
    def test_non_retryable_4xx_raises_llmerror(self, post: MagicMock):
        post.return_value = http_response(400)
        client = make_client()
        with pytest.raises(LLMError):
            client.generate(system_prompt="s", user_prompt="u")
        assert post.call_count == 1  # 400 не ретраится

    @patch("src.agent.llm_client.requests.post")
    def test_auth_401_raises_llmautherror_no_retry(self, post: MagicMock):
        """401 — неверный ключ: отдельный тип ошибки, без ретраев."""
        post.return_value = http_response(401)
        client = make_client()
        with pytest.raises(LLMAuthError):
            client.generate(system_prompt="s", user_prompt="u")
        assert post.call_count == 1

    @patch("src.agent.llm_client.requests.post")
    def test_auth_403_raises_llmautherror_no_retry(self, post: MagicMock):
        """403 — ключ без прав/заблокирован: тоже LLMAuthError, без ретраев."""
        post.return_value = http_response(403)
        client = make_client()
        with pytest.raises(LLMAuthError):
            client.generate(system_prompt="s", user_prompt="u")
        assert post.call_count == 1

    def test_missing_api_key_raises(self):
        with pytest.raises(LLMError):
            LLMClient(api_key=None)


class TestOrchestratorWithRealLLMClient:
    """Сквозной цикл diagnose() с настоящим LLMClient (HTTP замоканирован)."""

    @patch("src.agent.llm_client.requests.post")
    def test_full_diagnosis_valid_parsed(self, post: MagicMock):
        post.return_value = http_response()
        orch = make_orchestrator(make_client())
        result = orch.diagnose({"namespace": "prod", "pod_name": "api-1"})

        Diagnosis.model_validate(result)
        assert result["severity"] == "high"
        assert "OOMKilled" in result["root_cause"]
        assert result["evidence"] == ["restarts=5", "OOMKilling event"]
        # Системный промт ушёл в API ровно как в ТЗ.
        sent_messages = post.call_args.kwargs["json"]["messages"]
        assert sent_messages[0]["content"] == SYSTEM_PROMPT
        assert "senior SRE" in SYSTEM_PROMPT and "severity='unknown'" in SYSTEM_PROMPT

    @patch("src.agent.llm_client.requests.post")
    def test_invalid_json_then_retry_then_fallback(self, post: MagicMock):
        bad = MagicMock()
        bad.status_code = 200
        bad.raise_for_status.return_value = None
        bad.json.return_value = {"choices": [{"message": {"content": "не JSON!"}}]}
        post.return_value = bad

        orch = make_orchestrator(make_client())
        result = orch.diagnose({"namespace": "prod", "pod_name": "api-1"})

        Diagnosis.model_validate(result)
        assert result["severity"] == "unknown"
        assert "валидацию" in result["root_cause"]
        assert post.call_count == 2  # основной запрос + один retry с текстом ошибки
        retry_user = post.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        assert "не прошёл валидацию" in retry_user

    @patch("src.agent.llm_client.requests.post")
    def test_markdown_wrapped_json_is_parsed(self, post: MagicMock):
        wrapped = f"```json\n{VALID_ANSWER}\n```"
        post.return_value = http_response(
            payload={"choices": [{"message": {"content": wrapped}}]}
        )
        orch = make_orchestrator(make_client())
        result = orch.diagnose({"namespace": "prod", "pod_name": "api-1"})
        assert result["severity"] == "high"  # обёртка корректно срезана

    def test_diagnosis_payload_validation_rules(self):
        # Схема ответа: обязательный root_cause, severity по умолчанию unknown.
        p = DiagnosisPayload.model_validate({"root_cause": "x"})
        assert p.severity == "unknown"
        with pytest.raises(Exception):
            DiagnosisPayload.model_validate({"evidence": []})  # нет root_cause
