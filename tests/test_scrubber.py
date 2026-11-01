"""Unit-тесты PII/secret scrubber'а и его интеграции в оркестратор.

Покрывает: все паттерны ТЗ (JWT, API-ключи, пароли в URL, Bearer, email),
отсутствие false positives на обычных K8s-строках, счётчики типов,
конфигурацию ENABLE_SCRUBBING/REDACT_IPS (env + kwargs) и вызов скраббера
в IncidentOrchestrator ДО отправки контекста в LLM (с метрикой
scrubbed_items_total и без маскирования ответа модели).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from src.config import Settings, get_settings
from src.utils.scrubber import scrub_sensitive_data, scrub_structure

# ----------------------------------------------------------------------
# Тестовые секреты (синтетические, не боевые)
# ----------------------------------------------------------------------
JWT_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
OPENAI_KEY = "sk-" + "aB1cD2" * 5  # sk- + 30 alphanumeric (>= 20)
GITHUB_PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # ghp_ + 36
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"  # AKIA + 16 [0-9A-Z]
GENERIC_KEY = "key-" + "x9Y8z7" * 7  # key- + 42 alphanumeric (>= 32)
EMAIL = "admin@example.com"
DB_URL = "postgres://app:hunter2@db.internal:5432/orders"
BEARER_HEADER = "Authorization: Bearer abc123.DEF-456_x~y+z/w=="


@pytest.fixture(autouse=True)
def _hermetic_scrubber_settings(monkeypatch: pytest.MonkeyPatch):
    """Изолировать тесты от .env разработчика: фиксированные флаги скраббинга."""
    monkeypatch.setenv("ENABLE_SCRUBBING", "true")
    monkeypatch.setenv("REDACT_IPS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _metric_value(item_type: str) -> float:
    """Текущее значение scrubbed_items_total{type=...} (0 если серии ещё нет)."""
    return REGISTRY.get_sample_value("scrubbed_items_total", {"type": item_type}) or 0.0


class TestSecretPatterns:
    """Каждый паттерн ТЗ маскируется на [REDACTED:<type>]."""

    def test_jwt_masked(self) -> None:
        masked, counters = scrub_sensitive_data(f"auth failed, token {JWT_TOKEN} rejected")
        assert JWT_TOKEN not in masked
        assert "[REDACTED:jwt]" in masked
        assert counters == {"jwt": 1}

    def test_openai_key_masked(self) -> None:
        masked, counters = scrub_sensitive_data(f"OPENAI_API_KEY={OPENAI_KEY}")
        assert OPENAI_KEY not in masked
        assert "[REDACTED:api_key]" in masked
        assert counters == {"api_key": 1}

    def test_github_pat_masked(self) -> None:
        masked, counters = scrub_sensitive_data(f"export GITHUB_TOKEN={GITHUB_PAT}")
        assert GITHUB_PAT not in masked
        assert "[REDACTED:api_key]" in masked
        assert counters == {"api_key": 1}

    def test_pat_inside_url_masked_as_password(self) -> None:
        """PAT в git-URL перехватывается более ранним правилом паролей в URL."""
        masked, counters = scrub_sensitive_data(
            f"git clone https://x-access-token:{GITHUB_PAT}@github.com/org/repo"
        )
        assert GITHUB_PAT not in masked
        assert "[REDACTED:password]" in masked
        assert counters == {"password": 1}

    def test_aws_access_key_masked(self) -> None:
        masked, _ = scrub_sensitive_data(f"AWS_ACCESS_KEY_ID={AWS_ACCESS_KEY}")
        assert AWS_ACCESS_KEY not in masked
        assert "[REDACTED:api_key]" in masked

    def test_generic_key_masked(self) -> None:
        masked, _ = scrub_sensitive_data(f"internal service credential: {GENERIC_KEY}")
        assert GENERIC_KEY not in masked
        assert "[REDACTED:api_key]" in masked

    def test_url_password_masked_structure_preserved(self) -> None:
        masked, counters = scrub_sensitive_data(f"connecting to {DB_URL}")
        assert "hunter2" not in masked
        # Схема, пользователь и хост сохраняются — они нужны для диагностики.
        assert "postgres://app:[REDACTED:password]@db.internal:5432/orders" in masked
        assert counters == {"password": 1}

    def test_bearer_token_masked(self) -> None:
        masked, counters = scrub_sensitive_data(BEARER_HEADER)
        assert "abc123.DEF-456_x~y+z/w==" not in masked
        assert masked == "Authorization: [REDACTED:bearer]"
        assert counters == {"bearer": 1}

    def test_email_masked(self) -> None:
        masked, counters = scrub_sensitive_data(f"notify {EMAIL} and dev.team+alerts@corp.io")
        assert EMAIL not in masked
        assert "dev.team+alerts@corp.io" not in masked
        assert masked.count("[REDACTED:email]") == 2
        assert counters == {"email": 2}

    def test_realistic_log_line_all_secrets_masked(self) -> None:
        line = (
            f"2026-10-17T10:00:00Z ERROR login user={EMAIL} pass={DB_URL} "
            f"token={JWT_TOKEN} header='{BEARER_HEADER}' key={OPENAI_KEY}"
        )
        masked, counters = scrub_sensitive_data(line)
        for secret in (EMAIL, "hunter2", JWT_TOKEN, "abc123", OPENAI_KEY):
            assert secret not in masked
        assert counters == {
            "password": 1, "jwt": 1, "api_key": 1, "bearer": 1, "email": 1,
        }


class TestFalsePositives:
    """Обычные диагностические строки не должны скрабиться."""

    @pytest.mark.parametrize(
        "text",
        [
            "pod restarts",
            "Back-off restarting failed container api-7d9f-x2kq",
            "Liveness probe failed: HTTP probe failed with statuscode: 500",
            "2026-10-17T10:00:00Z ERROR connection refused to database",
            "kubectl -n production get pods -o wide",
            "ImagePullBackOff: back-off 5m0s pulling image nginx:1.27",
            "OOMKilled: container exceeded memory limit 512Mi",
        ],
    )
    def test_ordinary_k8s_strings_not_masked(self, text: str) -> None:
        masked, counters = scrub_sensitive_data(text)
        assert masked == text
        assert counters == {}

    @pytest.mark.parametrize(
        "text",
        [
            "sk-short",  # < 20 символов после sk-
            "key-abc123",  # < 32 символов после key-
            "ghp_tooshort",  # != 36 символов после ghp_
            "AKIA123",  # < 16 символов после AKIA
            "monkey-abc",  # 'key-' внутри слова, но хвост короче 32 символов
        ],
    )
    def test_short_key_like_strings_not_masked(self, text: str) -> None:
        masked, counters = scrub_sensitive_data(text)
        assert masked == text
        assert counters == {}


class TestCountersAndConfig:
    """Счётчики типов и конфигурация ENABLE_SCRUBBING."""

    def test_counters_exact_per_type(self) -> None:
        text = f"first {JWT_TOKEN} second {JWT_TOKEN} contact {EMAIL}"
        masked, counters = scrub_sensitive_data(text)
        assert counters == {"jwt": 2, "email": 1}
        assert masked.count("[REDACTED:jwt]") == 2

    def test_empty_text_passthrough(self) -> None:
        assert scrub_sensitive_data("") == ("", {})

    def test_disabled_via_kwarg_passthrough(self) -> None:
        text = f"token {JWT_TOKEN} mail {EMAIL}"
        masked, counters = scrub_sensitive_data(text, enabled=False)
        assert masked == text
        assert counters == {}

    def test_disabled_via_env_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ENABLE_SCRUBBING=false в окружении -> полный passthrough без изменений."""
        monkeypatch.setenv("ENABLE_SCRUBBING", "false")
        get_settings.cache_clear()
        text = f"token {JWT_TOKEN} url {DB_URL} mail {EMAIL}"
        masked, counters = scrub_sensitive_data(text)
        assert masked == text
        assert counters == {}

    def test_defaults_in_settings_model(self) -> None:
        """Дефолты конфигурации: скраббинг включён, IP не маскируются."""
        assert Settings.model_fields["enable_scrubbing"].default is True
        assert Settings.model_fields["redact_ips"].default is False

    def test_idempotent_on_already_masked_text(self) -> None:
        once = f"pass {DB_URL} token {JWT_TOKEN} mail {EMAIL}"
        masked1, counters1 = scrub_sensitive_data(once)
        masked2, counters2 = scrub_sensitive_data(masked1)
        assert masked2 == masked1  # плейсхолдеры не матчатся повторно
        assert counters2 == {}  # и не двойной счёт в метриках
        assert counters1 == {"password": 1, "jwt": 1, "email": 1}


