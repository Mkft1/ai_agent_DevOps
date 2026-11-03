# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Slack Interactive Buttons для аппрува опасных рекомендаций (двусторонняя
  интеграция): `_notify_slack_approval` теперь отправляет **Block Kit**-сообщение —
  секция с контекстом инцидента (namespace/pod/root cause/suggested fix/incident
  ID) и кнопки **✅ Approve** (style=primary) / **❌ Reject** (style=danger) с
  `value = approve:<incident_id>` / `reject:<incident_id>`; поле `text` сохраняет
  прежний текстовый формат (push-уведомления, обратная совместимость). Новое поле
  `Diagnosis.incident_id` (`uuid4().hex[:12]`, default_factory) коррелирует
  Slack-решение с записью аудита диагностики (`incident_id` добавлен в
  `_RESULT_FIELDS` журнала аудита). Новый эндпоинт
  `POST /webhook/slack-interactivity` (src/main.py): парсит form-поле `payload`
  Slack Interactive Components, извлекает `actions[0].value`, пишет событие аудита
  `slack_approve`/`slack_reject` (incident_id, действие, пользователь, канал) и
  отвечает `200 Approved`/`Rejected`; ошибки парсинга → `400`, неизвестное
  действие → `400`. Валидация `X-Slack-Signature` по официальной схеме Slack
  (HMAC-SHA256 `v0:{timestamp}:{body}`, constant-time сравнение, replay-окно
  5 минут) при заданном `SLACK_SIGNING_SECRET`; невалидная подпись → `401`; без
  секрета — dev-режим с WARNING. Конфигурация: `SLACK_SIGNING_SECRET` и
  `SLACK_APP_TOKEN` (Socket Mode, зарезервирован) в `src/config.py` и
  `.env.example`; rate limit эндпоинта 30/мин. Токены/секреты не логируются.
  Тесты: `tests/test_slack_interactivity.py` — 26 тестов (структура Block Kit,
  incident_id формат/уникальность/roundtrip, approve/reject + аудит, 400-ветки,
  подпись: неверная/чужая/устаревшая/подменённое тело → 401, dev-режим,
  корреляция incident_id в аудите диагностики). Документация: подразделы
  «Interactive Approval Buttons (Slack)» / «Интерактивные кнопки аппрува (Slack)»
  в README.md/README.ru.md (настройка Slack App: Incoming Webhooks,
  Interactivity & Shortcuts Request URL, Signing Secret, Event Subscriptions),
  строка эндпоинта в API-таблицах. Примечание: транспорт уведомления —
  `httpx.AsyncClient` (как в существующем коде), а не `requests` из текста
  задачи — тесты мокают фактический транспорт.
- OIDC authentication and role-based authorization (`src/auth.py`): опциональная
  JWT-защита `POST /diagnose` и `POST /webhook/alertmanager` (`OIDC_ENABLED`,
  по умолчанию `false` — полная обратная совместимость). JWT проверяется целиком
  на стороне API: подпись по JWKS провайдера (OIDC Discovery
  `{issuer}/.well-known/openid-configuration`, кэш ключей 300 с), обязательные
  claims `iss`/`aud`/`exp`; разрешены только асимметричные алгоритмы
  RS/PS/ES/EdDSA (`HS256` отклоняется — защита от algorithm-confusion);
  fail-closed: невалидный/просроченный токен или недоступный провайдер → `401`
  (`WWW-Authenticate: Bearer`). RBAC по claim `roles`/`groups`:
  `ALLOWED_ROLES_DIAGNOSE` (default `["sre-user", "sre-admin"]`) и
  `ALLOWED_ROLES_WEBHOOK` (default `["alertmanager", "sre-admin"]`),
  недостаточная роль → `403`; аутентификация выполняется до создания
  оркестратора (401/403 раньше 503). Вебхук Alertmanager дополнительно
  принимает shared-секрет в заголовке `X-Webhook-Secret`
  (`WEBHOOK_SHARED_SECRET`, constant-time сравнение `secrets.compare_digest`) —
  для интеграций без возможности подписывать JWT. `/health`, `/ready`,
  `/metrics` не защищаются никогда (kubelet-пробы, Prometheus-scraping).
  Токены и секреты не попадают в логи. Fail-fast валидация конфигурации:
  `OIDC_ENABLED=true` без `OIDC_ISSUER_URL`/`OIDC_AUDIENCE` не запускается.
  Streamlit-дашборд (`ui/dashboard.py`): при включённом OIDC — экран логина
  (ссылка на провайдера + поле ввода JWT, хранение только в
  `st.session_state["user_token"]`), пользователь/роли в сайдбаре с кнопкой
  Logout, а «Approve & Apply» рендерится только для `sre-admin`/`devops-admin`
  (клиентское декодирование claims без проверки подписи; реальная авторизация —
  на API). Зависимости: `authlib==1.9.2` + `itsdangerous==2.2.0`; ОТКЛОНЕНИЕ от
  версий спецификации (authlib 1.3.0, python-jose 3.3.0): authlib 1.3.0 имеет
  12 advisory pip-audit (исправлены в 1.6.9–1.9.1), python-jose исключён —
  неисправленный PYSEC-2026-4298 (HS256 algorithm-confusion, fix нет даже в
  3.5.0) плюс зависимость ecdsa с PYSEC-2026-1325; блокирующий dependency-audit
  CI не пропустил бы эти пины. Тесты: `tests/test_auth.py` — 71 тест (реальные
  RS256-JWT против фейкового JWKS-провайдера: подпись/iss/aud/exp, tampering,
  HS256, unknown kid, fail-closed, кэш JWKS и TTL, 401/403 на HTTP-слое,
  открытые пробы, webhook-секрет, отсутствие токенов/секретов в логах,
  UI-логин и gating аппрувов через AppTest). Документация: раздел
  «Authentication & Authorization (OIDC)» в README.md/README.ru.md (флоу,
  примеры Keycloak/Auth0/Azure AD, ролевая модель), переменные в `.env.example`.
