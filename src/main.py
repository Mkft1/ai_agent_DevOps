"""Точка входа FastAPI-приложения AI-агента диагностики инцидентов в Kubernetes.

Эндпоинты:
    GET  /health    — живность процесса (без внешних зависимостей).
    GET  /ready     — проверка доступности K8s API и Prometheus (200/503).
    GET  /metrics   — Prometheus-метрики (HTTP-метрики + кастомные метрики агента).
    POST /diagnose  — приём инцидента, запуск IncidentOrchestrator, возврат RCA.

Логика диагностики целиком в оркестраторе — здесь только HTTP-обвязка.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
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

# ----------------------------------------------------------------------
# OpenAPI: enterprise-метаданные Swagger UI (/docs)
# ----------------------------------------------------------------------
API_DESCRIPTION = """
**AI DevOps Agent** — LLM-агент для диагностики инцидентов в Kubernetes.

Принимает инциденты (алерты) через REST и Alertmanager-webhook, собирает живые
сигналы кластера — состояние подов и события Kubernetes, метрики Prometheus,
логи контейнеров — обогащает анализ retrieval'ом по локальным runbook'ам
(**hybrid RAG**: TF-IDF + vector embeddings) и возвращает структурированный
**Root Cause Analysis (RCA)** плюс человекочитаемый отчёт для Slack/Telegram.

## Как это работает
`Alertmanager / curl / chat-bot → POST /diagnose → IncidentOrchestrator →
[K8sTools, PrometheusTools, LogsTools] → RunbookRAG → LLM (Qwen /
OpenAI-compatible) → RCA → Report`. Ответ LLM валидируется pydantic-схемой
с одним корректирующим retry и честным fallback-диагнозом.

## Guardrails (безопасность по умолчанию)
* **Read-only**: агент не выполняет изменений в кластере (`ENABLE_WRITE_ACTIONS=false`);
* **Namespace allow-list** (`ALLOWED_NAMESPACES`): запросы вне списка → `403` до любых обращений к кластеру;
* **Policy-as-Code**: опасные рекомендации (`delete`, `drop`, `rm -rf`, `scale 0`)
  помечаются `requires_approval: true` и уходят на аппрув человеку в Slack;
* **Bounded context**: лимиты `MAX_LOG_LINES` / `MAX_CONTEXT_CHARS`; секреты только из `.env`.

