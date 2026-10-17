"""Ядро AI-агента: оркестратор диагностики инцидентов в Kubernetes.

Инцидент -> сбор данных (K8s / Prometheus / логи) -> контекст -> LLM -> RCA.

LLM-клиент на этом шаге — заглушка :class:`LLMClientStub`, чтобы не тратить
токены; реальный API подключается заменой одного класса (тот же интерфейс
``generate(prompt, context) -> str``).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, Field

from src.agent.tools.k8s_tools import K8sTools
from src.agent.tools.logs_tools import LogsTools
from src.agent.tools.prometheus_tools import PrometheusTools
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Системный промт SRE-эксперта (по ТЗ).
SYSTEM_PROMPT = (
    "Ты — SRE-эксперт. Проанализируй данные и верни RCA в формате JSON: "
    "{root_cause: str, evidence: list[str], suggested_fix: str, severity: str}"
)

SEVERITY_LEVELS = ("critical", "high", "medium", "low", "info")


class Incident(BaseModel):
    """Входной инцидент (алерт) для диагностики."""

    namespace: str = Field(default="default", description="Namespace в Kubernetes")
    pod_name: str | None = Field(default=None, description="Имя пода (опционально)")
    deployment_name: str | None = Field(
        default=None, description="Имя деплоймента (опционально)"
    )
    description: str = Field(default="", description="Текст алерта / описание")


class Diagnosis(BaseModel):
    """Выходной диагноз: RCA от LLM плюс сырые данные для отладки."""

    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    severity: str = "info"
    raw_data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    llm_response: str = ""


class LLMClient(Protocol):
    """Интерфейс LLM-клиента — позволяет подменить заглушку на реальный API."""

    def generate(self, prompt: str, context: dict) -> str:  # pragma: no cover
        ...


class LLMClientStub:
    """Заглушка LLM: возвращает фиксированный JSON-ответ, токены не тратятся.

    Реализует интерфейс ``generate(prompt, context) -> str`` — на следующем
    шаге достаточно написать класс с тем же методом (OpenAI/Qwen) и передать
    его в ``IncidentOrchestrator(llm_client=...)``.
    """

    STUB_ANSWER = {
        "root_cause": "[stub] Анализ ещё не выполнялся реальным LLM.",
        "evidence": ["Ответ сгенерирован LLMClientStub без обращения к API."],
        "suggested_fix": "Подключить реальный LLM-клиент вместо заглушки.",
        "severity": "info",
    }

    def generate(self, prompt: str, context: dict) -> str:
        """Вернуть фиксированный JSON-ответ (эмулирует ответ LLM).

        Args:
            prompt: пользовательский промт с контекстом диагностики.
            context: структурированный контекст собранных данных.

        Returns:
            Строка с JSON: root_cause / evidence / suggested_fix / severity.
        """
        logger.debug(
            "LLMClientStub.generate: prompt_len=%d, context_keys=%s",
            len(prompt),
            list(context.keys()),
        )
        return json.dumps(self.STUB_ANSWER, ensure_ascii=False)


class IncidentOrchestrator:
    """Координирует цикл диагностики: сбор фактов -> контекст -> LLM -> RCA."""

    def __init__(
        self,
        k8s_tools: K8sTools | None = None,
        prometheus_tools: PrometheusTools | None = None,
        logs_tools: LogsTools | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        """Создать оркестратор.

        Args:
            k8s_tools: инструменты Kubernetes (по умолчанию — новый экземпляр).
            prometheus_tools: инструменты Prometheus.
            logs_tools: инструменты логов.
            llm_client: LLM-клиент; по умолчанию — безтокенная заглушка.
        """
        self.k8s = k8s_tools if k8s_tools is not None else K8sTools()
        self.prometheus = (
            prometheus_tools if prometheus_tools is not None else PrometheusTools()
        )
        self.logs = logs_tools if logs_tools is not None else LogsTools()
        self.llm = llm_client if llm_client is not None else LLMClientStub()

    # ------------------------------------------------------------------
    # Основной цикл диагностики
    # ------------------------------------------------------------------
    def diagnose(self, incident: dict | Incident) -> dict:
        """Диагностировать инцидент и вернуть диагноз (dict для API-слоя).

        Шаги:
            a. поды в namespace; b. статус/события target-пода; c. CPU/память
            через Prometheus; d. tail-50 логов; e. контекст; f. запрос к LLM;
            g. парсинг RCA + сырые данные. Ошибки инструментов не роняют
            агент — они фиксируются в ``warnings``.

        Args:
            incident: словарь или модель :class:`Incident`
                (namespace, pod_name?, deployment_name?, description).

        Returns:
            Словарь-диагноз: root_cause, evidence, suggested_fix, severity,
            raw_data, warnings.
        """
        if isinstance(incident, dict):
            incident = Incident.model_validate(incident)

        warnings: list[str] = []
        context: dict[str, Any] = {
            "incident": incident.model_dump(),
        }

        # a. Список подов в namespace.
        try:
            pods = self.k8s.get_pods(namespace=incident.namespace)
        except Exception as exc:  # защита от непредвиденных ошибок инструмента
            logger.error("get_pods failed: %s", exc)
            pods = []
        if pods:
            context["pods"] = pods
        else:
            warnings.append(f"Не удалось получить список подов в ns/{incident.namespace}")

        # b. Целевой под: статус, рестарты, события.
        target_pod = incident.pod_name
        if not target_pod and incident.deployment_name:
            prefix = f"{incident.deployment_name}-"
            candidates = [p for p in pods if str(p.get("name", "")).startswith(prefix)]
            if candidates:
                target_pod = candidates[0]["name"]
        if not target_pod and len(pods) == 1:
            target_pod = pods[0]["name"]

        if target_pod:
            pod_info = next(
                (p for p in pods if p.get("name") == target_pod), None
            )
            if pod_info:
                context["target_pod"] = {
                    "name": pod_info["name"],
                    "status": pod_info.get("status"),
                    "restarts": pod_info.get("restarts"),
                    "node": pod_info.get("node"),
                }
            else:
                warnings.append(f"Под {target_pod} не найден в ns/{incident.namespace}")

            try:
                events = self.k8s.get_events(namespace=incident.namespace)
            except Exception as exc:
                logger.error("get_events failed: %s", exc)
                events = []
            relevant = [
                e
                for e in events
                if target_pod in str(e.get("involved_object", ""))
                or e.get("type") == "Warning"
            ]
            context["events"] = relevant[:20]
            if not events:
                warnings.append("События Kubernetes не получены (пусто или ошибка)")

            # c. CPU и память через Prometheus.
            try:
                cpu = self.prometheus.get_pod_cpu_usage(
                    namespace=incident.namespace, pod_name=target_pod
                )
            except Exception as exc:
                logger.error("get_pod_cpu_usage failed: %s", exc)
                cpu = None
            try:
                mem = self.prometheus.get_pod_memory_usage(
                    namespace=incident.namespace, pod_name=target_pod
                )
            except Exception as exc:
                logger.error("get_pod_memory_usage failed: %s", exc)
                mem = None
            context["metrics"] = {"cpu_cores": cpu, "memory_bytes": mem}
            if cpu is None:
                warnings.append(f"CPU-метрики по поду {target_pod} недоступны")
            if mem is None:
                warnings.append(f"Memory-метрики по поду {target_pod} недоступны")

            # d. Логи пода (tail 50 строк).
            try:
                log_text = self.logs.get_pod_logs(
                    namespace=incident.namespace, pod_name=target_pod, tail_lines=50
                )
            except Exception as exc:
                logger.error("get_pod_logs failed: %s", exc)
                log_text = ""
            context["pod_logs_tail_50"] = log_text
            if not log_text:
                warnings.append(f"Логи пода {target_pod} не получены")
        else:
            warnings.append("pod_name не указан и не выводится из incident — skip per-pod steps")

        # e/f. Контекст -> LLM с системным промтом.
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Данные диагностики:\n{json.dumps(context, ensure_ascii=False, default=str)}"
        )
        try:
            llm_raw = self.llm.generate(prompt=prompt, context=context)
        except Exception as exc:
            logger.error("LLM generate failed: %s", exc)
            llm_raw = ""
            warnings.append("LLM не ответил — RCA не сформирован")

        # g. Парсинг ответа + сырые данные для отладки.
        diagnosis = self._parse_llm_response(llm_raw)
        result = Diagnosis(
            root_cause=diagnosis.get("root_cause", ""),
            evidence=list(diagnosis.get("evidence", []) or []),
            suggested_fix=diagnosis.get("suggested_fix", ""),
            severity=diagnosis.get("severity", "info"),
            raw_data=context,
            warnings=warnings,
            llm_response=llm_raw,
        )
        logger.info(
            "Diagnosis complete: severity=%s, warnings=%d",
            result.severity,
            len(result.warnings),
        )
        return result.model_dump()

    @staticmethod
    def _parse_llm_response(raw: str) -> dict:
        """Извлечь JSON из ответа LLM (толерантно к markdown/поясняющему тексту)."""
        if not raw:
            return {}
        text = raw.strip()
        # Срезаем ```json ... ``` обёртки, если они есть.
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            logger.warning("LLM response has no JSON object: %.200s", raw)
            return {}
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM JSON: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Отчёт для Slack/Telegram
    # ------------------------------------------------------------------
    def format_report(self, diagnosis: dict) -> str:
        """Сформировать читаемый текстовый отчёт по результатам диагностики.

        Args:
            diagnosis: словарь, возвращённый :meth:`diagnose`.

        Returns:
            Многострочный текст отчёта (emoji-разметка для мессенджеров).
        """
        sev = str(diagnosis.get("severity", "info")).lower()
        sev_icon = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(sev, "ℹ️")

        incident = diagnosis.get("raw_data", {}).get("incident", {})
        lines = [
            f"{sev_icon} *Инцидент: диагностика* (severity: {sev})",
            f"*Namespace:* `{incident.get('namespace', 'n/a')}`"
            + (f", *Pod:* `{incident.get('pod_name')}`" if incident.get("pod_name") else ""),
        ]
        if incident.get("description"):
            lines.append(f"*Алерт:* {incident['description']}")

        lines += [
            "",
            f"*Root cause:* {diagnosis.get('root_cause') or 'не определён'}",
            "",
            "*Evidence:*",
        ]
        evidence = diagnosis.get("evidence") or []
        lines += [f"  • {e}" for e in evidence] or ["  • —"]

        lines += [
            "",
            f"*Suggested fix:* {diagnosis.get('suggested_fix') or '—'}",
        ]

        warnings = diagnosis.get("warnings") or []
        if warnings:
            lines += ["", "⚠️ *Замечания по сбору данных:*"]
            lines += [f"  • {w}" for w in warnings]

        return "\n".join(lines)