class TestIpRedaction:
    """IP-адреса: по умолчанию сохраняются, REDACT_IPS=true маскирует."""

    LOG_WITH_IPS = "connection from 10.244.1.15 to 172.17.0.1:443 refused"

    def test_ips_kept_by_default(self) -> None:
        masked, counters = scrub_sensitive_data(self.LOG_WITH_IPS)
        assert masked == self.LOG_WITH_IPS
        assert counters == {}

    def test_ips_masked_with_flag(self) -> None:
        masked, counters = scrub_sensitive_data(self.LOG_WITH_IPS, redact_ips=True)
        assert "10.244.1.15" not in masked
        assert "172.17.0.1" not in masked
        assert masked.count("[REDACTED:ip]") == 2
        assert counters == {"ip": 2}

    def test_invalid_ip_octets_not_masked(self) -> None:
        masked, counters = scrub_sensitive_data("bogus address 999.999.999.999 seen", redact_ips=True)
        assert masked == "bogus address 999.999.999.999 seen"
        assert counters == {}


class TestScrubStructure:
    """Рекурсивное маскирование JSON-подобных структур (K8s-события)."""

    def test_recursive_dict_list_masking(self) -> None:
        event = {
            "type": "Warning",
            "reason": "FailedMount",
            "message": f"MountVolume.SetUp failed, token {JWT_TOKEN}",
            "involved_object": "Pod/api-7d9f-x2kq",
            "count": 3,
            "last_timestamp": None,
            "nested": [f"mail {EMAIL}", 42, {"deep": f"url {DB_URL}"}],
        }
        clean, counters = scrub_structure(event)
        assert "[REDACTED:jwt]" in clean["message"]
        assert clean["nested"][0] == "mail [REDACTED:email]"
        assert "[REDACTED:password]" in clean["nested"][2]["deep"]
        assert counters == {"jwt": 1, "email": 1, "password": 1}

    def test_non_string_values_untouched(self) -> None:
        clean, counters = scrub_structure(
            {"count": 42, "ok": True, "ratio": 0.25, "nothing": None}
        )
        assert clean == {"count": 42, "ok": True, "ratio": 0.25, "nothing": None}
        assert counters == {}


