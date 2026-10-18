# Runbook: CrashLoopBackOff

## Symptoms
- Pod repeatedly restarts: status `CrashLoopBackOff`, high restart count.
- Back-off delay grows: 10s → 20s → 40s ... up to 5m.

## Root cause
The container starts and exits with a non-zero code shortly after launch. Causes: app misconfiguration (missing env var, bad config file), failing startup probes, exception at boot (stack trace in logs), missing volume/mount, wrong command/entrypoint, dependency unavailable (DB not ready).

## How to fix
1. Get previous container logs: `kubectl logs <pod> --previous` — look for panic/traceback/exit message.
2. Check exit code: 1 = app error, 137 = SIGKILL/OOM, 139 = segfault, 143 = SIGTERM.
3. Validate config: compare env vars and mounted ConfigMaps with a known-good revision (`kubectl rollout history`).
4. If a rollback is needed: `kubectl rollout undo deployment/<deploy>`.
5. To debug interactively (write actions allowed): exec into the crashed container via an ephemeral debug pod and run the entrypoint manually.
6. Confirm dependencies (DB, queues) are reachable from the pod network policy.

## Prevention
- Add proper readiness/liveness probes with sensible initialDelaySeconds.
- Validate configuration in CI (schema check, `--dry-run` boot mode).
