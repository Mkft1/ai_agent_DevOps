"""Точка входа FastAPI-приложения AI-агента диагностики инцидентов в Kubernetes."""

from fastapi import FastAPI

from src.config import get_settings
from src.utils.logger import setup_logging

settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(
    title="AI Agent DevOps",
    description="AI-агент для диагностики инцидентов в Kubernetes (метрики, логи, состояние подов, RCA).",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Проверка работоспособности сервиса."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
