# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- <!-- Новые фичи собираются здесь до следующего релиза (Keep a Changelog). -->

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