## Наблюдаемость
`/metrics` (Prometheus), structured JSON-логи (`LOG_FORMAT=json`), `X-Request-ID`
в ответах, OpenTelemetry-трейсы (OTLP), журнал аудита `audit.jsonl` и Streamlit-
дашборд оператора (`make dashboard`). Эксплуатация — `docs/OPERATIONS.md`.
"""

OPENAPI_TAGS = [
    {
        "name": "Health & Status",
        "description": "Пробы живости/готовности и статус внешних зависимостей "
        "(Kubernetes API, Prometheus).",
    },
    {
        "name": "Diagnostics",
        "description": "AI-диагностика инцидентов: сбор сигналов кластера, RAG по "
        "runbook'ам, RCA от LLM.",
    },
    {
        "name": "Webhooks",
        "description": "Приёмники внешних систем алертинга (Prometheus Alertmanager).",
    },
]

app = FastAPI(
    title="AI DevOps Agent API",
    description=API_DESCRIPTION,
    version="1.0.0",
    contact={"name": "AI DevOps Team", "url": "https://github.com/Mkft1/ai_agent_DevOps"},
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    openapi_tags=OPENAPI_TAGS,
    swagger_ui_parameters={"syntaxHighlight.theme": "obsidian"},
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


# ----------------------------------------------------------------------
# OpenTelemetry: распределённый трейсинг (Jaeger/Tempo через OTLP gRPC).
# Экспорт включается только при заданном OTEL_EXPORTER_OTLP_ENDPOINT;
# без него спаны собираются in-process и никуда не отправляются.
# ----------------------------------------------------------------------
def setup_tracing(settings: Settings) -> TracerProvider:
    """Инициализировать OTel TracerProvider и OTLP-экспорт (если настроен).

    Args:
        settings: конфигурация приложения (``otel_service_name``,
            ``otel_exporter_otlp_endpoint``).

    Returns:
        Активный :class:`TracerProvider` (SDK) — тесты могут добавлять
        в него своих span-процессоров (например, InMemorySpanExporter).
    """
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if settings.otel_exporter_otlp_endpoint:
        # Jaeger all-in-one слушает gRPC OTLP на 4317 (схема http -> insecure).
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
        logger.info("OTel tracing: exporting to %s", settings.otel_exporter_otlp_endpoint)
    else:
        logger.info("OTel tracing: OTEL_EXPORTER_OTLP_ENDPOINT not set — export disabled")
    trace.set_tracer_provider(provider)
    return provider


setup_tracing(settings)
# Авто-спаны на каждый HTTP-запрос (method, path, status, latency).
FastAPIInstrumentor.instrument_app(app)

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

    namespace: str = Field(
        default="default",
        description="Kubernetes namespace, в котором произошёл инцидент. "
        "Должен входить в ALLOWED_NAMESPACES, иначе запрос отклоняется (403).",
        examples=["default"],
    )
    pod_name: str | None = Field(
        default=None,
        description="Имя целевого пода (опционально). Если не указано, агент "
        "попробует вывести под из deployment_name или списка подов namespace.",
        examples=["payments-api-7d9f4b-x2kq"],
    )
    deployment_name: str | None = Field(
        default=None,
        description="Имя деплоймента (опционально): используется для поиска "
        "целевого пода по префиксу имени, если pod_name не задан.",
        examples=["payments-api"],
    )
    description: str = Field(
        default="",
        description="Текст алерта / свободное описание проблемы. Используется "
        "как поисковый запрос RAG по runbook'ам и контекст для LLM.",
        examples=["Pod payments-api restarts repeatedly, alert: ContainerOOMKilling"],
    )

    def to_incident(self) -> Incident:
        """Конвертировать HTTP-модель в доменную модель оркестратора."""
        return Incident.model_validate(self.model_dump())


class DiagnosisResponse(BaseModel):
    """Ответ POST /diagnose: диагноз LLM + читаемый отчёт для мессенджеров."""

    diagnosis: Diagnosis = Field(
        description="Структурированный RCA: root_cause, evidence, suggested_fix, "
        "severity, warnings, requires_approval и сырые данные сбора.",
    )
    report: str = Field(
        description="Готовый текст отчёта для Slack/Telegram (emoji-разметка).",
        examples=["🟠 *Инцидент: диагностика* (severity: high)\n*Namespace:* `default`..."],
    )


class ReadyResponse(BaseModel):
    """Ответ GET /ready: статусы внешних зависимостей."""

    status: str = Field(
        description="`ready`, если все зависимости доступны, иначе `degraded`.",
        examples=["ready"],
    )
    checks: dict[str, str] = Field(
        description="Статусы проверок по имени зависимости: `ok` или "
        "`unavailable: <причина>` (kubernetes, prometheus).",
        examples=[{"kubernetes": "ok", "prometheus": "ok"}],
    )


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
@app.get(
    "/health",
    tags=["Health & Status"],
    summary="Liveness probe",
    description="Liveness probe. Returns 200 if the application is running.",
)
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


async def _check_prometheus(s: Settings) -> str:
    """Лёгкая проверка доступности Prometheus (GET /-/ready, таймаут 3 с).

    Асинхронно (``httpx.AsyncClient``): не блокирует event loop FastAPI.

    Returns:
        "ok" | "unavailable: <причина>".
    """
    url = s.prometheus_url.rstrip("/") + "/-/ready"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return "ok"
        return f"unavailable: HTTP {resp.status_code}"
    except Exception as exc:
        logger.warning("Prometheus readiness check failed: %s", exc)
        return f"unavailable: {exc}"


@app.get(
    "/ready",
    tags=["Health & Status"],
    summary="Readiness probe",
    description="Readiness probe. Checks K8s API and Prometheus connectivity.",
    response_model=ReadyResponse,
)
async def ready() -> JSONResponse:
    """Проверка готовности: доступность K8s API и Prometheus.

    Возвращает 200, если обе зависимости доступны, иначе 503. Тело ответа
    содержит статус каждой проверки. Prometheus опрашивается асинхронно
    (httpx), K8s-проверка (sync-клиент) вынесена в worker-поток, чтобы не
    блокировать event loop.
    """
    checks = {
        "kubernetes": await asyncio.to_thread(_check_kubernetes, settings),
        "prometheus": await _check_prometheus(settings),
    }
    all_ok = all(v == "ok" for v in checks.values())
    body = ReadyResponse(status="ready" if all_ok else "degraded", checks=checks)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=body.model_dump(),
    )


@app.post(
    "/diagnose",
    tags=["Diagnostics"],
    summary="Diagnose Incident",
    description="Accepts incident details and returns an AI-generated Root "
    "Cause Analysis (RCA) in JSON format.",
    response_model=DiagnosisResponse,
)
async def diagnose(
    payload: DiagnoseRequest,
    orchestrator: IncidentOrchestrator = Depends(get_orchestrator),
) -> DiagnosisResponse:
    """Принять инцидент и запустить диагностику через IncidentOrchestrator.

    Эндпоинт не дублирует логику агента: только валидация входа, вызов
    ``orchestrator.diagnose()`` и форматирование отчёта.
    """
    incident = payload.to_incident()
    try:
        diagnosis_dict = await orchestrator.adiagnose(incident)
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


@app.post(
    "/webhook/alertmanager",
    tags=["Webhooks"],
    summary="Alertmanager webhook",
    description="Webhook receiver for Prometheus Alertmanager. Parses alerts "
    "and triggers diagnosis automatically.",
    response_model=AlertmanagerWebhookResponse,
)
async def webhook_alertmanager(
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
            diagnosis_dict = await orchestrator.adiagnose(incident)
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
