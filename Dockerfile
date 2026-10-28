# syntax=docker/dockerfile:1
# Multi-stage сборка AI-агента диагностики инцидентов в Kubernetes.
#
# base    -> системные build-зависимости (одинаковы для builder и любых future-стадий)
# builder -> установка зависимостей (CPU-only PyTorch ДО requirements.txt)
# runtime -> минимальный образ: только site-packages/bin из builder + код src/
#
# Секреты (.env, kubeconfig) в образ НЕ попадают — см. .dockerignore и runtime-монтирования.

# ---------------------------------------------------------------
# Stage 1: base — системные зависимости для сборки wheel'ов при необходимости.
# Все текущие зависимости (torch CPU, sklearn, streamlit, fastapi...) ставят
# manylinux-wheel'ы, поэтому набор минимален: build-essential оставлен как
# страховка для будущих sdist-зависимостей; libpq-dev не нужен (нет PostgreSQL).
# ---------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------
# Stage 2: builder — зависимости в системный site-packages.
# КЭШИРОВАНИЕ: requirements.txt копируется ОТДЕЛЬНО и ДО кода — правки кода
# не сбрасывают тяжёлый pip-слой (torch + sklearn + streamlit).
# CPU-only PyTorch ставится ПЕРВЫМ через index-url cpu: иначе sentence-
# transformers затянет CUDA-вариант torch (~2 ГБ+ CUDA-библиотеки).
# ---------------------------------------------------------------
FROM base AS builder

WORKDIR /build

# CPU-only PyTorch ставится ПЕРВЫМ и с явным пином: версия обязана совпадать
# с torch==X в requirements.txt, чтобы requirements-слой видел зависимость
# закрытой (wheel 2.14.1+cpu удовлетворяет specifier ==2.14.1 по PEP 440)
# и не тянул CUDA-сборку из PyPI. Индекс ниже ПЕРЕОПРЕДЕЛЯЕТ дефолтный PyPI.
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------
# Stage 3: runtime — чистый python:3.12-slim без компиляторов и кэшей pip.
# ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="ai-agent-devops" \
      org.opencontainers.image.description="AI-агент для диагностики инцидентов в Kubernetes"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root пользователь; домашний каталог для монтируемого kubeconfig
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /home/appuser/.kube \
    && chown -R appuser:appuser /home/appuser

WORKDIR /app

# Только установленные пакеты и консольные скрипты из builder
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Код приложения и нужные конфиги (ничего лишнего из build-контекста)
COPY src/ ./src/
COPY requirements.txt .env.example ./

USER appuser

EXPOSE 8000

# Лёгкая проверка живости через stdlib (curl в образе отсутствует)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
