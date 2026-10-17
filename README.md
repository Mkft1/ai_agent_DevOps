# ai_agent_DevOps

AI-агент для диагностики инцидентов в Kubernetes.

## Что делает агент

- Читает **метрики** из Prometheus (загрузка CPU/RAM, ошибки, задержки).
- Собирает и анализирует **логи** контейнеров и компонентов кластера.
- Проверяет **состояние подов**, деплоев и других ресурсов Kubernetes.
- Формирует гипотезу о причине инцидента и предлагает **RCA** (Root Cause Analysis)
  с рекомендациями для on-call инженера.

## Структура проекта

```
ai_agent_DevOps/
├── README.md
├── requirements.txt
├── .gitignore
├── .env.example
├── src/
│   ├── main.py            # Точка входа FastAPI-приложения
│   ├── config.py          # Загрузка конфигурации из .env
│   ├── agent/
│   │   ├── orchestrator.py  # Оркестрация шагов диагностики (K8s -> контекст -> LLM -> RCA)
│   │   ├── llm_client.py    # Реальный LLM-клиент (Qwen / OpenAI-совместимый API, ретраи tenacity)
│   │   └── tools/
│   │       ├── k8s_tools.py         # Инструменты Kubernetes
│   │       ├── prometheus_tools.py  # Инструменты Prometheus
│   │       └── logs_tools.py        # Инструменты логов
│   └── utils/
│       └── logger.py      # Базовое логирование
├── tests/
├── Dockerfile             # многоступенчатый образ (builder + runtime, non-root)
├── docker-compose.yml     # локальный запуск сервиса agent (порт 8000)
└── .dockerignore          # .git/.env/tests/.venv/__pycache__ не попадают в образ
```

## Запуск

### Локально (без Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните переменные окружения
uvicorn src.main:app --reload
```

### В Docker

Сборка и запуск одним контейнером (секреты подгружаются только в рантайме, `.dockerignore` не пускает `.env` в образ):

```bash
cp .env.example .env                      # заполните QWEN_API_KEY / OPENAI_API_KEY и др.
docker build -t ai-agent-devops .         # многоступенчатая сборка: builder + runtime
docker run -d --name ai-agent \
  -p 8000:8000 \
  --env-file .env \
  -v ~/.kube/config:/home/appuser/.kube/config:ro \
  ai-agent-devops
```

### docker-compose (рекомендуемый локальный вариант)

Поднимает сервис `agent` на порту 8000, подгружает `.env` и монтирует kubeconfig только для чтения:

```bash
docker compose up --build -d
docker compose logs -f agent              # логи сервиса
docker compose down                       # остановка
```

Проверка работоспособности:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

## Переменные окружения

Все настройки читаются из `.env` (см. `.env.example`) через pydantic-settings. Ключи API храните только в `.env` — в репозиторий и в Docker-образ они не попадают.

| Переменная        | По умолчанию                                   | Описание                                                        |
|-------------------|------------------------------------------------|-----------------------------------------------------------------|
| `KUBECONFIG_PATH` | `~/.kube/config`                               | Путь к kubeconfig для доступа к кластеру Kubernetes             |
| `PROMETHEUS_URL`  | `http://localhost:9090`                        | Базовый URL Prometheus для запроса метрик                       |
| `LOG_LEVEL`       | `INFO`                                         | Уровень логирования: DEBUG / INFO / WARNING / ERROR / CRITICAL  |
| `OPENAI_API_KEY`  | —                                              | Ключ OpenAI (если используете OpenAI-совместимый API)           |
| `QWEN_API_KEY`    | —                                              | Ключ Qwen/DashScope (приоритетнее `OPENAI_API_KEY`)             |
| `LLM_BASE_URL`    | `https://dashscope.aliyuncs.com/compatible-mode` | Base URL LLM-API (OpenAI-совместимый: Qwen, vLLM, Ollama...)  |
| `LLM_MODEL`       | `qwen-max`                                     | Имя модели LLM                                                  |

## API

| Метод | Путь        | Описание                                                        |
|-------|-------------|-----------------------------------------------------------------|
| GET   | `/health`   | Живость сервиса → `{"status": "ok"}`                            |
| GET   | `/ready`    | Проверка доступности K8s API и Prometheus → 200 / 503           |
| POST  | `/diagnose` | Приём инцидента и запуск диагностики через оркестратор          |

Пример запроса:

```bash
curl -X POST http://localhost:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "prod", "pod_name": "web-1", "description": "Pod restarts alert"}'
# -> {"diagnosis": {root_cause, evidence, suggested_fix, severity, ...}, "report": "..."}
```

Swagger UI доступен на `/docs`. CORS открыт для всех origins (dev-режим).

## Тесты

```bash
python -m pytest tests/ -v
```

> Текущая версия: каркас + инструменты K8s/Prometheus/логов + оркестратор с реальным
> LLM-клиентом (Qwen/OpenAI-совместимый API) + HTTP-API.
