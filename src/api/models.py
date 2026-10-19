"""Pydantic-модели входящих пейлоадов веб-слоя.

Здесь живут модели запросов, которые приходят из внешних систем
(Alertmanager и т.п.), чтобы ``src/main.py`` оставался тонким HTTP-слоем.
Модели толерантны к реальному формату Alertmanager: неизвестные поля
игнорируются (``extra="ignore"``), отсутствующие словари defaulted пусты.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agent.orchestrator import Incident


class AlertmanagerAlert(BaseModel):
    """Один алерт из пейлоада Alertmanager (формат v4).

    Attributes:
        status: ``firing`` или ``resolved``.
        labels: метки алерта (alertname, namespace, pod, severity, ...).
        annotations: человекочитаемые тексты (summary, description, ...).
        starts_at: ISO-8601 время начала (поле ``startsAt`` в пейлоаде).
        ends_at: ISO-8601 время окончания (``endsAt``; для firing — нулевое).
        fingerprint: уникальный отпечаток алерта (для трейсинга в логах).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str = Field(
        default="firing",
        description="Состояние алерта: `firing` (диагностируется) или "
        "`resolved` (агент пропускает).",
        examples=["firing"],
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Метки алерта: alertname, namespace, pod, severity и т.д. "
        "Из них извлекаются namespace и целевой под инцидента.",
        examples=[{"alertname": "PodOOMKilling", "namespace": "default",
                   "pod": "payments-api-7d9f4b-x2kq", "severity": "critical"}],
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="Человекочитаемые тексты алерта: summary и description "
        "становятся описанием инцидента для RAG и LLM.",
        examples=[{"summary": "Pod payments-api restarts repeatedly",
                   "description": "Container OOMKilled"}],
    )
    starts_at: str = Field(
        default="",
        alias="startsAt",
        description="ISO-8601 время возникновения алерта.",
        examples=["2026-10-19T10:00:00.000Z"],
    )
    ends_at: str = Field(
        default="",
        alias="endsAt",
        description="ISO-8601 время окончания (для firing — нулевое значение).",
        examples=["0001-01-01T00:00:00Z"],
    )
    fingerprint: str = Field(
        default="",
        description="Уникальный отпечаток алерта (для трейсинга в логах).",
        examples=["abc123def456"],
    )

    # ------------------------------------------------------------------
    # Маппинг алерта -> доменный Incident оркестратора
    # ------------------------------------------------------------------
    def extract_namespace(self) -> str:
        """Namespace из меток; по умолчанию ``default``, если метки нет."""
        return self.labels.get("namespace") or self.labels.get("k8s_namespace") or "default"

    def extract_pod_name(self) -> str | None:
        """Имя пода из меток (``pod`` / ``pod_name``); None, если под не указан."""
        return self.labels.get("pod") or self.labels.get("pod_name") or None

    def extract_description(self) -> str:
        """Описание инцидента: ``annotations.summary`` приоритетнее ``description``.

        Если обеих аннотаций нет — осмысленный fallback из ``alertname``,
        чтобы диагностика не начиналась с пустого описания.
        """
        summary = self.annotations.get("summary") or self.annotations.get("description")
        if summary:
            return summary
        alertname = self.labels.get("alertname", "unknown alert")
        return f"Alertmanager: {alertname} ({self.status})"

    def to_incident(self) -> Incident:
        """Сконвертировать алерт в доменную модель :class:`Incident`."""
        return Incident(
            namespace=self.extract_namespace(),
            pod_name=self.extract_pod_name(),
            deployment_name=self.labels.get("deployment") or None,
            description=self.extract_description(),
        )


class AlertmanagerPayload(BaseModel):
    """Стандартный пейлоад webhook'а Alertmanager (``version``/``status``/``alerts``).

    Остальные поля реального пейлоада (``receiver``, ``groupKey``,
    ``commonLabels``, ``externalURL`` и т.д.) игнорируются намеренно:
    для диагностики нужны только сами алерты.
    """

    model_config = ConfigDict(extra="ignore")

    version: str = Field(
        default="4",
        description="Версия формата вебхука Alertmanager.",
        examples=["4"],
    )
    status: str = Field(
        default="firing",
        description="Статус группы алертов (`firing` / `resolved`).",
        examples=["firing"],
    )
    alerts: list[AlertmanagerAlert] = Field(
        default_factory=list,
        description="Список алертов; диагностируются только `firing`.",
    )

    @property
    def firing_alerts(self) -> list[AlertmanagerAlert]:
        """Алерты в статусе ``firing`` — только они требуют диагностики."""
        return [a for a in self.alerts if a.status == "firing"]


class AlertmanagerWebhookResponse(BaseModel):
    """Ответ webhook'а: сколько алертов обработано/пропущено и сами диагнозы."""

    processed: int = Field(
        default=0, description="Сколько firing-алертов успешно диагностировано."
    )
    skipped: int = Field(
        default=0,
        description="Сколько алертов пропущено (resolved или guardrail-отклонение).",
    )
    diagnoses: list[dict] = Field(
        default_factory=list,
        description="Диагнозы по каждому обработанному алерту: "
        "{diagnosis: RCA, report: текст для мессенджеров}.",
    )
