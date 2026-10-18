# Runbook: OOMKilled (Memory limit exceeded)

## Symptoms
- Pod status: `OOMKilled`, container restarts with reason `OOMKilled`.
- Events: `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`.
- Prometheus: `container_memory_working_set_bytes` close to `container_spec_memory_limit_bytes` before kill.

## Root cause
The container exceeded its memory limit (`resources.limits.memory`). The kernel OOM killer terminated the process. Common causes: undersized limit, memory leak, sudden traffic spike, JVM not respecting container limits.

## How to fix
1. Check current limits: `kubectl get pod <pod> -o jsonpath='{.spec.containers[*].resources}'`.
2. Inspect memory trend in Prometheus: `container_memory_working_set_bytes{pod="<pod>"}` — if it grows monotonically, suspect a leak.
3. Increase the memory limit in the Deployment (e.g. +50%) and roll out:
   ```bash
   kubectl patch deployment <deploy> -p '{"spec":{"template":{"spec":{"containers":[{"name":"<c>","resources":{"limits":{"memory":"512Mi"}}}]}}}}'
   ```
4. For JVM apps set `-XX:MaxRAMPercentage=75` instead of raw heap size.
5. If a leak is confirmed — file a bug to the app team; restarting only hides the problem.

## Prevention
- Set both requests and limits; monitor `working_set / limit` ratio (>80% for 10m = alert).
- Enable HPA on memory only as a last resort (prefer CPU/custom metrics).
