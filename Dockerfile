# syntax=docker/dockerfile:1
# Многоступенчатая сборка образа AI-агента диагностики инцидентов в Kubernetes.
# Секреты (.env, kubeconfig) в образ НЕ попадают — см. .dockerignore.

# ---------------------------------------------------------------
# Stage 1: builder — установка зависимостей в изолированный venv
# ---------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Отдельный слой под requirements: кэш не сбрасывается при правках кода
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------
# Stage 2: runtime — минимальный итоговый образ
# ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="ai-agent-devops" \
      org.opencontainers.image.description="AI-агент для диагностики инцидентов в Kubernetes"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root пользователь; домашний каталог для монтируемого kubeconfig
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /home/appuser/.kube \
    && chown -R appuser:appuser /home/appuser/.kube

# Зависимости из builder-стадии (только site-packages venv, без pip-кэша)
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src/ ./src/

USER appuser

EXPOSE 8000

# Лёгкая проверка живости через stdlib (curl в образе отсутствует)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
