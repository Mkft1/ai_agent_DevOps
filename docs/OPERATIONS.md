# Operations Runbook — K8s Incident Diagnosis AI Agent

Audience: on-call engineers. This document covers how to *operate alongside* the
agent: what to do with its verdicts, how to react to degraded data, how to extend
its knowledge base, and how to troubleshoot the agent itself.

Ground rules to keep in mind:

- The agent is **read-only** (`ENABLE_WRITE_ACTIONS=false`). It never mutates the
  cluster; every change is a *suggestion* that a human applies.
- Every diagnosis is audited: one JSON line per event in `AUDIT_LOG_PATH`
  (default `audit.jsonl`). Keep the `X-Request-ID` from the HTTP response — it
  correlates the diagnosis with logs (`LOG_FORMAT=json`) and traces (Jaeger).
- The agent degrades gracefully: a missing data source becomes a `warnings` entry,
  not a crash. Warnings lower confidence — see [Handling Warnings](#handling-warnings).

---

## Incident Response Flow

When `POST /diagnose` (or the Alertmanager webhook) returns **`severity: critical`**:

1. **Read the verdict first, act second.**
   - `root_cause` — the hypothesis; `evidence` — the facts behind it;
     `suggested_fix` — the proposal; `warnings` — data gaps (see below).
   - Cross-check at least one piece of `evidence` against the live cluster before
     trusting the RCA:
     ```bash
     kubectl -n <ns> get pod <pod> -o wide
     kubectl -n <ns> describe pod <pod> | sed -n '/Events:/,$p'
     kubectl -n <ns> logs <pod> --tail=50 --previous   # for restarted/OOMKilled containers
     ```

2. **Check `requires_approval`.**
   - `requires_approval: true` means the suggested fix matched the dangerous-action
     policy (`delete`, `drop`, `rm -rf`, `scale 0`). If `SLACK_WEBHOOK_URL` is
     configured, an approval request was already posted to Slack
     (`⚠️ Action Required: ... Approve or Reject?`); the report also carries a 🔔 line.
   - **Never apply a `requires_approval: true` fix under time pressure without a
     second human eyes-on.** That flag exists exactly for the 3 a.m. case.

3. **Verify the fix is safe before applying it:**
   - Dry-run against the API server first:
     ```bash
     kubectl apply --dry-run=server -f patched-manifest.yaml   # or: kubectl diff -f ...
     ```
   - Snapshot the current state so the change is reversible:
     ```bash
     kubectl -n <ns> get deploy <name> -o yaml > /tmp/<name>.backup-$(date +%s).yaml
     ```
   - Check the blast radius: namespace-scoped vs cluster-scoped, replica count,
     PodDisruptionBudgets, HPA ownership of the fields you are about to edit.
   - If the diagnosis cites a runbook (`relevant_runbooks`), follow its
     `How to fix` steps and its `Prevention` checklist — they are peer-reviewed,
     the LLM prose is not.
   - When in doubt, reproduce on a throwaway cluster first: `make e2e` spins up kind.

4. **Apply incrementally and watch the signals.**
   - One change at a time; monitor restarts, `container_memory_working_set_bytes`,
     HTTP error rate, and the agent's own `/metrics`.
   - Re-run `/diagnose` after the change: a clean verdict with empty `warnings`
     and falling restarts is your closure signal.

5. **Close the loop.**
   - Save the audit line (`grep <request_id> audit.jsonl`) and the trace ID into the
     incident ticket / postmortem.
   - If the real root cause differed from the agent's, add the incident to the
     golden dataset — see [Updating the Golden Dataset](#updating-the-golden-dataset).

If `severity` is `unknown` or `evidence` is empty, treat the answer as
"insufficient data", not as "all good": collect facts manually and check `warnings`.

---

## Handling Warnings

`warnings` is the agent's honesty channel: every data source that failed during
collection is listed there. The diagnosis is still produced from whatever data
remained — **your confidence must drop accordingly**.

| Warning (example) | Meaning | On-call action |
|---|---|---|
| `K8s API unavailable, using only Prometheus metrics` | kubeconfig invalid/expired, API server down, or RBAC denial | `kubectl get ns` to confirm; check `KUBECONFIG_PATH` and token expiry; the verdict rests on metrics+logs only — verify pod/event facts manually |
| `Не удалось получить список подов в ns/<ns>` | same as above (empty pod list) | as above; check API-server load / etcd health |
| `CPU-метрики по поду X недоступны` / `Memory-метрики ...` | Prometheus unreachable or cAdvisor metrics missing | `curl -s $PROMETHEUS_URL/-/ready`; check `PROMETHEUS_URL`, network policies; OOM-style RCA loses its key evidence — check limits manually |
| `Логи пода X не получены` | pod never started (ImagePullBackOff/CreateContainerConfigError) or log backend down | expected for never-started pods; otherwise check kubelet/log pipeline |
| `LLM не ответил — RCA не сформирован` | LLM API down / auth failed / rate-limited after retries | see [Troubleshooting](#troubleshooting); fall back to runbooks + manual RCA |
| HTTP 403 `Namespace 'x' not allowed` | guardrail: namespace outside `ALLOWED_NAMESPACES` | intentional; change only via reviewed config update, never ad-hoc |

Rules of thumb:

- **One warning** → proceed, but cross-check the missing source manually.
- **Two or more** → treat the RCA as a hint, not a verdict.
- **All sources degraded** → do not act on the RCA; diagnose manually and fix the
  agent's connectivity (dependency health is visible at `GET /ready`: 200 = both
  K8s and Prometheus reachable, 503 = degraded).

---

## Adding a New Runbook

Runbooks are the agent's peer-reviewed knowledge (`docs/runbooks/*.md`). They are
retrieved by hybrid search (TF-IDF + embeddings) and injected into the LLM context
as `relevant_runbooks`, steering `suggested_fix`.

1. **Create the file** `docs/runbooks/<incident_slug>.md` (e.g. `pod_evicted.md`)
   using the house template — four sections, English, real symptom terms:
   ```markdown
   # Runbook: <Title>

   ## Symptoms
   - Pod status: `...`, events: `...`, metric behaviour: `...`

   ## Root cause
   <Why it happens, typical triggers>

   ## How to fix
   1. ...with copy-pasteable `kubectl` commands...

   ## Prevention
   - ...alerts, limits, review checks...
   ```
   Use the exact terms operators and Kubernetes emit (`OOMKilled`,
   `ImagePullBackOff`, `manifest unknown`, ...): retrieval matches on them.
2. **Restart the application** — the TF-IDF/embedding index is built at startup:
   ```bash
   docker compose restart agent     # or rollout the deployment; dev: uvicorn --reload
   ```
3. **Sanity-check retrieval** (should return your file first):
   ```bash
   python -c "from src.agent.rag import RunbookRAG; \
   print([t.splitlines()[0] for t in RunbookRAG().find_relevant('<typical error text>')])"
   ```
4. **Verify with `make eval`** — add a golden case for the new incident (see below)
   and confirm the runbook hit-rate stays 100%.
5. Optionally assert retrieval in `tests/test_rag.py`, then open a PR
   (`ruff check src tests scripts eval` + `pytest` must be green).

---

## Updating the Golden Dataset

The golden dataset (`eval/golden_dataset.json`) is the regression gate for RAG and
prompt/model changes. Feed it from **real incidents with a confirmed root cause** —
every postmortem is a free test case.

1. Pick an incident whose RCA was confirmed by a human (ticket/postmortem).
2. Append a case to `eval/golden_dataset.json`:
   ```json
   {
     "incident": {
       "namespace": "default",
       "pod_name": "web-1",
       "description": "Pod web-1 crash loops after deploy, alert: CrashLoopBackOff"
     },
     "expected_root_cause": "Application crashes at startup: missing configuration causes repeated container exits",
     "expected_runbook": "crash_loop.md"
   }
   ```
   - `description`: what the alert/on-call saw (keep it natural — this is the query).
   - `expected_root_cause`: the confirmed RCA in one sentence; the judge scores
     *semantics*, so wording may differ from the agent's.
   - `expected_runbook`: the runbook that should be retrieved (create it first if
     the incident type is new).
   - **Anonymize**: no customer data, secrets, or internal hostnames.
3. **Add the matching scenario** to `SCENARIOS` in `eval/evaluate.py`
   (mocked pods/events/logs/metrics keyed by the runbook name) — the harness has no
   real cluster, so symptoms must come from here. Mirror the real incident's
   events/log lines and use the runbook's terminology.
4. Run `make eval`: expect average judge score ≥ 4.0 and runbook hit-rate 100%
   (exit code 0). A failing case means retrieval or the prompt regressed — fix
   before merging.
5. Commit dataset + scenario + runbook in one PR.

---

## Troubleshooting the Agent Itself

### LLM API returns 429 (Rate Limit)

- The client already retries 429/5xx with exponential backoff (tenacity, 3 attempts)
  — occasional 429s heal themselves; look for `LLM API HTTP 429, retrying...` in logs.
- Persistent 429s = quota/concurrency exhaustion:
  1. Check the provider dashboard (Qwen/DashScope or your gateway) for QPM/TPM limits.
  2. Reduce load: group/deduplicate alerts in Alertmanager (`group_by`, `group_wait`)
     so one incident does not fire N diagnoses; the webhook skips `resolved` alerts.
  3. Shrink token cost: lower `MAX_CONTEXT_CHARS` (default 16000) and `MAX_LOG_LINES`.
  4. Switch model/provider via `LLM_MODEL` / `LLM_BASE_URL` if the limit is per-model.
- While the LLM is unavailable the agent still answers with a fallback diagnosis
  (`warnings: ["LLM не ответил — RCA не сформирован"]`, severity from collected data)
  and increments `llm_fallback_total` — **alert on its rate** to notice silent
  degradation:
  ```promql
  rate(llm_fallback_total[10m]) > 0
  ```

### Embedding model (sentence-transformers) does not load

- Symptom: startup log `Embedding model all-MiniLM-L6-v2 unavailable (...) — RAG
  falls back to pure TF-IDF` or `sentence-transformers is not installed ...`.
  The agent keeps working on TF-IDF only — **synonym/paraphrase retrieval degrades**,
  exact-term retrieval does not.
- Causes and fixes:
  1. **No network / HF blocked** (first run downloads the model): pre-download on a
     build/host with network and point `HF_HOME` at the cache, or bake the cache into
     the image; for air-gapped clusters set `HF_HUB_OFFLINE=1` with a pre-seeded cache.
  2. **No disk space** for the model cache: free space or move `HF_HOME` to a volume.
  3. **Package missing** in the runtime image: rebuild from `requirements.txt`
     (sentence-transformers is included).
  4. Intentional TF-IDF-only mode: `ENABLE_HYBRID_RAG=false`.
- Verify retrieval quality after any of the above with `make eval`
  (runbook hit-rate is the sensitive metric).

### Quick reference

| Symptom | First check |
|---|---|
| `POST /diagnose` → 503 `LLM is not configured` | `QWEN_API_KEY`/`OPENAI_API_KEY` in env; demo mode: `LLM_OFFLINE=true` (stub answers) |
| `/ready` → 503 | `checks` body: which dependency (kubernetes/prometheus) is unreachable |
| No traces in Jaeger | `OTEL_EXPORTER_OTLP_ENDPOINT` set? (unset = export disabled by design) |
| Audit file missing | `AUDIT_LOG_PATH` writable by the app user (failures are logged, never fatal) |
| Slack approval not arriving | `SLACK_WEBHOOK_URL` set and webhook alive; without it a WARNING is logged and `requires_approval` stays `true` |

Escalation: agent infrastructure issues → platform/on-call channel; wrong RCA with
cluster impact → incident commander + postmortem, and add the case to the golden
dataset so the agent learns from it.
