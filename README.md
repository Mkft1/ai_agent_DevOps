<p align="center">
  <a href="./README.md"><b>🇬🇧 English</b></a> &nbsp;|&nbsp; <a href="./README.ru.md"><b>🇷🇺 Русский </b></a>
</p>

| Interactive Dashboard | API Documentation (Swagger) |
|---|---|
| ![Dashboard](docs/assets/dashboard.png) | ![Swagger](docs/assets/swagger.png) |

<h1 align="center">K8s Incident Diagnosis AI Agent</h1>

[![CI](https://github.com/Mkft1/ai_agent_DevOps/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Mkft1/ai_agent_DevOps/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Docker](https://img.shields.io/badge/docker-multistage-2496ED?logo=docker&logoColor=white)](./Dockerfile)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An LLM-powered SRE agent that diagnoses Kubernetes incidents end-to-end. It ingests alerts via a REST API, correlates live signals — pod status, Kubernetes events, Prometheus metrics, container logs — grounds the analysis with TF-IDF retrieval over local runbooks (RAG), and returns a structured **Root Cause Analysis (RCA)** plus a human-readable report ready for Slack/Telegram.

The agent is strictly **read-only by default**: it never mutates the cluster (`ENABLE_WRITE_ACTIONS=false`), so it is safe to expose to production alert pipelines.

---

## Features

| | |
|---|---|
| 🧠 **LLM RCA** | OpenAI-compatible chat API (Qwen/DashScope by default) with strict JSON output validated by Pydantic; automatic one-shot retry on invalid JSON and an honest fallback diagnosis. |
| 📚 **Hybrid RAG over runbooks** | Local Markdown runbooks (`docs/runbooks/*.md`) ranked by **hybrid search**: `0.5 × TF-IDF` (exact terms) + `0.5 × cosine similarity` of sentence-transformers embeddings (`all-MiniLM-L6-v2`, semantic match). No vector DB, no external service; if the model is unavailable the agent gracefully falls back to pure TF-IDF. Top playbooks are injected into the LLM context as `relevant_runbooks` and drive `suggested_fix`. |
| ☸️ **Kubernetes integration** | Official Python client: pods, events, deployments, nodes, container logs (including pre-restart logs for OOMKilled/CrashLoopBackOff). |
| 📈 **Prometheus integration** | instant & range queries, per-pod CPU/memory usage via `container_*` cAdvisor metrics. |
| 🛡️ **Guardrails** | Namespace allow-list (`ALLOWED_NAMESPACES`), log-size cap (`MAX_LOG_LINES`), context-size cap in tokens (`MAX_CONTEXT_TOKENS`, tiktoken), read-only mode (`ENABLE_WRITE_ACTIONS`), **two-level dangerous-action policy** (destructive patterns — `delete`/`drop`/`rm -rf`/`scale 0` and paraphrases like `remove the pod`, `terminate all instances`, `set replicas to 0`, `kubectl apply -f`, `chmod 777` — plus ANY non-read-only `kubectl`/`helm`/`oc` command are flagged `requires_approval: true` and routed to a human via Slack), cluster-level **Policy-as-Code**: least-privilege RBAC + Kyverno ClusterPolicies in `manifests/`, and **PII/secret scrubbing before LLM calls** (`ENABLE_SCRUBBING`: JWTs, API keys, passwords in URLs, Bearer tokens and emails in logs/events are masked as `[REDACTED:<type>]` before the context leaves the perimeter). All configurable via `.env`, nothing hardcoded. |
| 🔐 **Authentication & RBAC (OIDC)** | Optional JWT auth (`OIDC_ENABLED`, default `false` = full backward compatibility): `POST /diagnose` and `POST /webhook/alertmanager` require a Bearer JWT from any OIDC provider (Keycloak/Auth0/Azure AD/Okta) — signature verified against the provider JWKS (RS/PS/ES/EdDSA only, `HS256` rejected), `iss`/`aud`/`exp` enforced, roles from the `roles`/`groups` claim (`401`/`403`, fail-closed); the webhook also accepts a constant-time-compared `X-Webhook-Secret` for Alertmanager. `/health`, `/ready`, `/metrics` stay open for probes/scraping. Dashboard login + admin-only approvals included. See [Authentication & Authorization](#authentication--authorization-oidc). |
| 🩹 **Graceful degradation** | If any data source fails (K8s API down, Prometheus unreachable, empty logs), the agent records a warning in the report and continues diagnosing with whatever data it has — it never crashes the pipeline. |
| 📊 **Observability** | `/metrics` endpoint (`prometheus-fastapi-instrumentator`) + custom counters/histograms: `diagnose_requests_total`, `diagnose_duration_seconds`, `k8s_api_errors_total`, `llm_fallback_total`, `scrubbed_items_total`, `policy_approval_required_total`. `X-Request-ID` correlation (echoed/generated per request), optional structured JSON logging (`LOG_FORMAT=json`) for Loki/CloudWatch, an append-only audit journal (`AUDIT_LOG_PATH`, JSON Lines) of every diagnosis, and **OpenTelemetry distributed tracing** (auto HTTP spans + manual spans per K8s/Prometheus/logs/RAG/LLM call, OTLP export to Jaeger/Tempo). |
| 🚨 **Alertmanager webhook** | `POST /webhook/alertmanager` accepts the standard Alertmanager payload, diagnoses every `firing` alert (namespace/pod from labels, text from `annotations.summary`/`description`), skips `resolved`, and answers `400` on malformed payloads. |
| 🔁 **Chaos self-test** | `scripts/chaos_test.py` creates an ImagePullBackOff pod, asks the agent to diagnose it, asserts the RCA mentions "image"/"pull", and cleans up. Idempotent. |
| ☸️ **Helm chart** | Production-ready `charts/ai-agent` (v0.1.0): Deployment with probes & non-root securityContext, ClusterIP Service, least-privilege RBAC, ConfigMap/Secret from `values.yaml`, `make helm-lint` / `make helm-install`. |
| 🖥️ **Operator dashboard** | `make dashboard` — Streamlit UI over the audit journal: incident table (Timestamp/Namespace/Severity/Root Cause), severity metrics, and approval cards with `suggested_fix` + "Approve & Apply" for every `requires_approval` incident. With OIDC enabled: JWT login screen, current user/roles in the sidebar, and the approve button rendered only for `sre-admin`/`devops-admin`. |
| 🧪 **Testing & CI/CD** | 439 unit/integration tests with mocked clients (94% coverage, CI gate `--cov-fail-under=85`, SentenceTransformer mocked so tests never download models), plus an offline **evaluation framework** (`make eval`) with a golden dataset and LLM-as-a-judge scoring; | GitHub Actions pipeline: lint → test+coverage → Docker build+smoke → kind-based e2e on `main` (auto-skipped when no LLM secret is configured). |
| 🐳 **Containerized** | Multi-stage `python:3.12-slim` image, non-root user, healthcheck, no secrets baked into the image. |

---

## Architecture

```mermaid
flowchart LR
    AM[Alertmanager /\ncurl / chat-bot] -->|POST /diagnose| API[FastAPI\nsrc/main.py]
    API --> ORCH{IncidentOrchestrator}

    subgraph Tools [Data collection tools]
        K8S[K8sTools\npods / events /\ndeployments / nodes]
        PROM[PrometheusTools\nCPU / memory /\ninstant+range queries]
        LOGS[LogsTools\npod & container logs]
    end

    ORCH --> K8S
    ORCH --> PROM
    ORCH --> LOGS

    K8S --> CTX[(Structured context\ndict)]
    PROM --> CTX
    LOGS --> CTX

    CTX --> RAG[RunbookRAG\nTF-IDF over\ndocs/runbooks/*.md]
    RAG -->|"context +\nrelevant_runbooks"| TRUNC[truncate_context\nMAX_CONTEXT_TOKENS\ntiktoken]
    TRUNC --> LLM[LLM Client\nQwen / OpenAI-compatible\nretries + JSON validation]
    LLM -->|Diagnosis payload| DIAG[RCA JSON:\nroot_cause / evidence /\nsuggested_fix / severity]
    DIAG --> REPORT[format_report\nSlack / Telegram text]
    REPORT --> API

    subgraph Guardrails
        G1[ALLOWED_NAMESPACES]
        G2[MAX_LOG_LINES]
        G3[ENABLE_WRITE_ACTIONS=false]
    end
    G1 -.enforced by.-> ORCH
    G2 -.enforced by.-> ORCH
    G3 -.enforced by.-> ORCH
```

Pipeline in one line:

`Alertmanager → POST /diagnose → IncidentOrchestrator → [K8sTools, PrometheusTools, LogsTools] → RunbookRAG → LLM → RCA → Report`

---
## Architecture Decisions

A few deliberate trade-offs worth knowing about:

1. **Hybrid RAG (TF-IDF + local embeddings) instead of a vector database.** Runbooks are small (a handful of Markdown files), so retrieval runs fully in-process with **zero external infrastructure** — no embeddings API, no Chroma/Qdrant sidecar. Exact-term matching (scikit-learn TF-IDF) is combined with semantic matching (sentence-transformers `all-MiniLM-L6-v2` embeddings, cosine similarity) as `0.5 × tfidf + 0.5 × cosine` — the gold-standard hybrid recipe: synonyms and paraphrases are found even when no keyword overlaps. The index is rebuilt in-process at startup; adding knowledge is just dropping a `.md` file into `docs/runbooks/`. If the embedding model cannot be installed/downloaded, `RunbookRAG` degrades gracefully to pure TF-IDF (`ENABLE_HYBRID_RAG=false` disables the vector branch outright).
2. **FastAPI + pydantic-settings.** FastAPI gives async request handling, automatic OpenAPI docs (`/docs` Swagger UI) and request/response validation out of the box; pydantic models make the incident contract (`Incident`, `Diagnosis`) explicit and testable, and all configuration is env-driven via one `Settings` class.
3. **Strict JSON output validated by Pydantic, with one retry and an honest fallback.** Instead of trusting free-form LLM prose, the system prompt demands a fixed JSON schema; invalid responses trigger a single correction retry, then a deterministic fallback diagnosis (`severity="unknown"`) — the pipeline never returns garbage or crashes because of the model.
4. **Read-only by default + graceful degradation.** The agent suggests fixes rather than executing them (`ENABLE_WRITE_ACTIONS=false`), and any failing data source becomes a warning in the report instead of an exception — safe to wire directly into production alert pipelines.
5. **OpenAI-compatible client over raw HTTP (requests + tenacity).** No heavyweight SDK lock-in: works with Qwen/DashScope, OpenAI, or any self-hosted compatible gateway by changing two env vars (`LLM_BASE_URL`, `LLM_MODEL`).
6. **Multi-stage Docker build (base → builder → runtime).** Security and size: the runtime image is a clean `python:3.12-slim` with only installed packages plus `src/` — no compilers, no pip cache, no repo history, no tests/docs; it runs as non-root `appuser` (UID 10001). Copying `requirements.txt` as its own layer keeps the heavy dependency layer cached across code changes, and `cache-to: type=gha,mode=min` in CI exports only final layers so the cache upload stays small.
7. **CPU-only PyTorch in the image.** The agent embeds runbooks with sentence-transformers — it never trains and never uses a GPU. Installing `torch` from `https://download.pytorch.org/whl/cpu` *before* `requirements.txt` pins the CPU wheel (~200 MB) and stops `sentence-transformers` from pulling the CUDA build (+2 GB of drivers/libs): smaller image, faster CI, identical inference.
8. **Production hardening: async I/O + file-locked audit log.** Slack approval notifications and the Prometheus readiness check (`GET /-/ready`) use `httpx.AsyncClient` with short timeouts (5 s / 3 s) instead of blocking `requests` calls: during an alert storm a synchronous HTTP call would occupy every FastAPI threadpool worker and stall the whole API, while async I/O keeps the event loop free even when Slack/Prometheus is slow or down (the remaining sync clients — Kubernetes, Prometheus queries, LLM — are offloaded via `asyncio.to_thread`, so the event loop never blocks). Every append to the audit journal (`audit.jsonl`) runs under a `filelock` (`FileLock`): concurrent requests across threads/processes (uvicorn workers, FastAPI threadpool) cannot interleave JSON lines — each audit record is atomic and stays machine-readable; a lock timeout (10 s) is logged as an audit error and never fails the HTTP request itself.
---

## Quick Start

> 🎬 **Demo GIF placeholder.** A recorded terminal demo will live at
> `docs/assets/demo.gif` (see [docs/assets/demo_placeholder.md](docs/assets/demo_placeholder.md)
> for how to record it with terminalizer/asciinema). Until then the previews above and
> the real `/diagnose` outputs below show the agent in action.

### 1. Configure environment

```bash
cp .env.example .env
# edit .env: set QWEN_API_KEY (or OPENAI_API_KEY), KUBECONFIG_PATH, PROMETHEUS_URL
```

| Variable | Default | Description |
|---|---|---|
| `KUBECONFIG_PATH` | `~/.kube/config` | Path to the kubeconfig used by K8sTools/LogsTools. |
| `PROMETHEUS_URL` | `http://localhost:9090` | Base URL of your Prometheus instance. |
| `LOG_LEVEL` | `INFO` | Application log level (`DEBUG` also logs prompts/LLM responses, without secrets). |
| `QWEN_API_KEY` / `OPENAI_API_KEY` | — | LLM API key (`QWEN_API_KEY` takes precedence). Never hardcoded, only via env. |
| `LLM_BASE_URL` | DashScope compatible-mode | OpenAI-compatible endpoint (`.../v1` suffix optional). |
| `LLM_MODEL` | `qwen-max` | Chat model name — swap freely (gpt-4o-mini, qwen-plus, ...) without code changes. |
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins (JSON list, e.g. `["http://localhost:3000"]`). |
| `ALLOWED_NAMESPACES` | `["default"]` | Namespaces the agent may inspect; anything else → HTTP 403. |
| `MAX_LOG_LINES` | `200` | Hard cap on `tail_lines` for log retrieval. |
| `MAX_CONTEXT_TOKENS` | `4000` | Max size of the serialized LLM context **in tokens** (precise tiktoken `cl100k_base` count); longest fields (logs) are truncated with `... [truncated to fit token limit]`. |
| `ENABLE_WRITE_ACTIONS` | `false` | Read-only mode. No write path exists in the codebase today. |
| `LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one JSON object per line for log aggregators). |
| `LLM_OFFLINE` | `false` | Demo/offline mode: without an LLM key `/diagnose` answers with the deterministic `LLMClientStub` instead of an honest `503`. Keep `false` in production. |
| `AUDIT_LOG_PATH` | `audit.jsonl` | Append-only audit journal (JSON Lines): one record per diagnosis / guardrail rejection / webhook batch. |
| `SLACK_WEBHOOK_URL` | — (disabled) | Incoming webhook for human approval of dangerous fixes; unset = WARNING in logs, `requires_approval` still set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — (disabled) | OTLP/gRPC endpoint for trace export, e.g. `http://localhost:4317` (Jaeger). Unset = tracing collects spans in-process, no export. |
| `OTEL_SERVICE_NAME` | `ai-agent-devops` | `service.name` resource attribute shown in Jaeger/Tempo. |
| `ENABLE_HYBRID_RAG` | `true` | Hybrid retrieval (TF-IDF + embeddings). `false` = pure TF-IDF. |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model for the vector branch of hybrid RAG. |

### 2. Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs (Swagger UI): **http://localhost:8000/docs**

#### Developer shortcuts with `make`

A `Makefile` in the repo root wraps the most common commands so you don't have to remember them:

```bash
make install       # pip install -r requirements.txt -r requirements-dev.txt
make lint          # ruff check src tests scripts
make test          # pytest --cov=src --cov-report=term-missing tests/
make run           # uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
make docker-build  # docker build -t ai-agent-devops:local .
make chaos         # python scripts/chaos_test.py (ImagePullBackOff self-test)
make e2e           # bash scripts/run_integration_test.sh (kind cluster, end-to-end)
make eval          # python eval/evaluate.py (LLM-as-a-judge quality gate)
make dashboard     # streamlit run ui/dashboard.py (operator UI on :8501)
make docs-check    # python scripts/check_docs_metrics.py (README vs pytest)
```

The Makefile is a convenience wrapper only — it does not change or replace CI/CD.

#### Real `/diagnose` response (OOMKilled incident)

The agent retrieves the matching runbook (`docs/runbooks/oom_killed.md`) via RAG and grounds the fix on it:

```json
{
  "diagnosis": {
    "root_cause": "Container 'payments-api' is being OOMKilled: working set (~488 MiB) has reached the 512Mi memory limit; the batch allocation at 09:40:55 triggers a cgroup OOM kill (3 restarts, matching Warning events).",
    "evidence": [
      "container_memory_working_set_bytes = 511705088 bytes (~488 MiB) vs limit 512Mi",
      "Pod status shows 5 restarts; last state reason: OOMKilled",
      "K8s Warning event 'OOMKilling' for pod/payments-api-7d9f4b-x2kq",
      "Logs: 'fatal error: runtime: out of memory' right before container exit",
      "Runbook oom_killed.md: raise memory limit or cap batch size"
    ],
    "suggested_fix": "Increase the container memory limit to 1Gi (kubectl patch) and cap the batch buffer size via PAYMENTS_BATCH_MB=128; then roll the deployment and watch restarts drop to zero.",
    "severity": "high",
    "warnings": []
  },
  "report": "🟠 Incident: diagnosis (severity: high)\nNamespace: default, Pod: payments-api-7d9f4b-x2kq\n..."
}
```

### 3. Run with Docker

```bash
docker build -t k8s-incident-agent .
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v "$HOME/.kube/config:/home/appuser/.kube/config:ro" \
  k8s-incident-agent
```

### 4. Run with docker-compose

```bash
docker compose up --build
# app on http://localhost:8000, kubeconfig and .env mounted read-only
```

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe → `{"status": "ok"}`. |
| `GET` | `/ready` | Checks K8s API + Prometheus reachability → `200` or `503`. |
| `POST` | `/diagnose` | Accepts an incident, returns `{diagnosis, report}`. |
| `POST` | `/webhook/alertmanager` | Alertmanager webhook: diagnoses firing alerts, returns `{processed, skipped, diagnoses}`; `400` on invalid JSON/schema. |
| `GET` | `/metrics` | Prometheus exposition format (HTTP + custom metrics). |

All responses carry an `X-Request-ID` header (echoed when provided by the client/LB, generated otherwise); the same id appears in JSON logs (`LOG_FORMAT=json`) for request/log correlation.

### Example: `POST /diagnose`

Request:

```bash
curl -s -X POST http://localhost:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "namespace": "default",
    "pod_name": "payments-api-7d9f4b-x2kq",
    "description": "Pod payments-api restarts repeatedly, alert: ContainerOOMKilling"
  }'
```

Response (excerpt — produced by the real orchestrator pipeline; deterministic LLM stand-in used here to avoid spending API tokens):

```json
{
  "diagnosis": {
    "root_cause": "Container 'payments-api' is being OOMKilled: working set (~488 MiB) has reached the 512Mi memory limit; the batch allocation at 09:40:55 triggers a cgroup OOM kill (3 restarts, matching Warning events).",
    "evidence": [
      "container_memory_working_set_bytes = 511705088 bytes (~488 MiB) vs limit 512Mi",
      "Pod status shows 5 restarts; last state reason: OOMKilled",
      "K8s Warning event 'OOMKilling' for pod/payments-api-7d9f4b-x2kq",
      "Logs: 'fatal error: runtime: out of memory' right before container exit",
      "Runbook oom_killed.md: raise memory limit or cap batch size"
    ],
    "suggested_fix": "Increase the container memory limit to 1Gi (kubectl patch) and cap the batch buffer size via PAYMENTS_BATCH_MB=128; then roll the deployment and watch restarts drop to zero.",
    "severity": "high",
    "warnings": []
  },
  "report": "🟠 *Incident: diagnosis* (severity: high)\n*Namespace:* `default`, *Pod:* `payments-api-7d9f4b-x2kq`\n..."
}
```

Note how the last piece of evidence comes from the **RAG-retrieved runbook** (`oom_killed.md`) — retrieved automatically from the pod status + log lines, not hardcoded.

### Example: chaos scenario (ImagePullBackOff)

After running `python scripts/chaos_test.py` against a live cluster + local agent:

```bash
curl -s -X POST http://localhost:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "default", "pod_name": "bad-pod", "description": "Pod in ImagePullBackOff"}'
```

```json
{
  "diagnosis": {
    "root_cause": "Image pull failure for pod 'bad-pod': the tag 'definitely-bad-tag-404' does not exist in the registry (ErrImagePull -> backoff).",
    "severity": "medium",
    "suggested_fix": "Fix the image tag in the Deployment spec to an existing tag (e.g. nginx:1.27) and roll the deployment."
  }
}
```

### Alertmanager integration

Point Alertmanager at the agent — every firing alert becomes a full RCA:

```yaml
# alertmanager.yml
route:
  receiver: ai-agent
  group_by: [alertname, namespace]
receivers:
  - name: ai-agent
    webhook_configs:
      - url: http://<agent-host>:8000/webhook/alertmanager
        send_resolved: true   # resolved-алерты агент пропускает (skipped), но аудит получает
```

Behaviour: `labels.namespace` → incident namespace (default `default`), `labels.pod`/`labels.pod_name` → pod, `annotations.summary` (else `annotations.description`, else `alertname`) → description; each diagnosis is written to the audit journal (`AUDIT_LOG_PATH`) and returned in `diagnoses[]` together with the Slack/Telegram-ready `report`. Malformed JSON or schema violations return `400`; a guardrail-rejected namespace skips that alert (`skipped++`) instead of failing the whole batch.

## Security Policies & Approvals

The agent never mutates the cluster — but its *suggestions* can be dangerous.
Before an operator acts on them, a role-based policy gate runs
(`src/agent/policies.py::check_policy(suggested_fix)`) with **two defense levels**:

1. **Dangerous patterns (level 1).** The recommendation is scanned with
   case-insensitive regexes for destructive actions *and their synonyms /
   paraphrases* — the model cannot slip through by rewording the command
   ("remove the pod", "terminate all instances", "set replicas to zero"):

   | Dangerous action | Detected patterns (regex, case-insensitive) | Why it is dangerous |
   |---|---|---|
   | Resource deletion | `delete`, `remove\s+(the\s+)?pod`, `terminate\s+(all\s+)?(instance\|pod)`, `kill\s+(the\s+)?pod` | Irreversible loss of pods/PVCs/namespaces and in-memory state |
   | Replica zeroing | `scale\s+0`, `set\s+replicas\s+to\s+(0\|zero)`, `scale\s+down\s+to\s+(0\|zero)`, `replicas\s*[:=]\s*0` | Full service outage — covers YAML manifests (`replicas: 0`), CLI flags (`--replicas=0`) and word form ("to zero") |
   | Arbitrary manifests | `kubectl\s+apply\s+-f`, `kubectl\s+create\s+-f` | Privileged pods, hostPath mounts, RBAC backdoors — a classic prompt-injection foothold |
   | Filesystem destruction | `rm\s+-rf` | Unrecoverable data loss on the node |
   | Database destruction | `drop`/`drop\s+table`, `truncate\s+table` | Irreversible loss of tables and their data |
   | Permission escalation | `chmod\s+777`, `chown\s+root` | World-writable data; breaks non-root security contexts (`runAsNonRoot`) |
   | Secret / system-file injection | `export\s+.*=\s*.*secret`, `echo\s+.*>\s*/etc/passwd` | Secret leakage via env/child processes; node compromise and root accounts |

2. **CLI mention (level 2, defense in depth).** ANY mention of `kubectl`, `helm`
   or `oc` (OpenShift) that is not a read-only command (`get` / `describe` /
   `logs`) sets `requires_approval: true` automatically — even when no level-1
   pattern matched — and logs a `WARNING`:
   `Policy guardrail: kubectl/helm command detected, requiring approval`.
   Rationale: to actually mutate a cluster the model inevitably needs a CLI
   tool, so tool mentions are guarded even for subcommands the regexes do not
   know yet. Read-only diagnostics (`kubectl get pods`, `kubectl describe pod`,
   `kubectl logs`) remain approval-free — they mutate nothing.
3. In read-only mode (`ENABLE_WRITE_ACTIONS=false`) every hit flags the diagnosis
   **`requires_approval: true`** (visible in `POST /diagnose` and
   `POST /webhook/alertmanager` responses, in the audit journal and as a 🔔 line
   in the Slack/Telegram report) and increments the `policy_approval_required_total`
   Prometheus counter.
4. If `SLACK_WEBHOOK_URL` is configured, the agent posts an approval request to an
   incoming webhook:

   ```
   ⚠️ Action Required: Agent suggests dangerous fix: kubectl delete pod bad-pod -n default. Approve or Reject?
   ```

   Without the webhook the agent logs a `WARNING` (approval still required in the
   diagnosis) — it never crashes. A Slack outage also cannot break diagnosis:
   send failures are logged and the response is returned as usual.
5. Every approval request increments the `approval_requests_total` Prometheus metric.

```bash
# .env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/XXXX
```

Safe recommendations (raise limits, fix image tags, add probes...) and read-only
CLI commands (`kubectl get pods`, `kubectl describe pod`, `kubectl logs`) skip the
gate: `requires_approval: false`, no Slack message.

### Rate limiting (alert-storm / financial DoS protection)

`POST /diagnose` is limited to **5 requests/minute** per client IP and
`POST /webhook/alertmanager` to **30/minute** (slowapi, in-memory storage by
default; set `RATE_LIMIT_REDIS_URL` to share counters across API replicas).
Exceeding the limit returns **429** with `{"detail": "Rate limit exceeded. Please retry later."}`
and a `Retry-After` header. Liveness/readiness probes (`/health`, `/ready`) are
never limited — Kubernetes must always be able to check the pod.
Automated proof that the limits work: `make load-test` — a Locust scenario verifying
429 on `/diagnose` overload (see [Testing → Load Testing](#load-testing-rate-limit-verification)).

## Authentication & Authorization (OIDC)

The API and the operator dashboard support **OIDC authentication with role-based
access control** (`src/auth.py`). It is **disabled by default**
(`OIDC_ENABLED=false`): every endpoint behaves exactly as before, so dev
environments, CI and existing deployments are unaffected. When enabled,
state-changing endpoints require a valid **JWT** issued by your OIDC provider.

### How it works

```
┌──────┐  1. sign-in   ┌───────────────────────┐
│ User │ ────────────▶ │ OIDC Provider          │ Keycloak / Auth0 /
└──┬───┘               │ (authorization server) │ Azure AD / Okta
   │  2. JWT           └───────────┬───────────┘
   │ ◀──────────────────────────── │
   │                               │ 4. OIDC Discovery + JWKS
   │ 3. Authorization: Bearer JWT  │    (public keys, cached 300 s)
   ▼                               ▼
┌──────────────────────────────────────────────┐
│ AI DevOps Agent — FastAPI API + Streamlit UI │
│ verify signature (RS/PS/ES/EdDSA) + iss/aud/ │
│ exp, then check roles/groups claim (RBAC)    │
└──────────────────────────────────────────────┘
```

### What is protected

| Endpoint | Requirement when `OIDC_ENABLED=true` |
|---|---|
| `POST /diagnose` | Bearer JWT **+** at least one role from `ALLOWED_ROLES_DIAGNOSE` (default: `sre-user`, `sre-admin`) |
| `POST /webhook/alertmanager` | Bearer JWT with a role from `ALLOWED_ROLES_WEBHOOK` (default: `alertmanager`, `sre-admin`) **or** header `X-Webhook-Secret` equal to `WEBHOOK_SHARED_SECRET` |
| `GET /health`, `GET /ready`, `GET /metrics` | **Never protected** — kubelet probes and Prometheus scraping must keep working |

- Missing / invalid / expired token → **401** with `WWW-Authenticate: Bearer`;
  valid token but insufficient role → **403**.
- Auth runs *before* the orchestrator bootstrap: an unauthenticated caller can
  never reach the LLM pipeline (401/403 take precedence over 503).
- Roles are read from the `roles` **or** `groups` claim (Keycloak / Azure AD /
  Okta / Auth0 shapes); one match is enough.
- `WEBHOOK_SHARED_SECRET` covers Alertmanager and similar integrations that
  cannot sign JWTs; comparison is constant-time (`secrets.compare_digest`).
  Generate one with `openssl rand -hex 32`.

### Security properties

- **Asymmetric algorithms only** (`RS*`, `PS*`, `ES*`, `EdDSA`) — `HS256` is
  rejected by design, preventing algorithm-confusion attacks with public keys.
- **Full claim validation**: `iss`, `aud`, `exp` are essential; the signature
  is verified against the provider JWKS fetched via OIDC Discovery
  (`{issuer}/.well-known/openid-configuration` → `jwks_uri`) and cached for
  **300 s**, so key rotation is picked up automatically.
- **Fail-closed**: provider unreachable or JWKS broken → `401`, never "open".
- **No secrets in logs**: tokens and webhook secrets are never logged — only
  error types and the `sub` claim.

### Configuration (`.env`)

All variables are documented in [`.env.example`](./.env.example):

```bash
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://keycloak.example.com/realms/devops
OIDC_AUDIENCE=ai-agent-devops-api
ALLOWED_ROLES_DIAGNOSE=["sre-user", "sre-admin"]
ALLOWED_ROLES_WEBHOOK=["alertmanager", "sre-admin"]
WEBHOOK_SHARED_SECRET=<openssl rand -hex 32>
```

`OIDC_ENABLED=true` with an empty issuer or audience is a **fail-fast
configuration error** at startup (pydantic validator), not a runtime surprise.

**Keycloak** — issuer is the realm URL (`https://keycloak.example.com/realms/devops`).
Create a client (e.g. `ai-agent-devops-api`), add client roles `sre-user`,
`sre-admin`, `alertmanager`, and attach a *Client scopes → Role list* mapper so
roles land in the access-token `roles`/`groups` claim. Set `OIDC_AUDIENCE` to
the client ID and make sure tokens are requested with `audience=ai-agent-devops-api`.

**Auth0** — issuer `https://<tenant>.auth0.com/`, audience = the *API identifier*
registered under Applications → APIs. Manage roles via *Auth0 → RBAC → Roles*
and enable "Add permissions in the Access Token"; map permissions into the
`roles` claim with an Action (or list your permission names in `ALLOWED_ROLES_*`).

**Azure AD (Entra ID)** — issuer
`https://login.microsoftonline.com/<tenant-id>/v2.0`. Expose an API
(App registrations → Expose an API), add **App roles** `sre-user` / `sre-admin` /
`alertmanager` (they appear in the token's `roles` claim) and set
`OIDC_AUDIENCE` to the Application (client) ID.

Calling the API:

```bash
TOKEN=<obtained from your provider: browser SSO, client-credentials, oidc-cli>
curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"namespace": "default", "description": "Pod restarts repeatedly"}' \
  http://localhost:8000/diagnose

# Alertmanager-style webhook call with the shared secret instead of a JWT:
curl -sS -H "X-Webhook-Secret: $WEBHOOK_SHARED_SECRET" \
  -H 'Content-Type: application/json' \
  -d @alertmanager-payload.json http://localhost:8000/webhook/alertmanager
```

For Alertmanager itself, put the secret into `http_config.http_headers`:

```yaml
receivers:
  - name: ai-agent
    webhook_configs:
      - url: http://ai-agent.monitoring.svc:8000/webhook/alertmanager
        http_headers:
          X-Webhook-Secret: <WEBHOOK_SHARED_SECRET>   # store in a K8s Secret
```

### Role model (defaults)

| Role | `POST /diagnose` | `POST /webhook/alertmanager` | Dashboard approvals |
|---|---|---|---|
| `sre-user` | ✅ allowed | ❌ 403 | view only |
| `sre-admin` | ✅ allowed | ✅ allowed | ✅ "Approve & Apply" |
| `devops-admin` | ❌ 403 (add to `ALLOWED_ROLES_DIAGNOSE` to allow) | ❌ 403 (add to `ALLOWED_ROLES_WEBHOOK` to allow) | ✅ "Approve & Apply" |
| `alertmanager` (service account) | ❌ 403 | ✅ allowed | — |

To assign roles: create them in your IdP (client roles / App roles / RBAC
roles), assign to users or service accounts, and make sure the IdP emits them
in the access-token `roles` or `groups` claim — the agent reads whichever is
present (`roles` takes precedence).

### Dashboard (Streamlit)

With `OIDC_ENABLED=true` the dashboard shows a **login screen** first: a link
button to the provider's sign-in page plus a JWT paste field. The token is
kept in `st.session_state["user_token"]` only — never logged, never persisted.
The sidebar shows the current user (`sub`) and roles with a **Logout** button.
The "✅ Approve & Apply" button on approval cards is rendered **only for
`sre-admin` / `devops-admin`** — roles are decoded client-side from the JWT
payload (no signature check in the browser; the API remains the actual
enforcement point). With OIDC disabled the dashboard behaves exactly as before.

## Security & Policy-as-Code

Application guardrails (`ALLOWED_NAMESPACES`, read-only mode) are the first line of
defence; the cluster enforces the same rules independently via **least-privilege RBAC**
and **Kyverno Policy-as-Code** — a compromised or misconfigured agent still cannot
touch what it must not touch.

```bash
# 1. Kyverno (if not installed): policy engine that enforces the ClusterPolicies
helm repo add kyverno https://kyverno.github.io/kyverno
helm install kyverno kyverno/kyverno -n kyverno --create-namespace

# 2. Apply RBAC + policies
kubectl apply -f manifests/
```

| Manifest | What it is | Protection |
|---|---|---|
| `manifests/agent-rbac.yaml` | ServiceAccount `ai-agent` + Role (`get/list/watch` only) + RoleBinding in `default` | Least privilege: the agent has **no write verbs at cluster level**, even if `ENABLE_WRITE_ACTIONS` is ever flipped or the app is compromised |
| `manifests/policies/kyverno-block-kube-system.yaml` | ClusterPolicy (Enforce): deny CREATE/UPDATE/DELETE in `kube-system` for every ServiceAccount except system ones (`kube-system`/`kube-public`/`kube-node-lease`) | System components (CoreDNS, kube-proxy, controllers) are untouchable by workloads and the agent |
| `manifests/policies/kyverno-validate-actions.yaml` | ClusterPolicy (Enforce): any write by SA `ai-agent` is denied unless the target namespace has label `agent-managed=true` | Write actions (if ever enabled) work only in explicitly opted-in namespaces |

Verify the enforcement yourself:

```bash
# RBAC: no write verbs for the agent
kubectl auth can-i delete pods -n default --as=system:serviceaccount:default:ai-agent   # -> no
kubectl auth can-i get pods  -n default --as=system:serviceaccount:default:ai-agent   # -> yes

# Kyverno: label-gated writes
kubectl label ns sandbox agent-managed=true   # opted-in namespace for future write paths
```

Policies run in `Enforce` mode (AdmissionReview deny). To observe first without
blocking, set `spec.validationFailureAction: Audit` and watch Kyverno's policy reports.

## Evaluation & Quality Assurance

Prompts, runbooks and models evolve — without a measurement harness every change is
blind. `make eval` runs a **golden dataset** of incidents through the real agent
pipeline (RAG + LLM) and scores the answers with **LLM-as-a-judge**:

```bash
make eval                # or: python eval/evaluate.py --min-score 4.0
```

* **Dataset** — `eval/golden_dataset.json`: 3 cases (OOMKilled, ImagePullBackOff,
  CrashLoopBackOff), each with `incident` (namespace/pod_name/description),
  `expected_root_cause` and `expected_runbook`. Add cases freely — the schema is
  validated at load.
* **No cluster needed** — K8s/Prometheus/logs tools are mocked inside the script with
  realistic per-scenario symptoms (events, log tails, metrics), so the evaluation
  measures what matters: **RAG retrieval + LLM reasoning**, not cluster availability.
* **Judge** — a second LLM call compares expected vs actual root cause semantically:
  «Сравни expected_root_cause: … и actual_root_cause: … Оцени по шкале от 1 до 5,
  где 5 — полное совпадение по смыслу. Верни только цифру.»
* **Metrics** — average judge score (1–5) and **runbook hit-rate** (share of cases where
  RAG retrieved the expected runbook into `relevant_runbooks`).
* **Gate** — exit code 0 only if avg score ≥ `--min-score` (default 4.0) **and**
  hit-rate is 100%; 1 = quality below threshold (use as a regression gate before
  prompt/RAG/model changes), 2 = environment error (missing LLM key, bad dataset).

```
==============================================================================
                 EVALUATION REPORT — LLM-as-a-judge (golden dataset)
==============================================================================
 CASE (runbook)             JUDGE 1-5  RUNBOOK  ACTUAL ROOT CAUSE
------------------------------------------------------------------------------
 oom_killed.md                    5/5  ✅       Container OOMKilled: memory limit…
 image_pull_backoff.md            5/5  ✅       Image pull failure: manifest un…
 crash_loop.md                    4/5  ✅       CrashLoopBackOff: missing DATABASE…
------------------------------------------------------------------------------
 Average judge score : 4.67 / 5  (порог: 4.0)
 Runbook hit-rate    : 100%  (3/3)
 Verdict             : ✅ PASS
==============================================================================
```

Requires an LLM key (`QWEN_API_KEY`/`OPENAI_API_KEY`) — the same model family can be
used for diagnosis and judging, or point `LLM_BASE_URL`/`LLM_MODEL` at a stronger judge.

## Tracing (OpenTelemetry)

The agent emits **distributed traces** so you can see exactly where milliseconds go:
K8s API calls, Prometheus queries, log tails, RAG retrieval or the LLM "thinking" time.

* **Automatic HTTP spans** — every request gets a server span (`GET /health`, `POST /diagnose`, ...) via `FastAPIInstrumentor.instrument_app(app)`.
* **Manual spans** in `IncidentOrchestrator` form a per-incident tree:

```
incident.diagnose            (attrs: incident.namespace, incident.severity, incident.warnings)
├── k8s.get_pods             (k8s.namespace, k8s.pods_found)
├── k8s.get_events           (k8s.namespace, k8s.events_found)
├── prometheus.pod_cpu       (k8s.pod)
├── prometheus.pod_memory    (k8s.pod)
├── logs.pod_tail            (k8s.pod, logs.tail_lines, logs.bytes)
├── rag.search               (rag.top_k, rag.matches)
└── llm.generate             (llm.model, llm.ok, llm.response_chars)
```

* Tool failures are recorded on the span (`record_exception` + `ERROR` status) — a degraded step is visible in the trace, not only in logs.
* Export is **opt-in**: set `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/gRPC). Without it spans are collected in-process and nothing is sent anywhere — zero external side effects in CI/offline runs.

### Try it locally with Jaeger

```bash
make jaeger          # or: docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uvicorn src.main:app --port 8000
curl -s localhost:8000/health >/dev/null
curl -s -X POST localhost:8000/diagnose -H 'Content-Type: application/json' \
     -d '{"namespace":"default","pod_name":"web-1","description":"pod restarts"}'
```

Open the Jaeger UI at **http://localhost:16686**, pick service `ai-agent-devops`
(override with `OTEL_SERVICE_NAME`) and inspect the `incident.diagnose` trace tree.
The same endpoint works with Tempo/OpenTelemetry Collector — anything speaking OTLP/gRPC.

---

## Security & Guardrails

All limits are configuration-driven (see table above), enforced **before** any cluster call:

1. **Namespace allow-list** — requests for namespaces outside `ALLOWED_NAMESPACES` fail fast with `"Namespace not allowed"` (HTTP 403); no API calls are made.
2. **Read-only by design** — with `ENABLE_WRITE_ACTIONS=false` the agent only reads data and *suggests* fixes. There is no delete/exec/apply code path in the repository; the flag guards future action-proposing features.
3. **Bounded context** — log tails capped by `MAX_LOG_LINES`; the whole LLM context is iteratively trimmed to `MAX_CONTEXT_TOKENS` by precise tiktoken counting (longest values keep their tail — the freshest log lines — and are marked `... [truncated to fit token limit]`), guaranteeing no context-window overflow on any provider.
4. **No secrets anywhere** — API keys come exclusively from `.env` (never committed, excluded from the Docker image via `.dockerignore`); prompt/response logging at DEBUG never includes auth headers.
5. **Graceful degradation** — a failing tool produces a warning in the report ("K8s API unavailable, using only Prometheus metrics"), not a crash; even a missing kubeconfig at startup only degrades K8s/log tools to empty results, and an unconfigured LLM key yields an actionable `503` (or stub mode via `LLM_OFFLINE=true`), never a bare `500`.
6. **Non-root container** — the runtime image runs as `appuser` (UID 10001) with a read-only kubeconfig mount.
7. **PII/secret scrubbing before LLM calls** — pod logs and Kubernetes events are masked (`[REDACTED:<type>]`) before the context is serialized for the external LLM provider; see [Data Privacy & PII Scrubbing](#data-privacy--pii-scrubbing) below.

### Data Privacy & PII Scrubbing

Container logs and Kubernetes events routinely carry secrets, and the diagnosis context is sent to a **third-party LLM provider** — so before serialization every payload passes through the scrubber (`src/utils/scrubber.py`, called by the orchestrator right before the LLM step):

| Pattern | Example | Masked as |
|---|---|---|
| JWT tokens | `eyJhbGciOi...` | `[REDACTED:jwt]` |
| API keys | `sk-...` (OpenAI), `ghp_...` (GitHub), `AKIA...` (AWS), `key-...` | `[REDACTED:api_key]` |
| Passwords in URLs | `postgres://app:hunter2@db:5432/orders` | `postgres://app:[REDACTED:password]@db:5432/orders` |
| Bearer tokens | `Authorization: Bearer abc...` | `[REDACTED:bearer]` |
| Emails | `admin@example.com` | `[REDACTED:email]` |

- **What is scrubbed**: `pod_logs_tail` and every Kubernetes event in the LLM context. **What is not**: the model's own answer (`root_cause`, `evidence`, `suggested_fix`) is returned as-is — it is generated text, not raw cluster data.
- **IP addresses are NOT masked by default** — they are essential for network diagnosis; set `REDACT_IPS=true` to mask IPv4 addresses as well (`[REDACTED:ip]`).
- **Disabling**: `ENABLE_SCRUBBING=false` turns masking into a pure passthrough — only do this for trusted local models (e.g. Ollama inside the cluster). Default is `true`; this is a hard requirement for enterprise compliance review.
- **Observability**: every masked item increments the Prometheus counter `scrubbed_items_total{type="jwt|api_key|password|bearer|email|ip"}` — a sudden spike shows what kind of secrets are circulating in your workloads.

---

## Testing

```
tests/
├── test_k8s_tools.py         # K8s client mocking (pods/events/deployments/nodes, ApiException paths)
├── test_prometheus_tools.py  # HTTP mocking: success, timeouts, 5xx, parsing
├── test_logs_tools.py        # read_namespaced_pod_log mocking
├── test_orchestrator.py      # full pipeline w/ mocks, graceful degradation, guardrails
├── test_llm_integration.py   # LLMClient: retries (429/5xx), JSON validation, fallback
├── test_rag.py               # RunbookRAG on fixture md-files + orchestrator integration
├── test_text_utils.py        # truncate_context recursion & guarantees
├── test_chaos_test.py        # chaos script logic (mocked k8s + requests)
├── test_config.py            # Settings validators: fail-fast on bad LOG_LEVEL/LOG_FORMAT/limits
├── test_auth.py              # OIDC: real RS256 JWTs vs fake JWKS provider, 401/403, webhook secret, UI login
├── test_logger.py            # JSON structured logging + request_id correlation
├── test_webhook_alertmanager.py  # Alertmanager payload parsing, 400s, resolved-skip, audit journal
├── test_tracing.py           # OTel provider, FastAPI auto-spans, manual tool/LLM spans
├── test_policies.py          # dangerous-fix policy, Slack approval flow, no-webhook fallback
├── test_scrubber.py          # PII/secret masking: patterns, false positives, config, orchestrator wiring
├── test_evaluate.py          # eval harness: dataset schema, judge parsing, exit codes
├── test_dashboard.py         # audit journal parsing, table build, approval filter
└── test_api.py               # FastAPI TestClient: /health /ready /diagnose /metrics, CORS, errors, X-Request-ID
```

- **Unit tests**: every external boundary (Kubernetes API, Prometheus HTTP, LLM HTTP) is mocked — the suite runs offline in ~2 seconds.
- **Coverage**: `pytest --cov=src --cov-report=xml --cov-fail-under=85` — currently **~94%** (uploaded as a CI artifact every run; the CI fails if coverage drops below 85%).
- **Docs drift guard**: `make docs-check` (и шаг CI после pytest) сверяет числа
  тестов/покрытия в этом README с реальным прогоном — расхождение роняет CI.
- **Integration (end-to-end)**: `scripts/run_integration_test.sh` spins up a real [kind](https://kind.sigs.k8s.io/) cluster, creates a pod with a bad image tag, runs the agent under uvicorn, sends a real `POST /diagnose`, and asserts the RCA mentions *"image"*/*"pull"* (via `jq`). Idempotent: leftover clusters/pods are removed before each run; cleanup happens even on failure.
- **CI jobs** (GitHub Actions, target 5-6 min): `lint` and `helm` run **in parallel**; `test` (pytest **-n auto** via pytest-xdist, coverage gate ≥85%, docs drift-guard) needs only `lint`; on push to `main`/dispatch, `build` (Docker + smoke, needs [test, helm], BuildKit gha-cache `mode=max` + pip cache-mounts; NOT run on PRs) and `integration-test` (kind e2e, needs only test, runs **parallel to build**, gracefully skipped without `QWEN_API_KEY`/`OPENAI_API_KEY` secret) finish the pipeline. PRs run lint+helm+test+dependency-audit for fast feedback; `[skip ci]` in a commit title skips the run. Security layer: `dependency-audit` (pip-audit, blocking on runtime `requirements.txt`, dev-deps report-only) runs parallel to tests, and `security-scan` (Hadolint + Trivy image scan, `CRITICAL,HIGH` fail-fast, cached Trivy DB) runs after build parallel to e2e; both are skippable via repo variable `SKIP_SECURITY_SCAN=true`.

### Load Testing (rate-limit verification)

[`scripts/load_test.py`](scripts/load_test.py) (Locust) is the automated proof that the
slowapi limits actually work: under overload `POST /diagnose` returns **429**, the app
does not crash (no 5xx), and `GET /health` stays unrestricted.

```bash
make install     # once: installs locust (requirements-dev.txt)
make run         # terminal 1 — the agent on http://localhost:8000
make load-test   # terminal 2 — locust headless: 10 users, 10 seconds
```

**Expected result:** ~50% of `/diagnose` requests get **429** — of the burst of 10, the
first 5 pass with 200 and the next 5 are strictly 429 (the `POST /diagnose [429 expected]`
row in the stats table); `/health` — **100% 200** (thousands of requests while the limit
is being hit). Scenario verdicts are the `SCENARIO test_diagnose_rate_limit` and
`SCENARIO test_health_unlimited` rows with **0 failures**; any unexpected behaviour (500,
429 within the first five, non-429 beyond the limit, non-200 on `/health`) is recorded as
a real locust failure and a non-zero exit code.

Notes:

- The slowapi counter is keyed by client IP, so all Locust users on localhost share one
  60-second window — the `/diagnose` burst runs exactly once per run, while the other
  9 users keep hammering `/health` in parallel (proving health checks are not blocked
  under overload).
- `/diagnose` needs an LLM: `QWEN_API_KEY`/`OPENAI_API_KEY`, or `LLM_OFFLINE=true` in
  `.env` (stub mode — fast responses, recommended for this test). Without either, the
  endpoint honestly returns 503 before the limiter fires and the scenario fails with a
  hint. With a real (slow) LLM key, raise the run duration:
  `locust -f scripts/load_test.py --headless -u 10 -t 120s --host=http://localhost:8000`.
- Rerun at most once per minute (the limit window is 60 s), or restart the agent.

---

## Kubernetes Deployment

The repo ships a production-ready Helm chart (`charts/ai-agent`, version 0.1.0):

```bash
# 1. Validate the chart
make helm-lint          # helm lint charts/ai-agent

# 2. Install / upgrade (values file is the single source of truth)
make helm-install       # helm upgrade --install ai-agent charts/ai-agent -f charts/ai-agent/values.yaml
# or with your LLM key:
helm upgrade --install ai-agent charts/ai-agent \
  --namespace ai-agent --create-namespace \
  --set secrets.llmApiKey="$QWEN_API_KEY"

# 3. Reach the API
kubectl -n ai-agent port-forward svc/ai-agent 8000:8000
curl -s localhost:8000/health
```

What the chart gives you out of the box:

* **Deployment** with liveness (`/health`) and readiness (`/ready`) probes,
  checksum-annotated ConfigMap/Secret (config changes roll the pods),
  `runAsNonRoot: true` (UID 10001), `readOnlyRootFilesystem`, dropped capabilities,
  resource requests/limits and an emptyDir for the audit journal;
* **Service** (ClusterIP) exposing the API on port 8000;
* **ServiceAccount + Role/RoleBinding** — read-only verbs (`get/list/watch`) only,
  matching the Kyverno policies in `manifests/` (SA name `ai-agent`);
* **ConfigMap** with all non-secret settings from `src/config.py`
  (`PROMETHEUS_URL`, `LOG_LEVEL`, guardrails, RAG, OTel, audit path);
* **Secret** for API keys (`secrets.llmApiKey` → `QWEN_API_KEY`, etc.).
  In production prefer External Secrets/Vault: set `secrets.existingSecret`
  and the chart will not create the Secret itself.

All knobs live in `charts/ai-agent/values.yaml` (replicas, image tag, resources,
env, probes, securityContexts, nodeSelector/tolerations/affinity). CI runs
`helm lint` + `helm template` on every push, so the chart cannot silently rot.

## Interactive Dashboard

A lightweight Streamlit UI for on-call operators (runs **separately** from the FastAPI
service, read-only over the audit journal):

```bash
make dashboard           # or: streamlit run ui/dashboard.py
# open http://localhost:8501
```

What you see there:

* **Headline metrics** — three big tiles: Total Incidents, Critical Incidents
  (`severity=critical`) and Pending Approvals (`requires_approval=true`);
* **Incidents table** — every diagnosis from the audit journal (`AUDIT_LOG_PATH`,
  default `audit.jsonl`, parsed line-by-line with `json`): Time (human-readable,
  `19 Oct, 10:00`), Namespace, Pod, Severity with colour coding
  (🔴 critical / 🟠 high / 🟡 medium / 🟢 low) and Root Cause — newest first;
* **Approval cards** — for each incident with `requires_approval: true`: root cause,
  the dangerous `suggested_fix` in a code block, data-collection warnings and a
  **"✅ Approve & Apply"** button (demo confirmation with 🎈: the dashboard never
  touches the cluster — the human applies the change via kubectl/GitOps after a
  dry-run);
* **🔍 View Raw Audit Log** expander with the parsed JSON events and a sidebar
  Refresh button.

If the audit journal does not exist yet, the dashboard shows a warning with hints
(run the agent and send a few `/diagnose` or Alertmanager requests) instead of crashing.

## Documentation

| Document | For whom | Contents |
|---|---|---|
| [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) | **On-call engineers** | Incident response flow for `severity: critical` and safe-fix verification, handling `warnings` (degraded data sources), adding runbooks, updating the golden dataset, troubleshooting (LLM 429, embedding model load failures) |
| [`docs/runbooks/`](./docs/runbooks/) | SRE knowledge base | 8 peer-reviewed runbooks retrieved by the agent's hybrid RAG |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | Developers | Dev environment, tests/lint rules, PR process |
| [`CHANGELOG.md`](./CHANGELOG.md) | Everyone | Keep a Changelog: что вошло в каждый релиз |
| [`.github/pull_request_template.md`](./.github/pull_request_template.md) | Contributors | Чек-лист PR: тесты, линтер, docs, тесты на новую логику |
| [`README.ru.md`](./README.ru.md) | Русскоязычная версия | Полная синхронная версия этого README |

## Contributing

### Dev environment

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + ruff, pytest, pytest-cov, httpx
```

### Before opening a PR

```bash
ruff check src tests scripts          # linter (config in pyproject.toml)
pytest --cov=src tests/               # all tests must pass
```

### Project rules

- Follow Git flow in this repo: sync with `main` first (`git fetch origin main && git rebase origin/main`), one feature branch per task, clean diffs.
- Keep the agent **read-only** unless a task explicitly adds guarded write actions.
- New settings go through `src/config.py` (pydantic-settings) + `.env.example` — never hardcode.
- Every new tool method must swallow `ApiException`/HTTP errors and return an empty result + logged warning (graceful degradation principle).

### Adding a new runbook (RAG)

1. Create `docs/runbooks/<your_incident>.md` — use the existing files as templates: `Symptoms`, `Root causes`, `How to fix` (with copy-pasteable `kubectl` commands), `Prevention`.
2. Write naturally — the TF-IDF index is rebuilt at startup, no registration step needed.
3. Sanity-check retrieval:
   ```bash
   python -c "from src.agent.rag import RunbookRAG; print([t.splitlines()[0] for t in RunbookRAG().find_relevant('your sample error text')])"
   ```
4. Optionally add a keyword assertion to `tests/test_rag.py`.

---

## Project structure

```
ai_agent_DevOps/
├── .github/workflows/ci.yml     # lint → test → build → integration-test (kind)
├── Dockerfile                   # multi-stage (base/builder/runtime), CPU-only torch, non-root, healthcheck
├── docker-compose.yml           # local stack: agent + kubeconfig/.env mounts
├── Makefile                     # dev shortcuts: install/lint/test/run/docker-build/chaos/e2e
├── docs/runbooks/               # RAG knowledge base: 8 runbooks (OOMKilled, ImagePullBackOff, CrashLoop, DNS, NodeNotReady, PVCPending, CreateContainerConfigError, Evicted)
├── eval/
│   ├── golden_dataset.json      # golden incidents: expected root cause + runbook
│   └── evaluate.py              # LLM-as-a-judge harness (make eval), no cluster needed
├── manifests/
│   ├── agent-rbac.yaml          # least-privilege SA/Role/RoleBinding (read-only)
│   └── policies/                # Kyverno ClusterPolicies: kube-system lockdown,
│                                # agent-managed=true label gate for writes
├── ui/
│   └── dashboard.py             # Streamlit operator UI over audit.jsonl (make dashboard)
├── scripts/
│   ├── chaos_test.py            # ImagePullBackOff chaos experiment vs /diagnose
│   ├── run_integration_test.sh  # kind-based end-to-end test
│   └── kind/cluster.yaml        # kind cluster config
├── src/
│   ├── main.py                  # FastAPI app: /health /ready /diagnose /metrics /webhook/alertmanager
│   ├── api/models.py            # pydantic contracts: Alertmanager payload -> Incident
│   ├── auth.py                  # OIDC auth & RBAC: JWT verification (JWKS), FastAPI dependencies
│   ├── config.py                # pydantic-settings (.env driven)
│   ├── prompts/sre_system.txt   # system prompt for the LLM (loaded at init, fallback in code)
│   ├── agent/
│   │   ├── orchestrator.py      # IncidentOrchestrator: pipeline, guardrails, degradation, reporting
│   │   ├── policies.py          # 2-level dangerous-fix policy (synonym regexes + kubectl/helm/oc) -> approval
│   │   ├── llm_client.py        # OpenAI-compatible client: tenacity retries, 30s timeout
│   │   ├── rag.py               # RunbookRAG: hybrid retrieval (TF-IDF + embeddings) over docs/runbooks
│   │   └── tools/               # k8s_tools.py, prometheus_tools.py, logs_tools.py
│   └── utils/
│       ├── audit.py             # append-only audit journal (JSON Lines)
│       ├── logger.py            # stdlib logging setup
│       ├── metrics.py           # custom Prometheus metrics
│       ├── scrubber.py          # PII/secret masking before LLM calls ([REDACTED:<type>])
│       └── text_utils.py        # truncate_context (bounded, JSON-safe)
├── charts/ai-agent/             # production Helm chart (v0.1.0): deployment,
│                                # service, SA/RBAC, configmap, secret, helpers
└── tests/                       # 439 tests, fully mocked externals
```

---

## License

[MIT](./LICENSE)