- Extended policy guardrail (`src/agent/policies.py`): двухуровневая защита от
  опасных рекомендаций LLM. Уровень 1 — 18 регистронезависимых regex вместо
  4 substring'ов: к `delete`/`drop`/`rm -rf`/`scale 0` добавлены синонимы и
  перефразировки (`remove the pod`, `terminate all instances/pods`,
  `kill the pod`, `set replicas to 0/zero`, `scale down to 0/zero`,
  `replicas: 0`/`--replicas=0`, `kubectl apply|create -f`, `drop table`,
  `truncate table`, `chmod 777`, `chown root`, `export ...=...secret`,
  `echo ... > /etc/passwd`) —
  каждый паттерн задокументирован пояснением опасности. Уровень 2 — ЛЮБОЕ
  упоминание `kubectl`/`helm`/`oc`, не являющееся read-only-командой
  (`get`/`describe`/`logs`), автоматически требует аппрува и логирует WARNING
  «Policy guardrail: kubectl/helm command detected, requiring approval».
  Новый Prometheus-счётчик `policy_approval_required_total` инкрементируется
  на каждый вердикт `requires_approval=true`. Таблицы паттернов — в
  README.md/README.ru.md («Security Policies & Approvals»).
- PII/secret scrubbing before LLM calls (`src/utils/scrubber.py`): JWT, API-ключи
  (`sk-`/`ghp_`/`AKIA`/`key-`), пароли в URL, Bearer-токены и email в логах подов и
  K8s-событиях маскируются на `[REDACTED:<type>]` **до** сериализации контекста для
  внешнего LLM-провайдера (интеграция в `IncidentOrchestrator`); ответ модели
  (root_cause/evidence/suggested_fix) не маскируется. Конфигурация:
  `ENABLE_SCRUBBING` (default true; false — passthrough для доверенных локальных
  моделей), `REDACT_IPS` (default false — IP сохраняются для сетевой диагностики);
  Prometheus-счётчик `scrubbed_items_total{type}`. Обязательно для Enterprise
  compliance review.
- Security scanning in CI: job `dependency-audit` (pip-audit, блокирующий аудит
  runtime-зависимостей; dev-зависимости report-only, т.к. не входят в образ)
  параллельно с тестами; job `security-scan` после build (Hadolint Dockerfile +
  Trivy-скан образа: severity CRITICAL,HIGH, exit-code 1, ignore-unfixed, кэш БД
  Trivy через actions/cache; пересборка образа из тёплого gha-кэша) параллельно
  с e2e; оба job отключаемы переменной `SKIP_SECURITY_SCAN=true`.
- Precise token-based context control: `MAX_CONTEXT_CHARS` заменён на
  `MAX_CONTEXT_TOKENS` (default 4000); `truncate_context` и `estimate_tokens`
  используют tiktoken `cl100k_base` (fallback chars/4 offline), итеративно усекая
  самые длинные поля до строгого соответствия лимиту токенов с маркером
  `... [truncated to fit token limit]` — гарантия от переполнения контекстного
  окна у любого LLM-провайдера.
- API rate limiting (slowapi): `POST /diagnose` — 5/minute, `POST /webhook/alertmanager` —
  30/minute; 429 с JSON `{"detail": "Rate limit exceeded. Please retry later."}` и
  заголовком `Retry-After`; MemoryStorage по умолчанию, `RATE_LIMIT_REDIS_URL` —
  Redis для общих счётчиков между репликами; `/health` и `/ready` без лимитов.