# ----------------------------------------------------------------------
# Интеграция с IncidentOrchestrator: скраббинг ДО отправки в LLM
# ----------------------------------------------------------------------
LLM_ANSWER = (
    '{"root_cause": "CrashLoopBackOff: приложение падает при подключении к БД", '
    '"evidence": ["restarts=5", "connection refused"], '
    '"suggested_fix": "Проверить credentials Secret и доступность БД", '
    '"severity": "high"}'
)


def _make_settings(**overrides) -> Settings:
    """Settings без .env: детерминированные флаги для тестов оркестратора."""
    return Settings(_env_file=None, allowed_namespaces=["default"], **overrides)


def _make_orchestrator(logs: str = "", events: list | None = None,
                       llm_answer: str = LLM_ANSWER, settings: Settings | None = None):
    """Оркестратор на моках (как в tests/test_orchestrator.py)."""
    from src.agent.orchestrator import IncidentOrchestrator

    k8s = MagicMock()
    k8s.get_pods.return_value = [
        {"name": "api-7d9f-x2kq", "status": "CrashLoopBackOff", "restarts": 5,
         "node": "worker-1"},
    ]
    k8s.get_events.return_value = events if events is not None else []
    prom = MagicMock()
    prom.get_pod_cpu_usage.return_value = 0.25
    prom.get_pod_memory_usage.return_value = 134_217_728
    logs_tools = MagicMock()
    logs_tools.get_pod_logs.return_value = logs
    llm = MagicMock()
    llm.generate.return_value = llm_answer
    rag = MagicMock()
    rag.find_relevant.return_value = []
    orch = IncidentOrchestrator(
        k8s_tools=k8s, prometheus_tools=prom, logs_tools=logs_tools,
        llm_client=llm, settings=settings or _make_settings(), rag=rag,
    )
    return orch, llm


