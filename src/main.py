"""Точка входа FastAPI-приложения AI-агента диагностики инцидентов в Kubernetes.

Эндпоинты:
    GET  /health    — живность процесса (без внешних зависимостей).
    GET  /ready     — проверка доступности K8s API и Prometheus (200/503).
    POST /diagnose  — приём инцидента, запуск IncidentOrchestrator, возврат RCA.

Логика диагностики целиком в оркестраторе — здесь только HTTP-обвязка.
"""

from __future__ import annotations

import os

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from src.agent.orchestrator import (
    Diagnosis,
    Incident,
    IncidentOrchestrator,
    NamespaceNotAllowedError,
)
from src.config import Settings, get_settings
from src.utils.logger import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)

app = FastAPI(
    title="AI Agent DevOps",
    description="AI-агент для диагностики инцидентов в Kubernetes (метрики, логи, состояние подов, RCA).",
    version="0.1.0",
)

# CORS: для dev-режима разрешаем все origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # при allow_origins=["*"] браузеры всё равно не отдадут cookies
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    """Возвращает singleton оркестратора (создаётся при первом запросе)."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = IncidentOrchestrator()
    return _orchestrator


def run_health_check() -> dict[str, str]:
    """Ядро /health — вынесено в функцию для возможности моканирования в тестах."""
    return {"status": "ok"}


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
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Diagnosis failed for incident %s: %s", incident, exc)
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {exc}") from exc

    report = orchestrator.format_report(diagnosis_dict)
    return DiagnosisResponse(
        diagnosis=Diagnosis.model_validate(diagnosis_dict),
        report=report,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
