# Runbook: CreateContainerConfigError (missing Secret / ConfigMap)

## Symptoms
- Pod status: `Pending`, container waiting reason: `CreateContainerConfigError`.
- Events: `Error: secret "<name>" not found` or `configmap "<name>" not found`.
- Pod never starts; restarts stay 0; `kubectl describe pod` shows `Warning Failed` with the missing object name.

## Root cause
The container spec references a `Secret` or `ConfigMap` (via `envFrom`, `env.valueFrom`, or `volumes`) that does not exist in the pod's namespace. Typical triggers: manifest applied before the secret, secret deleted/rotated by another team, Helm release deployed without its values-secret, namespace recreated without copying dependent objects.

## How to fix
1. Identify the missing object: `kubectl describe pod <pod> | grep -A2 "CreateContainerConfigError"`.
2. Check what exists in the namespace: `kubectl get secret,configmap -n <ns>`.
3. Recreate the missing object, e.g.:
   ```bash
   kubectl create secret generic <name> --from-literal=KEY=VALUE -n <ns>
   # or
   kubectl apply -f secret.yaml -n <ns>
   ```
4. If the object lives in another namespace — copy it (secrets are namespaced):
   ```bash
   kubectl get secret <name> -n <src> -o yaml | sed "s/namespace: <src>/namespace: <ns>/" | kubectl apply -f -
   ```
5. Delete the pod to force re-scheduling: `kubectl delete pod <pod> -n <ns>`.

## Prevention
- Manage secrets/configmaps in the same GitOps path as the workloads that consume them (same Helm chart / Kustomize base).
- Add CI checks (`kubeconform`, `pluto`, or a simple OPA rule) that validate referenced `secretKeyRef`/`configMapKeyRef` objects exist.
- Alert on `CreateContainerConfigError` waiting reason — it never resolves without human action.