- Docs metrics drift guard: `scripts/check_docs_metrics.py` (`make docs-check`,
  шаг CI в test-job) — сверяет задекларированные в README.md/README.ru.md числа
  тестов и покрытия с реальностью (`pytest --collect-only` + `coverage.xml`)
  и падает с перечислением расхождений; юнит-тесты парсеров и EN/RU-консистентности.

### Fixed

- `fix(docker)`: builder-стадия переведена на изолированный `/opt/venv` — оверлей
  системного site-packages оставлял в runtime старые `*.dist-info` базового образа
  (`wheel 0.45.1`, CVE-2026-24049; `jaraco.context 5.3.0`, CVE-2026-23949), которые
  апгрейд пинов в builder не удалял; теперь в runtime копируется только venv
  (`setuptools==84.2.1` — явный пин ≥ фикса CVE-2025-47273, wheel/jaraco.context
  в venv не попадают), цель Trivy image-scan — Total: 0 CRITICAL/HIGH.
- `fix(docker)`: промежуточный апгрейд build-инструментов в builder
  (`setuptools`/`wheel`/`jaraco.context`) + hadolint-ignore для pip cache-mount.

## [1.0.0] - 2026-10-19

Первый полноценный релиз: production-готовый AI-агент диагностики инцидентов
Kubernetes с наблюдаемостью, guardrails, evaluation-фреймворком и операторскими
инструментами.

### Added

- **Incident diagnosis pipeline**: FastAPI-сервис (`POST /diagnose`) — сбор
  сигналов кластера (K8sTools: pods/events/deployments/nodes; PrometheusTools:
  CPU/memory; LogsTools: логи контейнеров, включая pre-restart), структурированный
  контекст → LLM (Qwen / любой OpenAI-совместимый API, tenacity-ретраи 429/5xx)
  → валидированный pydantic-схемой RCA (root_cause / evidence / suggested_fix /
  severity) с одним корректирующим retry и честным fallback-диагнозом;
  `GET /health`, `GET /ready`, `GET /metrics`.
- **Hybrid RAG over runbooks**: база из 8 markdown-runbook'ов
  (`docs/runbooks/`: OOMKilled, ImagePullBackOff, CrashLoop, DNS, NodeNotReady,
  PVCPending, CreateContainerConfigError, PodEvicted); retrieval =
  `0.5 × TF-IDF + 0.5 × cosine similarity` эмбеддингов sentence-transformers
  (`all-MiniLM-L6-v2`), без векторных БД и внешних сервисов; найденные runbook'и
  попадают в контекст LLM как `relevant_runbooks`.
- **Alertmanager integration**: `POST /webhook/alertmanager` — парсинг стандартного
  пейлоада v4, авто-диагностика firing-алертов, 400 на битом JSON/схеме,
  guardrail-skip запрещённых namespace'ов без падения батча.
- **Audit journal**: append-only JSON Lines (`AUDIT_LOG_PATH`) — каждая
  диагностика, guardrail-отклонение и webhook-батч одной JSON-строкой.
