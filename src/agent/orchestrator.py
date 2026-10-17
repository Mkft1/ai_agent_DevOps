"""Ядро AI-агента: оркестратор диагностики инцидентов в Kubernetes.

Инцидент -> сбор данных (K8s / Prometheus / логи) -> контекст -> LLM -> RCA.

LLM: по умолчанию используется реальный клиент :class:`src.agent.llm_client.LLMClient`
(Qwen / OpenAI-совместимый API, настройки из ``.env``). Для тестов и оффлайн-режима
можно передать безтокенную заглушку :class:`LLMClientStub` — оба реализуют
интерфейс ``generate(system_prompt=..., user_prompt=...) -> str``.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from src.agent.llm_client import LLMClient
from src.agent.tools.k8s_tools import K8sTools
from src.agent.tools.logs_tools import LogsTools
from src.agent.tools.prometheus_tools import PrometheusTools
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Системный промт senior SRE (по ТЗ).
SYSTEM_PROMPT = (
    "Ты — senior SRE. Тебе дан контекст инцидента в Kubernetes: метрики, логи, "
    "события. Верни строго JSON без markdown-обёртки с полями: root_cause "
    "(строка), evidence (массив строк), suggested_fix (строка), severity "
    "(low/medium/high/critical). Если данных недостаточно, укажи это в "
    "root_cause и поставь severity='unknown'."
)

SEVERITY_LEVELS = ("critical", "high", "medium", "low", "info", "unknown")


class Incident(BaseModel):
    """Входной инцидент (алерт) для диагностики."""

    namespace: str = Field(default="default", description="Namespace в Kubernetes")
    pod_name: str | None = Field(default=None, description="Имя пода (опционально)")
    deployment_name: str | None = Field(
        default=None, description="Имя деплоймента (опционально)"
    )
    description: str = Field(default="", description="Текст алерта / описание")


class DiagnosisPayload(BaseModel):
    """Схема ответа LLM (валидируется до сборки итогового диагноза).

    Поля соответствуют системному промту: root_cause / evidence /
    suggested_fix / severity. Строгая проверка типов — через pydantic.
    """

    root_cause: str
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    severity: str = "unknown"


class Diagnosis(BaseModel):
    """Выходной диагноз: RCA от LLM плюс сырые данные для отладки."""

    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    severity: str = "info"
    raw_data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    llm_response: str = ""


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Интерфейс LLM-клиента: заглушка и реальный API взаимозаменяемы."""

    def generate(
        self,
        prompt: str | None = None,
        context: dict[str, Any] | None = None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> str:  # pragma: no cover
        ...


class LLMClientStub:
    """Заглушка LLM: возвращает фиксированный JSON-ответ, токены не тратится.

    Используется в unit-тестах и оффлайн-режиме; интерфейс совпадает с
    :class:`src.agent.llm_client.LLMClient`.
    """

    STUB_ANSWER = {
        "root_cause": "[stub] Анализ ещё не выполнялся реальным LLM.",
        "evidence": ["Ответ сгенерирован LLMClientStub без обращения к API."],
        "suggested_fix": "Подключить реальный LLM-клиент вместо заглушки.",
        "severity": "info",
    }

    def generate(
        self,
        prompt: str | None = None,
        context: dict[str, Any] | None = None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> str:
        """Вернуть фиксированный JSON-ответ (эмулирует ответ LLM).

        Args:
            prompt: единый промт (старый формат).
            context: структурированный контекст собранных данных.
            system_prompt: системная роль (новый формат).
            user_prompt: пользовательское сообщение (новый формат).

        Returns:
            Строка с JSON: root_cause / evidence / suggested_fix / severity.
        """
        logger.debug(
            "LLMClientStub.generate: prompt_len=%d, context_keys=%s",
            len(prompt or user_prompt or ""),
            list(context.keys()) if context else None,
        )
        return json.dumps(self.STUB_ANSWER, ensure_ascii=False)


class IncidentOrchestrator:
    """Координирует цикл диагностики: сбор фактов -> контекст -> LLM -> RCA."""

    def __init__(
        self,
        k8s_tools: K8sTools | None = None,
        prometheus_tools: PrometheusTools | None = None,
        logs_tools: LogsTools | None = None,
        llm_client: LLMClientProtocol | None = None,
    ) -> None:
        """Создать оркестратор.

        Args:
            k8s_tools: инструменты Kubernetes (по умолчанию — новый экземпляр).
            prometheus_tools: инструменты Prometheus.
            logs_tools: инструменты логов.
            llm_client: LLM-клиент; по умолчанию — реальный
                :class:`~src.agent.llm_client.LLMClient` (Qwen/OpenAI-совместимый
                API из ``.env``). В тестах/оффлайне передавайте
                :class:`LLMClientStub`.
        """
        self.k8s = k8s_tools if k8s_tools is not None else K8sTools()
        self.prometheus = (
            prometheus_tools if prometheus_tools is not None else PrometheusTools()
        )
        self.logs = logs_tools if logs_tools is not None else LogsTools()
        self.llm = llm_client if llm_client is not None else LLMClient()

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

        # e/f. Контекст -> LLM: системный промт отдельно, данные — в user-сообщении.
        user_prompt = (
            f"Данные диагностики:\n"
            f"{json.dumps(context, ensure_ascii=False, default=str)}"
        )
        llm_raw = ""
        try:
            diagnosis_dict, llm_raw = self._query_llm(context, user_prompt)
        except Exception as exc:
            logger.error("LLM step failed: %s", exc)
            diagnosis_dict = {}
            warnings.append("LLM не ответил — RCA не сформирован")

        # g. Результат LLM плюс сырые данные для отладки.
        result = Diagnosis(
            root_cause=diagnosis_dict.get("root_cause", ""),
            evidence=list(diagnosis_dict.get("evidence", []) or []),
            suggested_fix=diagnosis_dict.get("suggested_fix", ""),
            severity=diagnosis_dict.get("severity", "info"),
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

    # ------------------------------------------------------------------
    # Взаимодействие с LLM: запрос, валидация, retry, fallback
    # ------------------------------------------------------------------
    def _query_llm(self, context: dict[str, Any], user_prompt: str) -> tuple[dict, str]:
        """Спросить LLM и вернуть (валидный диагноз, сырой ответ).

        Схема ТЗ: ответ валидируется pydantic-моделью :class:`DiagnosisPayload`;
        если JSON невалиден — один повторный запрос с указанием ошибки; если и
        повтор не помог — fallback-диагноз (данные собраны, но LLM не смог дать
        корректный ответ).

        Args:
            context: собранный контекст (для совместимости со старым API клиента).
            user_prompt: пользовательское сообщение с данными диагностики.

        Returns:
            Кортеж ``(diagnosis_dict, raw_response)``.

        Raises:
            Exception: если LLM-клиент недоступен (сеть/ключи) — обрабатывается
                вызывающим ``diagnose``.
        """
        raw = self.llm.generate(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, context=context
        )
        payload, error = self._validate_llm_answer(raw)
        if payload is not None:
            return payload, raw

        # Один retry: просим модель переделать ответ с учётом ошибки валидации.
        logger.warning("LLM answer invalid (%s) — retrying once", error)
        retry_prompt = (
            f"{user_prompt}\n\n"
            f"Твой предыдущий ответ не прошёл валидацию: {error}\n"
            "Исправь ошибку и верни строго JSON без markdown-обёртки с полями "
            "root_cause (str), evidence (list[str]), suggested_fix (str), "
            "severity (low/medium/high/critical/unknown)."
        )
        raw2 = self.llm.generate(
            system_prompt=SYSTEM_PROMPT, user_prompt=retry_prompt, context=context
        )
        payload2, error2 = self._validate_llm_answer(raw2)
        if payload2 is not None:
            return payload2, raw2

        # Fallback: валидного ответа нет — возвращаем диагностируемый результат
        # с пометкой, чтобы API/отчёт были честными перед оператором.
        logger.error("LLM answer still invalid after retry: %s", error2)
        fallback = {
            "root_cause": (
                "Не удалось получить валидный RCA от LLM: ответ не прошёл "
                f"валидацию схемы ({error2}). Данные инцидента собраны — "
                "требуется ручная проверка."
            ),
            "evidence": [f"Ошибка валидации: {error2}"],
            "suggested_fix": "Проверить доступность/настройки LLM и повторить диагностику.",
            "severity": "unknown",
        }
        return fallback, raw2

    @staticmethod
    def _validate_llm_answer(raw: str) -> tuple[dict | None, str]:
        """Распарсить и pydantic-валидировать ответ LLM.

        Args:
            raw: текст ответа модели.

        Returns:
            ``(payload_dict | None, error_message)`` — при успехе ошибка пустая.
        """
        data = LLMClient.parse_json_response(raw)
        if not data:
            return None, "JSON не найден в ответе или не разбился"
        try:
            return DiagnosisPayload.model_validate(data).model_dump(), ""
        except ValidationError as exc:
            return None, str(exc.errors()[0].get("msg", exc)) if exc.errors() else str(exc)

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