INCIDENT = {
    "namespace": "default",
    "pod_name": "api-7d9f-x2kq",
    "description": "CrashLoopBackOff alerts firing",
}


class TestOrchestratorIntegration:
    """Scrubber вызывается в оркестраторе ПЕРЕД сериализацией контекста в LLM."""

    def test_logs_and_events_scrubbed_before_llm(self) -> None:
        logs = (
            f"INFO connecting {DB_URL}\n"
            f"DEBUG token={JWT_TOKEN}\n"
            f"ERROR auth failed, notify {EMAIL}\n"
        )
        events = [
            {"type": "Warning", "reason": "Unhealthy", "count": 2,
             "message": f"probe used header '{BEARER_HEADER}'",
             "involved_object": "Pod/api-7d9f-x2kq"},
        ]
        orch, llm = _make_orchestrator(logs=logs, events=events)
        before = {t: _metric_value(t) for t in ("password", "jwt", "email", "bearer")}

        result = orch.diagnose(INCIDENT)

        prompt = llm.generate.call_args.kwargs["user_prompt"]
        for secret in ("hunter2", JWT_TOKEN, EMAIL, "abc123.DEF-456"):
            assert secret not in prompt, f"secret leaked to LLM prompt: {secret[:20]}"
        for marker in ("[REDACTED:password]", "[REDACTED:jwt]",
                       "[REDACTED:email]", "[REDACTED:bearer]"):
            assert marker in prompt
        # Метрика scrubbed_items_total инкрементирована с правильными labels.
        after = {t: _metric_value(t) for t in before}
        assert {t: after[t] - before[t] for t in before} == {
            "password": 1, "jwt": 1, "email": 1, "bearer": 1,
        }
        # Диагностика при этом успешно завершилась.
        assert result["severity"] == "high"

    def test_scrubbing_disabled_passthrough_to_llm(self) -> None:
        """ENABLE_SCRUBBING=false -> секреты уходят в LLM как есть (passthrough)."""
        logs = f"DEBUG token={JWT_TOKEN}\n"
        orch, llm = _make_orchestrator(
            logs=logs, settings=_make_settings(enable_scrubbing=False)
        )
        before = _metric_value("jwt")

        orch.diagnose(INCIDENT)

        prompt = llm.generate.call_args.kwargs["user_prompt"]
        assert JWT_TOKEN in prompt
        assert "[REDACTED:jwt]" not in prompt
        assert _metric_value("jwt") - before == 0

    def test_llm_answer_fields_not_scrubbed(self) -> None:
        """root_cause/evidence/suggested_fix из ответа LLM не маскируются."""
        answer = json.dumps({
            "root_cause": f"Утечка учётных данных: {EMAIL}",
            "evidence": [f"В логах найден токен {JWT_TOKEN}"],
            "suggested_fix": f"Ротировать ключ, уведомить {EMAIL}",
            "severity": "critical",
        }, ensure_ascii=False)
        orch, _ = _make_orchestrator(logs="clean logs", llm_answer=answer)

        result = orch.diagnose(INCIDENT)

        assert result["root_cause"] == f"Утечка учётных данных: {EMAIL}"
        assert result["evidence"] == [f"В логах найден токен {JWT_TOKEN}"]
        assert EMAIL in result["suggested_fix"]

    def test_redact_ips_setting_applied(self) -> None:
        """REDACT_IPS=true маскирует IP в логах; по умолчанию IP сохраняются."""
        logs = "connection from 10.244.1.15 refused\n"
        orch_on, llm_on = _make_orchestrator(
            logs=logs, settings=_make_settings(redact_ips=True)
        )
        orch_on.diagnose(INCIDENT)
        prompt_on = llm_on.generate.call_args.kwargs["user_prompt"]
        assert "10.244.1.15" not in prompt_on
        assert "[REDACTED:ip]" in prompt_on

        orch_off, llm_off = _make_orchestrator(logs=logs)
        orch_off.diagnose(INCIDENT)
        prompt_off = llm_off.generate.call_args.kwargs["user_prompt"]
        assert "10.244.1.15" in prompt_off
