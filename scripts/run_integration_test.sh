#!/usr/bin/env bash
#
# Integration test: spin up a kind cluster, create a "bad" pod that ends in
# ImagePullBackOff, run the agent locally and verify that POST /diagnose
# identifies an image/pull related root cause.
#
# The script is IDEMPOTENT: any pre-existing cluster/pod from a previous run
# is deleted before creating new resources, and cleanup runs on exit (trap).
#
# Usage:  ./scripts/run_integration_test.sh
# Exit:   0 - agent diagnosed the incident correctly
#         1 - assertion failed or environment problem
#
set -euo pipefail

CLUSTER_NAME="agent-e2e"
NAMESPACE="default"
BAD_POD="bad-pod"
BAD_IMAGE="nginx:definitely-bad-tag-404"
AGENT_HOST="127.0.0.1"
AGENT_PORT="8000"
AGENT_URL="http://${AGENT_HOST}:${AGENT_PORT}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UVICORN_PID=""

log() { printf '\n[integration] %s\n' "$*"; }
fail() { printf '[integration][ERROR] %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites
require() {
    command -v "$1" >/dev/null 2>&1 || fail "'$1' is required but not installed (see README: kind/kubectl/jq/docker)"
}

require docker
require kind
require kubectl
require jq
require curl
require python3
kind version >/dev/null 2>&1 || fail "kind is installed but 'kind version' failed"
log "prerequisites OK: $(kind version)"

# ---------------------------------------------------------------------- cleanup
stop_unicorn() {
    if [[ -n "${UVICORN_PID}" ]] && kill -0 "${UVICORN_PID}" 2>/dev/null; then
        log "stopping uvicorn (pid ${UVICORN_PID})"
        kill "${UVICORN_PID}" 2>/dev/null || true
        wait "${UVICORN_PID}" 2>/dev/null || true
    fi
    UVICORN_PID=""
}

cleanup() {
    stop_unicorn
    # Delete the bad pod (ignore errors) and the whole cluster.
    kubectl --namespace "${NAMESPACE}" delete pod "${BAD_POD}" --ignore-not-found --grace-period=0 --force >/dev/null 2>&1 || true
    if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
        log "deleting kind cluster '${CLUSTER_NAME}'"
        kind delete cluster --name "${CLUSTER_NAME}" || true
    fi
}
trap cleanup EXIT

# ------------------------------------------------------- idempotent cluster init
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    log "cluster '${CLUSTER_NAME}' already exists (leftover) -> deleting it first"
    kind delete cluster --name "${CLUSTER_NAME}"
fi

log "creating kind cluster from scripts/kind/cluster.yaml"
kind create cluster --name "${CLUSTER_NAME}" --config "${REPO_ROOT}/scripts/kind/cluster.yaml" --wait 120s

log "exporting kubeconfig"
kind export kubeconfig --name "${CLUSTER_NAME}"
kubectl cluster-info
kubectl get nodes

# ------------------------------------------------------------ inject the fault
log "removing leftover bad pod (idempotency)"
kubectl --namespace "${NAMESPACE}" delete pod "${BAD_POD}" --ignore-not-found --grace-period=0 --force >/dev/null 2>&1 || true

log "creating bad pod (${BAD_IMAGE} -> ImagePullBackOff)"
kubectl run "${BAD_POD}" --image="${BAD_IMAGE}" --namespace="${NAMESPACE}"

log "waiting 10s for the pod to settle into ImagePullBackOff"
sleep 10
kubectl --namespace "${NAMESPACE}" describe pod "${BAD_POD}" | grep -E "Image|Reason|Back-off" || true

# ------------------------------------------------------------- start the agent
log "installing python dependencies (quietly)"
python3 -m pip install --quiet --disable-pip-version-check -r "${REPO_ROOT}/requirements.txt" 2>/dev/null || \
    python3 -m pip install --quiet --disable-pip-version-check -r "${REPO_ROOT}/requirements.txt"

log "starting agent: uvicorn src.main:app on ${AGENT_URL}"
(
    cd "${REPO_ROOT}"
    LOG_LEVEL=INFO KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}" \
    python3 -m uvicorn src.main:app --host "${AGENT_HOST}" --port "${AGENT_PORT}" \
        > /tmp/agent_integration.log 2>&1 &
    echo $! > /tmp/agent_integration.pid
)
UVICORN_PID="$(cat /tmp/agent_integration.pid)"

log "waiting 5s for the agent to boot"
sleep 5
curl -sf "${AGENT_URL}/health" >/dev/null || { tail -n 40 /tmp/agent_integration.log; fail "agent did not become healthy (/health)"; }
log "agent is healthy"

# ---------------------------------------------------------- diagnose via HTTP
log "POST ${AGENT_URL}/diagnose"
RESPONSE_FILE="/tmp/diagnose_response.json"
HTTP_CODE=$(curl -sS -o "${RESPONSE_FILE}" -w '%{http_code}' \
    -X POST "${AGENT_URL}/diagnose" \
    -H 'Content-Type: application/json' \
    --max-time 180 \
    -d "{\"namespace\": \"${NAMESPACE}\", \"pod_name\": \"${BAD_POD}\", \"description\": \"Pod in ImagePullBackOff\"}") \
    || { tail -n 40 /tmp/agent_integration.log; fail "request to /diagnose failed"; }

[[ "${HTTP_CODE}" == "200" ]] || { cat "${RESPONSE_FILE}"; fail "/diagnose returned HTTP ${HTTP_CODE}, expected 200"; }

ROOT_CAUSE=$(jq -r '.diagnosis.root_cause // empty' "${RESPONSE_FILE}")
[[ -n "${ROOT_CAUSE}" ]] || { cat "${RESPONSE_FILE}"; fail "response has no diagnosis.root_cause"; }
log "root_cause: ${ROOT_CAUSE}"

# --------------------------------------------------------------- assertion
if printf '%s' "${ROOT_CAUSE}" | grep -qiE 'image|pull'; then
    log "SUCCESS: root_cause mentions image/pull — agent detected the ImagePullBackOff incident"
else
    cat "${RESPONSE_FILE}"
    fail "ASSERTION FAILED: root_cause does not contain 'image' or 'pull'"
fi

exit 0
