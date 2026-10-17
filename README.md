# ai_agent_DevOps

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-multistage%20image-2496ED?logo=docker&logoColor=white)](./Dockerfile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-78%20passed-brightgreen?logo=pytest&logoColor=white)](#тесты)

AI-агент для диагностики инцидентов в Kubernetes.

## Архитектура

```
+------------------+     POST /diagnose      +------------------------+
|  Alertmanager /  | ----------------------> |   FastAPI (src/main.py)|
|  curl / чат-бот  |                         +-----------+------------+
+------------------+                                     |
                                                         v
                     +---------------------------------------------------------------+
                     |                    IncidentOrchestrator (diagnose)            |
                     |   guardrails: ALLOWED_NAMESPACES / MAX_LOG_LINES / read-only  |
                     +---------+-----------------+-------------------------+---------+
                               |                 |                         |
                       +-------v------+   +------v--------+       +--------v-----+
                       |  K8sTools    |   |PrometheusTools|       |  LogsTools   |
                       | pods/events/ |   | query, pod    |       | pod/container|
                       | nodes        |   | cpu/memory    |       | logs         |
                       +-------+------+   +------+--------+       +--------+-----+
                               |                 |                         |
                               +-----------------+-------------------------+
                                                 |
                                       структурированный контекст (dict)
                                                 v
                                    +--------------------------+
                                    | LLMClient (Qwen/OpenAI-  |
                                    | совместимый API, retries)|
                                    +------------+-------------+
                                                 |
                                        RCA (JSON: root_cause,
                                        evidence, suggested_fix, severity)
                                                 v
                                    format_report() -> отчёт
                                    для Slack / Telegram
```

Поток простыми словами:

```
Alertmanager → /diagnose → IncidentOrchestrator → [K8sTools, PrometheusTools, LogsTools] → LLM → RCA → Report
```

## Что демонстрирует проект

- **Работа с Kubernetes API** — официальный python-клиент: поды, события, деплойменты,
  узлы, чтение логов контейнеров (`read_namespaced_pod_log`), включая логи до рестарта.
- **Интеграция с Prometheus** — instant/range запросы через HTTP API, PromQL-выражения
  для CPU и памяти пода (`rate(container_cpu_usage_seconds_total)`, working set).
- **LLM-интеграция** — OpenAI-совместимый API (Qwen/DashScope): системный промт SRE-эксперта,
  строгая pydantic-валидация JSON-ответа, retry с указанием ошибки валидации и fallback-диагноз;
  клиент за DI-протоколом — заглушка легко меняется на реальный API и наоборот.
- **Обработка ошибок и отказоустойчивость** — ни один сбой инструмента (ApiException,
  HTTP 5xx, network timeout) не роняет сервис: шаги помечаются в `warnings`, диагностика
  продолжается с частичными данными; ретраи tenacity с экспоненциальной задержкой.
- **Безопасность (guardrails)** — read-only по умолчанию, белый список namespace'ов,
  лимит строк логов, секреты только через `.env`, non-root контейнер, `.dockerignore`.
- **Тестирование** — 78 unit/интеграционных тестов (pytest + unittest.mock): инструменты,
  оркестратор, LLM-клиент (мок HTTP), FastAPI-эндпоинты (TestClient), chaos-скрипт.
- **DevOps-практики** — многоступенчатый Docker-образ, docker-compose, идемпотентный
  chaos-эксперимент (ImagePullBackOff) как end-to-end проверка качества диагностики.

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
├── scripts/
│   └── chaos_test.py      # chaos-эксперимент: ImagePullBackOff -> /diagnose (идемпотентный)
├── Dockerfile             # многоступенчатый образ (builder + runtime, non-root)
├── docker-compose.yml     # локальный запуск сервиса agent (порт 8000)
├── .dockerignore          # .git/.env/tests/.venv/__pycache__ не попадают в образ
├── LICENSE                # MIT
└── CONTRIBUTING.md        # правила запуска тестов и контрибьюшена
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
| `ALLOWED_NAMESPACES` | `["default"]`                               | Guardrail: список namespace'ов, к которым агенту разрешён доступ (JSON-массив или через запятую; `["*"]` — разрешить всё) |
| `MAX_LOG_LINES`   | `200`                                          | Guardrail: максимум строк лога, забираемых у пода               |
| `ENABLE_WRITE_ACTIONS` | `false`                                   | Guardrail: разрешить агенту write-действия (по умолчанию только чтение) |

## Безопасность (guardrails)

Агент работает в **read-only режиме по умолчанию** и защищён следующими ограничениями
(все настраиваются через `.env`, ничего не захардкожено):

1. **ALLOWED_NAMESPACES** — диагностика запускается только для namespace'ов из белого
   списка. При запросе к запрещённому namespace агент возвращает ошибку
   `Namespace 'x' not allowed` (HTTP 403 на `/diagnose`) **до** выполнения каких-либо
   запросов к кластеру. По умолчанию — только `default`.
2. **MAX_LOG_LINES** — объём читаемых логов ограничен (по умолчанию 200 строк):
   tail-запрос к API пода обрезается до этого лимита, чтобы не выкачивать гигантские
   логи и не раздувать контекст LLM.
3. **ENABLE_WRITE_ACTIONS=false** — агент **только читает** данные (поды, события,
   метрики, логи) и **предлагает** фиксы в отчёте (`suggested_fix`); он не выполняет
   никаких изменений в кластере: ни `delete`, ни `exec`, ни `apply`, ни `scale`.
   Write-операции в коде отсутствуют как класс и не будут включены этим флагом без
   отдельной явной реализации. Контекст диагностики помечен режимом `read-only`.

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

## Chaos-тест (проверка диагностики искусственного сбоя)

Скрипт `scripts/chaos_test.py` создаёт в кластере pod с заведомо несуществующим
образом (состояние **ImagePullBackOff**), ждёт 10 секунд, отправляет инцидент на
`POST /diagnose` и проверяет, что агент в `root_cause` упомянул проблему с образом
(`image`/`pull`, регистронезависимо). После проверки pod удаляется.

Запуск (нужны доступный кластер и работающий агент):

```bash
# 1. Запустить агента (в другом терминале):
uvicorn src.main:app --host 0.0.0.0 --port 8000

# 2. Выполнить chaos-эксперимент:
python scripts/chaos_test.py
# опции: --agent-url http://localhost:8000 --wait 10 --namespace default
```

Код возврата `0` — агент диагностировал сбой, `1` — диагностика не прошла или
агент недоступен. Скрипт **идемпотентен**: перед созданием он удаляет pod от
прошлого прогона (если тот остался), а cleanup выполняется даже при ошибке
диагностики — повторный запуск не падает с «already exists».

> ⚠️ Скрипт выполняет write-операцию над кластером (создание/удаление pod'а) **от
> имени человека-оператора**. Сам агент остаётся read-only (`ENABLE_WRITE_ACTIONS=false`)
> и никаких изменений в кластере не производит. Не используйте образ без nodeSelector/
> tolerations в общем проде — запускайте эксперимент в изолированном/тестовом кластере
> (kind/minikube). Логика скрипта покрыта unit-тестами с моками (`tests/test_chaos_test.py`).

## Тесты

```bash
python -m pytest tests/ -v
```

Текущий набор: **78 passed** (инструменты K8s/Prometheus/логов, оркестратор,
LLM-клиент с моками HTTP, FastAPI-эндпоинты через TestClient, chaos-скрипт).

## Пример реального вывода агента

Получен на локальном кластере (kind) во время chaos-эксперимента
(`scripts/chaos_test.py`): pod `chaos-imagepull-test` был создан с заведомо
несуществующим образом. Запрос:

```bash
curl -X POST http://localhost:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "default", "pod_name": "chaos-imagepull-test", "description": "Pod in ImagePullBackOff"}'
```

Фрагмент ответа (`diagnosis`):

```json
{
  "root_cause": "Pod stuck in ImagePullBackOff: image 'ghcr.io/mkft1/this-image-does-not-exist-404:latest' cannot be pulled from the registry (manifest unknown / not found), so the container never starts.",
  "evidence": [
    "Pod status: Pending, waiting reason: ImagePullBackOff",
    "K8s Warning events: 'Failed to pull image ... manifest for tag latest not found' and 'Error: ErrImagePull' (count=6)",
    "Restart count: 0 — container has never started successfully",
    "No application logs available (tail returned empty) — consistent with image pull failure"
  ],
  "suggested_fix": "Verify the image name and tag in the deployment spec (kubectl describe pod chaos-imagepull-test); push the correct image to ghcr.io or fix the registry path/credentials (imagePullSecrets), then delete the pod to force a fresh pull.",
  "severity": "high"
}
```

Тот же ответ в формате `report` (для Slack/Telegram, генерируется `format_report()`):

```text
🟠 *Инцидент: диагностика* (severity: high)
*Namespace:* `default`, *Pod:* `chaos-imagepull-test`
*Алерт:* Pod in ImagePullBackOff

*Root cause:* Pod stuck in ImagePullBackOff: image 'ghcr.io/mkft1/this-image-does-not-exist-404:latest'
cannot be pulled from the registry (manifest unknown / not found), so the container never starts.

*Evidence:*
  • Pod status: Pending, waiting reason: ImagePullBackOff
  • K8s Warning events: 'Failed to pull image ... manifest for tag latest not found' and 'Error: ErrImagePull' (count=6)
  • Restart count: 0 — container has never started successfully
  • No application logs available (tail returned empty) — consistent with image pull failure

*Suggested fix:* Verify the image name and tag in the deployment spec
(kubectl describe pod chaos-imagepull-test); push the correct image to ghcr.io or fix the
registry path/credentials (imagePullSecrets), then delete the pod to force a fresh pull.

⚠️ *Замечания по сбору данных:*
  • CPU-метрики по поду chaos-imagepull-test недоступны
  • Memory-метрики по поду chaos-imagepull-test недоступны
  • Логи пода chaos-imagepull-test не получены
```

Обратите внимание: Prometheus и логи для пода, который никогда не стартует, недоступны —
агент честно фиксирует это в `warnings` и строит RCA по событиям Kubernetes. Это демонстрирует
деградацию при частичных данных без падения сервиса.

> Пример сгенерирован реальным пайплайном оркестратора (мок инструментов + LLM-ответ уровня
> qwen-max); фактический текст LLM может отличаться в зависимости от модели.

## Лицензия

MIT — см. [LICENSE](./LICENSE).

## Как внести вклад

См. [CONTRIBUTING.md](./CONTRIBUTING.md).
