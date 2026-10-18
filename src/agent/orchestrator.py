"""Ядро AI-агента: оркестратор диагностики инцидентов в Kubernetes.

Инцидент -> сбор данных (K8s / Prometheus / логи) -> контекст -> LLM -> RCA.

LLM: по умолчанию используется реальный клиент :class:`src.agent.llm_client.LLMClient`
(Qwen / OpenAI-совместимый API, настройки из ``.env``). Для тестов и оффлайн-режима
можно передать безтокенную заглушку :class:`LLMClientStub` — оба реализуют
интерфейс ``generate(system_prompt=..., user_prompt=...) -> str``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from src.agent.llm_client import LLMClient
from src.agent.rag import RunbookRAG
from src.agent.tools.k8s_tools import K8sTools
from src.agent.tools.logs_tools import LogsTools
from src.agent.tools.prometheus_tools import PrometheusTools
from src.config import Settings, get_settings
from src.utils.logger import get_logger
from src.utils.metrics import (
    inc_diagnose_request,
    inc_llm_fallback,
    observe_diagnose_duration,
)
from src.utils.text_utils import TOKEN_SOFT_LIMIT, estimate_tokens, truncate_context

logger = get_logger(__name__)


class NamespaceNotAllowedError(ValueError):
    """Guardrail: namespace не входит в ALLOWED_NAMESPACES — диагностика запрещена."""


# Количество строк лога по умолчанию (ограничивается MAX_LOG_LINES из конфига).
DEFAULT_TAIL_LINES = 50
# Число runbook'ов, подтягиваемых RAG в контекст LLM (top_k).
RAG_TOP_K = 2

# Fallback-версия системного промта senior SRE (используется, если файл
# src/prompts/sre_system.txt отсутствует или нечитаем — см. load_system_prompt).
DEFAULT_SYSTEM_PROMPT = (
    "Ты — senior SRE. Тебе дан контекст инцидента в Kubernetes: метрики, логи, "
    "события. Верни строго JSON без markdown-обёртки с полями: root_cause "
    "(строка), evidence (массив строк), suggested_fix (строка), severity "
    "(low/medium/high/critical). Если данных недостаточно, укажи это в "
    "root_cause и поставь severity='unknown'. Опирайся на relevant_runbooks, "
    "если они предоставлены, чтобы предложить suggested_fix."
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "sre_system.txt"


def load_system_prompt(path: Path | None = None) -> str:
    """Надёжно загрузить системный промт для LLM из текстового файла.

    Args:
        path: путь к файлу промта; по умолчанию — ``src/prompts/sre_system.txt``.

    Returns:
        Содержимое файла. Если файл отсутствует или пуст — возвращает
        :data:`DEFAULT_SYSTEM_PROMPT` с логированием ошибки (приложение не падает).
    """
    prompt_path = path if path is not None else SYSTEM_PROMPT_FILE
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"prompt file is empty: {prompt_path}")
        return text
    except Exception as exc:  # noqa: BLE001 - fallback обязателен по ТЗ
        logger.error(
            "Failed to load system prompt from %s (%s); using built-in fallback",
            prompt_path,
            exc,
        )
        return DEFAULT_SYSTEM_PROMPT


SYSTEM_PROMPT = load_system_prompt()

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
        settings: Settings | None = None,
        rag: RunbookRAG | None = None,
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
            settings: настройки приложения (guardrails); по умолчанию —
                :func:`~src.config.get_settings` из ``.env``.
            rag: RAG-поиск по runbook'ам; по умолчанию — новый
                :class:`~src.agent.rag.RunbookRAG` над ``docs/runbooks``.
                Передавайте экземпляр с пустой директорией (или mock), чтобы
                отключить RAG в тестах/оффлайне.
        """
        self.k8s = k8s_tools if k8s_tools is not None else K8sTools()
        self.prometheus = (
            prometheus_tools if prometheus_tools is not None else PrometheusTools()
        )
        self.logs = logs_tools if logs_tools is not None else LogsTools()
        self.llm = llm_client if llm_client is not None else LLMClient()
        self.settings = settings if settings is not None else get_settings()
        self.rag = rag if rag is not None else RunbookRAG()

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------
    def _check_namespace(self, namespace: str) -> None:
        """Guardrail: разрешён ли namespace (ALLOWED_NAMESPACES из конфига).

        Args:
            namespace: запрашиваемый namespace инцидента.

        Raises:
            NamespaceNotAllowedError: если namespace не в списке разрешённых.
        """
        allowed = list(self.settings.allowed_namespaces or [])
        if "*" in allowed:  # явный wildcard для отладки: разрешить всё
            return
        if namespace not in allowed:
            logger.warning(
                "Guardrail: namespace %r not in ALLOWED_NAMESPACES=%s",
                namespace,
                allowed,
            )
            raise NamespaceNotAllowedError(
                f"Namespace '{namespace}' not allowed. "
                f"Allowed namespaces: {allowed}"
            )

    def _clamp_tail_lines(self, tail_lines: int = DEFAULT_TAIL_LINES) -> int:
        """Ограничить requested tail_lines значением MAX_LOG_LINES из конфига."""
        max_lines = max(int(self.settings.max_log_lines), 1)
        clamped = min(max(int(tail_lines), 1), max_lines)
        if clamped != tail_lines:
            logger.debug(
                "Guardrail: tail_lines %s limited to MAX_LOG_LINES=%d",
                tail_lines,
                max_lines,
            )
        return clamped

    def _limit_context(self, context_json: str) -> str:
        """Ограничить размер контекста для LLM значением MAX_CONTEXT_CHARS.

        Если сериализованный контекст длиннее лимита — обрезаем с хвоста и
        добавляем пометку, чтобы модель понимала неполноту данных.
        """
        limit = max(int(self.settings.max_context_chars), 1000)
        if len(context_json) <= limit:
            return context_json
        logger.warning(
            "Context truncated to MAX_CONTEXT_CHARS=%d (was %d chars)",
            limit,
            len(context_json),
        )
        marker = "\n...[context truncated]"
        return context_json[: limit - len(marker)] + marker

    @staticmethod
    def _build_rag_query(context: dict[str, Any], log_text: str) -> str:
        """Сформировать поисковый запрос для RAG по runbook'ам.

        Объединяет описание инцидента, статус целевого пода и последние 10
        строк логов — этого набора симптомов достаточно TF-IDF-ранжированию,
        чтобы отличить OOMKilled от ImagePullBackOff/CrashLoopBackOff.
        """
        parts: list[str] = []
        incident = context.get("incident") or {}
        if incident.get("description"):
            parts.append(str(incident["description"]))
        target_pod = context.get("target_pod") or {}
        if target_pod.get("status"):
            parts.append(str(target_pod["status"]))
        if log_text:
            tail = [line for line in log_text.splitlines() if line.strip()][-10:]
            parts.extend(tail)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Основной цикл диагностики
    # ------------------------------------------------------------------
    def diagnose(self, incident: dict | Incident) -> dict:
        """Публичный вход диагностики с Prometheus-метриками.

        Оборачивает :meth:`_diagnose_impl` в таймер ``diagnose_duration_seconds``
        (label namespace), после получения
        результата инкрементирует ``diagnose_requests_total``
        (labels namespace, severity).

        Args:
            incident: словарь или модель :class:`Incident`.

        Returns:
            Словарь-диагноз (см. :meth:`_diagnose_impl`).
        """
        ns = (
            incident.get("namespace") if isinstance(incident, dict)
            else getattr(incident, "namespace", None)
        )
        started = time.perf_counter()
        try:
            result = self._diagnose_impl(incident)
        finally:
            observe_diagnose_duration(ns, time.perf_counter() - started)
        inc_diagnose_request(ns, result.get("severity"))
        return result

    def _diagnose_impl(self, incident: dict | Incident) -> dict:
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

        # Guardrail 0: разрешённый namespace — до любых запросов к кластеру.
        self._check_namespace(incident.namespace)

        warnings: list[str] = []
        context: dict[str, Any] = {
            "incident": incident.model_dump(),
        }
        if not self.settings.enable_write_actions:
            context["mode"] = "read-only"

        # a. Список подов в namespace (Graceful Degradation: ошибка K8s API не
        # прерывает диагностику — продолжаем только по Prometheus-метрикам).
        k8s_unavailable = False
        try:
            pods = self.k8s.get_pods(namespace=incident.namespace)
        except Exception as exc:  # защита от непредвиденных ошибок инструмента
            logger.error("get_pods failed: %s", exc)
            pods = []
            k8s_unavailable = True
        if not pods and not incident.pod_name:
            # Инструмент «съел» ApiException и вернул [] — сам факт ошибки виден
            # только в логах; считаем K8s API недоступным (degraded mode).
            logger.error(
                "K8s API unavailable: get_pods returned empty list in ns/%s",
                incident.namespace,
            )
            k8s_unavailable = True
        if pods:
            context["pods"] = pods
        else:
            warnings.append(f"Не удалось получить список подов в ns/{incident.namespace}")

        # Graceful Degradation: если K8s API недоступен (исключение или пустой
        # ответ с ошибкой в логах) — продолжаем диагностику только по Prometheus.
        k8s_unavailable = k8s_unavailable or not pods
        if k8s_unavailable:
            warnings.append(
                "K8s API unavailable, using only Prometheus metrics"
            )

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
            elif k8s_unavailable or not pods:
                # Graceful Degradation: под известен из алерта, но K8s API
                # недоступен/не вернул данных — берём цель из алерта.
                context["target_pod"] = {"name": target_pod, "source": "alert"}
            else:
                warnings.append(f"Под {target_pod} не найден в ns/{incident.namespace}")

            # K8s-события запрашиваем только если API заведомо доступен (есть поды).
            if pods:
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

            # c. CPU и память через Prometheus (работает даже при K8s outage —
            # kubelet/cAdvisor метрики независимы от API-сервера).
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

            # d. Логи пода (tail, ограничен MAX_LOG_LINES из конфига).
            tail_lines = self._clamp_tail_lines(DEFAULT_TAIL_LINES)
            try:
                log_text = self.logs.get_pod_logs(
                    namespace=incident.namespace, pod_name=target_pod, tail_lines=tail_lines
                )
            except Exception as exc:
                logger.error("get_pod_logs failed: %s", exc)
                log_text = ""
            context["pod_logs_tail"] = log_text
            if not log_text:
                warnings.append(f"Логи пода {target_pod} не получены")

            # d2. RAG: поиск релевантных runbook'ов по симптомам (статус пода +
            # последние 10 строк логов). Ошибки/пустой результат не влияют на
            # диагностику — агент работает как раньше без relevant_runbooks.
            try:
                runbooks = self.rag.find_relevant(
                    self._build_rag_query(context, log_text), top_k=RAG_TOP_K
                )
            except Exception as exc:
                logger.error("RAG lookup failed: %s", exc)
                runbooks = []
            if runbooks:
                context["relevant_runbooks"] = runbooks
        else:
            warnings.append("pod_name не указан и не выводится из incident — skip per-pod steps")

        # e/f. Контекст -> LLM: системный промт отдельно, данные — в user-сообщении.
        # truncate_context рекурсивно обрезает длинные строки (логи), чтобы JSON
        # не превышал MAX_CONTEXT_CHARS; _limit_context — финальная гарантия лимита.
        context = truncate_context(context, self.settings.max_context_chars)
        context_json = json.dumps(context, ensure_ascii=False, default=str)
        user_prompt = (
            f"Данные диагностики:\n{self._limit_context(context_json)}"
        )
        # Грубая оценка стоимости запроса в токенах (1 токен ~ 4 символа).
        # Только для наблюдаемости: не блокирует вызов — truncate_context уже
        # ограничил контекст по символам.
        self._log_token_estimate(user_prompt)
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
        def _log_token_estimate(self, user_prompt: str) -> None:
        """Залогировать грубую оценку токенов перед вызовом LLM.

        INFO-строка содержит оценку для системного промта, пользовательского
        промта (контекста) и ожидаемого ответа (короткий JSON ~ 300 токенов).
        Если суммарная оценка превышает ``TOKEN_SOFT_LIMIT`` (8000 токенов —
        примерный лимит дешёвых моделей), добавляется WARNING о возможном
        превышении контекста. Запрос при этом НЕ блокируется: размер контекста
        уже ограничен ``truncate_context``/MAX_CONTEXT_CHARS.
        """
        system_tokens = estimate_tokens(SYSTEM_PROMPT)
        user_tokens = estimate_tokens(user_prompt)
        expected_response_tokens = 300  # короткий RCA-JSON по формату промта
        total = system_tokens + user_tokens + expected_response_tokens
        logger.info(
            "LLM token estimate: system=%d, context=%d, expected_response=%d, total=%d",
            system_tokens,
            user_tokens,
            expected_response_tokens,
            total,
        )
        if total > TOKEN_SOFT_LIMIT:
            logger.warning(
                "Prompt exceeds soft token limit (%d > %d): possible context "
                "overflow for small models; request is NOT blocked",
                total,
                TOKEN_SOFT_LIMIT,
            )

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
        inc_llm_fallback()
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
