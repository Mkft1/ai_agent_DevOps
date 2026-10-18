"""Точка входа FastAPI-приложения AI-агента диагностики инцидентов в Kubernetes.

Эндпоинты:
    GET  /health    — живность процесса (без внешних зависимостей).
    GET  /ready     — проверка доступности K8s API и Prometheus (200/503).
    GET  /metrics   — Prometheus-метрики (HTTP-метрики + кастомные метрики агента).
    POST /diagnose  — приём инцидента, запуск IncidentOrchestrator, возврат RCA.

Логика диагностики целиком в оркестраторе — здесь только HTTP-обвязка.
"""

from __future__ import annotations

import json
import os
import uuid

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from src.agent.llm_client import LLMClient, LLMError
from src.agent.orchestrator import (
    Diagnosis,
    Incident,
    IncidentOrchestrator,
    LLMClientStub,
    NamespaceNotAllowedError,
)
from src.api.models import AlertmanagerPayload, AlertmanagerWebhookResponse
from src.config import Settings, get_settings
from src.utils.audit import log_audit_event
from src.utils.logger import get_logger, request_id_var, setup_logging

settings = get_settings()
setup_logging(settings.log_level, settings.log_format)
logger = get_logger(__name__)

app = FastAPI(
    title="AI Agent DevOps",
    description="AI-агент для диагностики инцидентов в Kubernetes (метрики, логи, состояние подов, RCA).",
    version="0.1.0",
)

# CORS: origins берутся из конфига (CORS_ORIGINS в .env); по умолчанию "*" для dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins or ["*"]),
    allow_credentials=False,  # при allow_origins=["*"] браузеры всё равно не отдадут cookies
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Корреляция логов и запросов: заголовок ``X-Request-ID``.

    Берёт ID из входящего заголовка (если его прислал клиент/балансер)
    либо генерирует новый; кладёт в :data:`~src.utils.logger.request_id_var`
    (попадает в JSON-логи) и возвращает в ответе для трейсинга клиентом.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response


app.add_middleware(RequestIDMiddleware)

# Prometheus: стандартные HTTP-метрики (информационные) + эндпоинт /metrics.
# Кастомные метрики агента объявлены в src/utils/metrics.py и попадают в
# тот же дефолтный REGISTRY — будут видны на /metrics автоматически.
instrumentator = Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics"],
)
instrumentator.instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


# ----------------------------------------------------------------------
# Pydantic-модели HTTP-контракта
# ----------------------------------------------------------------------
class DiagnoseRequest(BaseModel):
    """Тело POST /diagnose — входной инцидент (алерт)."""

    namespace: str = Field(default="default", description="Namespace в Kubernetes")
    pod_name: str | None = Field(default=None, description="Имя пода (опционально)")
    deployment_name: str | None = Field(
        default=None, description="Имя деплоймента (опционально)"
    )
    description: str = Field(default="", description="Текст алерта / описание проблемы")

    def to_incident(self) -> Incident:
        """Конвертировать HTTP-модель в доменную модель оркестратора."""
        return Incident.model_validate(self.model_dump())


class DiagnosisResponse(BaseModel):
    """Ответ POST /diagnose: диагноз LLM + читаемый отчёт для мессенджеров."""

    diagnosis: Diagnosis
    report: str


class ReadyResponse(BaseModel):
    """Ответ GET /ready: статусы внешних зависимостей."""

    status: str
    checks: dict[str, str]


# ----------------------------------------------------------------------
# Dependency injection: оркестратор создаётся лениво и переиспользуется.
# В тестах переопределяется через app.dependency_overrides[get_orchestrator].
# ----------------------------------------------------------------------
_orchestrator: IncidentOrchestrator | None = None