- **OpenTelemetry tracing**: авто-спаны HTTP (FastAPIInstrumentor) + ручные спаны
  пайплайна (`incident.diagnose` → `k8s.get_pods` / `k8s.get_events` /
  `prometheus.pod_cpu` / `prometheus.pod_memory` / `logs.pod_tail` / `rag.search` /
  `llm.generate`) с атрибутами и ERROR-статусами; OTLP-экспорт в Jaeger/Tempo
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `make jaeger`).
- **Guardrails & Policy-as-Code**: read-only by default (`ENABLE_WRITE_ACTIONS`),
  namespace allow-list (`ALLOWED_NAMESPACES` → 403), лимиты `MAX_LOG_LINES` /
  `MAX_CONTEXT_CHARS`; политика опасных действий (`delete`/`drop`/`rm -rf`/
  `scale 0` → `requires_approval: true`) со Slack-аппрувом
  («⚠️ Action Required: … Approve or Reject?»); Kyverno ClusterPolicies
  (lockdown kube-system для несистемных SA; write-действия агента только в
  namespace'ах с `agent-managed=true`) и least-privilege RBAC
  (`manifests/agent-rbac.yaml`, Role только get/list/watch).
- **Evaluation framework (LLM-as-a-judge)**: `eval/golden_dataset.json`
  (золотые инциденты) + `eval/evaluate.py` — прогон через реальный пайплайн без
  кластера, семантическая оценка судьи 1–5, runbook hit-rate, exit-коды как
  regression-гейт (`make eval`).
- **Operator tooling**: Streamlit-дашборд (`make dashboard`) — метрики
  Total/Critical/Pending Approvals, таблица инцидентов с цветовым кодированием
  severity и читаемым временем, карточки аппрува с «✅ Approve & Apply»,
  expander сырого журнала; on-call runbook `docs/OPERATIONS.md`
  (incident response, handling warnings, runbooks, golden dataset,
  troubleshooting 429/embeddings).
- **Kubernetes deployment**: production Helm chart `charts/ai-agent` v0.1.0
  (Deployment с probes и non-root securityContext, Service, SA+RBAC, ConfigMap,
  Secret с поддержкой external-secrets, `make helm-lint` / `make helm-install`).
- **CI/CD**: GitHub Actions — lint (ruff) → tests (pytest + coverage, гейт
  ≥85%) → Docker build + smoke `/health` → kind e2e (ImagePullBackOff →
  `/diagnose`, push в main) → helm lint/template; `workflow_dispatch`.
- **Developer experience**: Makefile (install/lint/test/run/docker-build/chaos/
  e2e/jaeger/eval/dashboard/helm-*), chaos-эксперимент `scripts/chaos_test.py`,
  kind-интеграционный тест `scripts/run_integration_test.sh`.
- **Docs & standards**: двуязычные README (EN/RU) с превью дашборда и Swagger,
  CONTRIBUTING.md, CHANGELOG.md (Keep a Changelog), шаблон PR и issue-шаблон
  bug report, enterprise-Swagger (`/docs`: теги, summary/description, схемы с
  examples, тема obsidian, version 1.0.0).

### Changed

- Dockerfile: build-инструменты базового образа (setuptools/wheel/jaraco.context)
  подняты до версий с закрытыми CVE (Trivy HIGH: CVE-2025-47273, CVE-2026-24049,
  CVE-2026-23949); hadolint-ignore для намеренных практик (pip cache-mount,
  apt без пина в builder).
- CI optimization (цель 5-6 минут вместо ~11): lint и helm идут параллельно;
  test зависит только от lint и крутится c `pytest -n auto` (pytest-xdist);
  build (Docker + smoke) ждёт [test, helm] и не запускается на PR;
  integration-test ждёт только test и идёт параллельно с build (только main/
  workflow_dispatch); Docker BuildKit-кэш `cache-to: type=gha,mode=max` +
  pip cache-mount в Dockerfile (`--mount=type=cache,target=/root/.cache/pip`) —
  wheels не скачиваются заново при изменении requirements.
- Strict dependency pinning: все прямые зависимости в `requirements.txt` и
  `requirements-dev.txt` зафиксированы через `==` (набор версий = clean-resolve
  CPython 3.12, верифицирован CI); `torch==2.14.1` согласован с CPU-индексом в
  Dockerfile (wheel `2.14.1+cpu` удовлетворяет пин по PEP 440) — воспроизводимые
  сборки без сюрпризов от минорных обновлений транзитивных зависимостей.
- **Docker multi-stage optimization**: base → builder → runtime, CPU-only PyTorch
  (`--index-url .../whl/cpu`) до requirements, `cache-to: type=gha,mode=min` —
  сборка образа в CI ускорена с ~21 до ~4 минут, образ минус ~2 ГБ.
- **Structured logging**: `LOG_FORMAT=json` (Loki/CloudWatch-ready) c корреляцией
  по `X-Request-ID` (middleware эхо/генерация заголовка).
- **Graceful degradation everywhere**: сбои инструментов → warnings в диагнозе,
  а не исключения; отсутствие kubeconfig → degraded-режим или in-cluster config;
  отсутствие LLM-ключа → честный 503 с подсказкой или stub-режим `LLM_OFFLINE`;
  незагрузка embedding-модели → fallback на чистый TF-IDF.

### Fixed

- SyntaxError в `src/agent/orchestrator.py` (метод без отступа внутри `diagnose`)
  и дублирующий импорт до `from __future__` — причина красных CI-ранов #20–#31.
- Ранжирование RAG: `find_relevant` сортировал кандидатов по тексту, а не по
  скору релевантности (топ выдачи зависел от алфавита).
- e2e CI-job: `chmod /usr/local/bin/kind: Operation not permitted` на новых
  раннерах — установка в `$HOME/.local/bin` + гейт шагов по secret'ам.

### Security

- Non-root контейнер (UID 10001), read-only rootfs в Helm-чарте, capabilities
  drop ALL; секреты только через env/.env (не попадают в образ и репозиторий);
  Policy-as-Code enforcement поверх app-level guardrails.

[1.0.0]: https://github.com/Mkft1/ai_agent_DevOps/releases/tag/v1.0.0
