"""Кастомные Prometheus-метрики AI-агента диагностики инцидентов.

Метрики объявляются на уровне модуля (дефолтный REGISTRY prometheus_client).
Все label-значения нормализуются к строкам и получают дефолт ``"unknown"``
для пустых/отсутствующих значений — чтобы никогда не падать на HTTP-пути.

Метрики:
    diagnose_requests_total      (Counter,   namespace, severity) — число
                                 выполненных диагностик.
    diagnose_duration_seconds    (Histogram, namespace)           — длительность
                                 метода IncidentOrchestrator.diagnose.
    k8s_api_errors_total         (Counter,   method)              — ошибки
                                 Kubernetes API в инструментах.
    llm_fallback_total           (Counter)                        — срабатывания
                                 fallback-диагноза (LLM вернул невалидный JSON).
    policy_approval_required_total (Counter)                      — вердикты
                                 policy-guardrail «требуется аппрув человека»
                                 (check_policy -> True: опасный паттерн или
                                 упоминание kubectl/helm/oc).
    scrubbed_items_total         (Counter,   type)                — число
                                 замаскированных чувствительных элементов
                                 (jwt/api_key/password/bearer/email/ip) перед
                                 отправкой данных в LLM.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# ----------------------------------------------------------------------
# Дефолтные значения labels (все ограничения — без хардкода в вызовах)
# ----------------------------------------------------------------------
DEFAULT_LABEL = "unknown"


def _label(value: object | None) -> str:
    """Нормализовать значение label: пустое/None -> ``unknown``."""
    if value is None:
        return DEFAULT_LABEL
    text = str(value).strip()
    return text or DEFAULT_LABEL


# ----------------------------------------------------------------------
# Определяемые метрики
# ----------------------------------------------------------------------
DIAGNOSE_REQUESTS_TOTAL = Counter(
    "diagnose_requests_total",
    "Total number of /diagnose requests processed by the agent",
    ["namespace", "severity"],
)

DIAGNOSE_DURATION_SECONDS = Histogram(
    "diagnose_duration_seconds",
    "Duration of IncidentOrchestrator.diagnose in seconds",
    ["namespace"],
)

K8S_API_ERRORS_TOTAL = Counter(
    "k8s_api_errors_total",
    "Total number of Kubernetes API errors encountered by agent tools",
    ["method"],
)

LLM_FALLBACK_TOTAL = Counter(
    "llm_fallback_total",
    "Total number of LLM fallback diagnoses (invalid JSON after retry)",
)

APPROVAL_REQUESTS_TOTAL = Counter(
    "approval_requests_total",
    "Total number of dangerous-fix approvals requested from humans (Slack)",
)

POLICY_APPROVAL_REQUIRED_TOTAL = Counter(
    "policy_approval_required_total",
    "Total number of policy-guardrail verdicts that flagged a suggested fix "
    "as requiring human approval (dangerous pattern or kubectl/helm/oc mention)",
)

SCRUBBED_ITEMS_TOTAL = Counter(
    "scrubbed_items_total",
    "Total number of sensitive items (secrets/PII) masked by the scrubber "
    "before sending diagnosis data to the LLM",
    ["type"],
)


# ----------------------------------------------------------------------
# Хелперы инкрементирования (не падают при любых входных данных)
# ----------------------------------------------------------------------
def inc_diagnose_request(namespace: str | None, severity: str | None) -> None:
    """Увеличить счётчик завершённых диагностик."""
    DIAGNOSE_REQUESTS_TOTAL.labels(
        namespace=_label(namespace), severity=_label(severity)
    ).inc()


def observe_diagnose_duration(namespace: str | None, seconds: float) -> None:
    """Записать длительность диагностики в гистограмму."""
    DIAGNOSE_DURATION_SECONDS.labels(namespace=_label(namespace)).observe(seconds)


def inc_k8s_api_error(method: str | None) -> None:
    """Увеличить счётчик ошибок Kubernetes API для метода инструмента."""
    K8S_API_ERRORS_TOTAL.labels(method=_label(method)).inc()


def inc_llm_fallback() -> None:
    """Увеличить счётчик срабатываний fallback-диагноза."""
    LLM_FALLBACK_TOTAL.inc()


def inc_approval_request() -> None:
    """Увеличить счётчик запрошенных человеческих аппрувов опасных фиксов."""
    APPROVAL_REQUESTS_TOTAL.inc()


def inc_policy_approval_required() -> None:
    """Увеличить счётчик вердиктов guardrail «требуется аппрув человека».

    Вызывается из :func:`src.agent.policies.check_policy` каждый раз, когда
    рекомендация помечается ``requires_approval=true`` (опасный паттерн
    уровня 1 или упоминание kubectl/helm/oc уровня 2).
    """
    POLICY_APPROVAL_REQUIRED_TOTAL.inc()


def inc_scrubbed_items(item_type: str | None, amount: int = 1) -> None:
    """Увеличить счётчик замаскированных чувствительных элементов по типу.

    Args:
        item_type: тип секрета из scrubber'а (jwt/api_key/password/bearer/
            email/ip); пустые значения нормализуются к ``unknown``.
        amount: число замаскированных элементов (<= 0 игнорируется).
    """
    if amount <= 0:
        return
    SCRUBBED_ITEMS_TOTAL.labels(type=_label(item_type)).inc(amount)