def get_orchestrator() -> IncidentOrchestrator:
    """Возвращает singleton оркестратора (создаётся при первом запросе).

    Raises:
        HTTPException 503: LLM-ключ не задан и ``LLM_OFFLINE`` выключен —
            сервис честно сообщает, что не сконфигурирован, вместо 500.
    """
    global _orchestrator
    if _orchestrator is None:
        llm: LLMClient | LLMClientStub
        try:
            llm = LLMClient()
        except LLMError as exc:
            if not settings.llm_offline:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "LLM is not configured: set QWEN_API_KEY or OPENAI_API_KEY "
                        "in .env (or start with LLM_OFFLINE=true for stub/demo mode)."
                    ),
                ) from exc
            logger.warning(
                "No LLM API key and LLM_OFFLINE=true — falling back to LLMClientStub"
            )
            llm = LLMClientStub()
        _orchestrator = IncidentOrchestrator(llm_client=llm)
    return _orchestrator


def run_health_check() -> dict[str, str]:
    """Ядро /health — вынесено в функцию для возможности моканирования в тестах."""
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Ошибки валидации/парсинга тела запроса: 422 по умолчанию, но для
# webhook Alertmanager контракт требует 400 (некорректный JSON/схема).
# ----------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Возвращает 400 для /webhook/alertmanager и 422 для остальных эндпоинтов."""
    status = 400 if request.url.path == "/webhook/alertmanager" else 422
    return JSONResponse(
        status_code=status,
        content={"detail": json.loads(json.dumps(exc.errors(), default=str))},
    )


# ----------------------------------------------------------------------
# Глобальный обработчик необработанных исключений -> 500 {"detail": ...}
# ----------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Логирует любую необработанную ошибку и возвращает 500 с detail."""
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ----------------------------------------------------------------------
# Эндпоинты
# ----------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    """Проверка работоспособности сервиса (не зависит от внешних систем)."""
    return run_health_check()


def _check_kubernetes(s: Settings) -> str:
    """Лёгкая проверка доступности K8s API (список namespace, limit=1).

    Returns:
        "ok" | "unavailable: <причина>".
    """
    try:
        import kubernetes

        kubeconfig = os.path.expanduser(s.kubeconfig_path)
        kubernetes.config.load_kube_config(config_file=kubeconfig)
        v1 = kubernetes.client.CoreV1Api()
        # Минимальный запрос к API серверу.
        v1.list_namespace(limit=1, _request_timeout=5)
        return "ok"
    except Exception as exc:
        logger.warning("K8s readiness check failed: %s", exc)
        return f"unavailable: {exc}"


def _check_prometheus(s: Settings) -> str:
    """Лёгкая проверка доступности Prometheus (GET /-/ready, таймаут 3 с).

    Returns:
        "ok" | "unavailable: <причина>".
    """
    url = s.prometheus_url.rstrip("/") + "/-/ready"
    try:
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return "ok"
        return f"unavailable: HTTP {resp.status_code}"
    except Exception as exc:
        logger.warning("Prometheus readiness check failed: %s", exc)
        return f"unavailable: {exc}"


@app.get("/ready", response_model=ReadyResponse)
def ready() -> JSONResponse:
    """Проверка готовности: доступность K8s API и Prometheus.

    Возвращает 200, если обе зависимости доступны, иначе 503. Тело ответа
    содержит статус каждой проверки.
    """
    checks = {
        "kubernetes": _check_kubernetes(settings),
        "prometheus": _check_prometheus(settings),
    }
    all_ok = all(v == "ok" for v in checks.values())
    body = ReadyResponse(status="ready" if all_ok else "degraded", checks=checks)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=body.model_dump(),
    )


