# Runbook: ImagePullBackOff / ErrImagePull

## Symptoms
- Pod stuck in `Pending`/`ImagePullBackOff`; container waiting reason: `ImagePullBackOff` or `ErrImagePull`.
- Events: `Failed to pull image "...": rpc error: code = Unknown desc = ... not found` or `unauthorized`.

## Root cause
The kubelet cannot pull the container image. Typical causes: wrong image name/tag (image does not exist in the registry), missing/invalid registry credentials (imagePullSecrets), network/DNS problems to the registry, rate limiting by the registry.

## How to fix
1. Read the exact error from events: `kubectl get events --field-selector reason=Failed -n <ns>` or `kubectl describe pod <pod>`.
2. Validate the image path locally: `docker pull <image>` (or `crane digest <image>`) — if it fails, the tag/name is wrong.
3. Check the Deployment spec: `kubectl get deploy <deploy> -o jsonpath='{..image}'` — look for typos and non-existent tags.
4. If error is `unauthorized`: verify `imagePullSecrets` on the ServiceAccount/Pod and registry credentials validity.
5. If the tag was pushed recently — check registry replication/CDN lag; delete the pod to force re-pull.
6. Fix the manifest and `kubectl rollout undo` if a bad release was deployed.

## Prevention
- Pin immutable tags/digests, forbid `latest` in production.
- Add CI check that the image exists before deploying.