@app.post("/diagnose", response_model=DiagnosisResponse)
def diagnose(
    payload: DiagnoseRequest,
    orchestrator: IncidentOrchestrator = Depends(get_orchestrator),
) -> DiagnosisResponse:
    """Принять инцидент и запустить диагностику через IncidentOrchestrator.

    Эндпоинт не дублирует логику агента: только валидация входа, вызов
    ``orchestrator.diagnose()`` и форматирование отчёта.
    """
    incident = payload.to_incident()
    try:
        diagnosis_dict = orchestrator.diagnose(incident)
    except NamespaceNotAllowedError as exc:
        # Guardrail: запрещённый namespace — запрос не выполняется.
        logger.warning("Guardrail rejected incident: %s", exc)
        log_audit_event(
            "guardrail_rejected",
            incident.model_dump(),
            {"root_cause": f"guardrail: {exc}", "severity": "unknown"},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Diagnosis failed for incident %s: %s", incident, exc)
        log_audit_event(
            "diagnose_failed",
            incident.model_dump(),
            {"root_cause": f"exception: {exc}", "severity": "unknown"},
        )
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {exc}") from exc

    log_audit_event("diagnose", incident.model_dump(), diagnosis_dict)
    report = orchestrator.format_report(diagnosis_dict)
    return DiagnosisResponse(
        diagnosis=Diagnosis.model_validate(diagnosis_dict),
        report=report,
    )


@app.post("/webhook/alertmanager", response_model=AlertmanagerWebhookResponse)
def webhook_alertmanager(
    payload: AlertmanagerPayload,
    orchestrator: IncidentOrchestrator = Depends(get_orchestrator),
) -> AlertmanagerWebhookResponse:
    """Webhook для Alertmanager: диагностика всех firing-алертов пейлоада.

    Контракт: принимаем стандартный пейлоад Alertmanager (``version``/
    ``status``/``alerts``); из каждого firing-алерта извлекаются namespace
    (``labels.namespace``), pod (``labels.pod``/``pod_name``) и описание
    (``annotations.summary`` || ``annotations.description``) и передаются
    в :meth:`IncidentOrchestrator.diagnose`. Resolved-алерты пропускаются.
    Ошибки парсинга JSON/схемы -> 400 (см. ``request_validation_handler``).
    Каждое событие пишется в журнал аудита (``src/utils/audit.py``).
    """
    firing = payload.firing_alerts
    skipped = len(payload.alerts) - len(firing)
    if not firing:
        logger.info(
            "Alertmanager webhook: no firing alerts (status=%s, alerts=%d)",
            payload.status,
            len(payload.alerts),
        )
        log_audit_event(
            "alertmanager_no_firing",
            {"status": payload.status, "alerts": len(payload.alerts)},
            {},
        )
        return AlertmanagerWebhookResponse(processed=0, skipped=skipped, diagnoses=[])

    diagnoses: list[dict] = []
    for alert in firing:
        incident = alert.to_incident()
        logger.info(
            "Alertmanager firing alert: name=%s ns=%s pod=%s fingerprint=%s",
            alert.labels.get("alertname"),
            incident.namespace,
            incident.pod_name,
            alert.fingerprint,
        )
        try:
            diagnosis_dict = orchestrator.diagnose(incident)
        except NamespaceNotAllowedError as exc:
            # Guardrail: запрещённый namespace — алерт пропускаем, не роняем батч.
            logger.warning("Guardrail rejected alert: %s", exc)
            log_audit_event(
                "guardrail_rejected",
                incident.model_dump(),
                {"root_cause": f"guardrail: {exc}", "severity": "unknown"},
            )
            skipped += 1
            continue
        except Exception as exc:
            logger.exception("Diagnosis failed for alert %s", alert.labels.get("alertname"))
            log_audit_event(
                "diagnose_failed",
                incident.model_dump(),
                {"root_cause": f"exception: {exc}", "severity": "unknown"},
            )
            raise HTTPException(status_code=500, detail=f"Diagnosis failed: {exc}") from exc

        log_audit_event("alertmanager_diagnose", incident.model_dump(), diagnosis_dict)
        report = orchestrator.format_report(diagnosis_dict)
        diagnoses.append({"diagnosis": diagnosis_dict, "report": report})

    return AlertmanagerWebhookResponse(
        processed=len(diagnoses), skipped=skipped, diagnoses=diagnoses
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
