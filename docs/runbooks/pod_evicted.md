# Runbook: Pod Evicted (node resource pressure)

## Symptoms
- Pod status: `Failed`, reason: `Evicted`; message contains `The node was low on resource: memory|ephemeral-storage|disk`.
- Events: `Warning Evicted` from `kubelet`; pod disappears from the workload and a replacement is scheduled elsewhere.
- Node condition `MemoryPressure` / `DiskPressure` / `PIDPressure` = `True` (`kubectl describe node`).

## Root cause
The kubelet evicts pods to protect node stability when a resource pressure threshold is crossed (memory, ephemeral storage, inodes, PIDs). Pods without `requests`/`limits` or with low `priorityClassName` are evicted first. Common drivers: log/file growth in `emptyDir`, missing ephemeral-storage limits, memory-hungry neighbour pod, node undersized for the scheduled load.

## How to fix
1. Confirm the eviction reason: `kubectl get events -n <ns> --field-selector reason=Evicted`.
2. Check node pressure: `kubectl describe node <node> | grep -A8 "Conditions"`.
3. Free the pressured resource on the node:
   ```bash
   # ephemeral storage: clean up logs / emptyDir of the noisiest pod
   kubectl debug node/<node> -it --image=busybox -- du -sh /var/log/pods/* | sort -h | tail
   ```
4. Set explicit `resources.requests/limits` (including `ephemeral-storage`) so eviction order is predictable:
   ```bash
   kubectl patch deployment <deploy> -p '{"spec":{"template":{"spec":{"containers":[{"name":"<c>","resources":{"requests":{"memory":"256Mi","ephemeral-storage":"1Gi"},"limits":{"memory":"512Mi","ephemeral-storage":"2Gi"}}}]}}}}'
   ```
5. For critical workloads set a higher `priorityClassName` so they are evicted last.
6. If the node is genuinely undersized — scale the node group or rebalance with topology spread / anti-affinity.

## Prevention
- Always define requests/limits and `ephemeral-storage` caps; monitor `container_fs_usage_bytes` vs node capacity.
- Ship logs to a collector instead of accumulating files on node disk; cap `emptyDir.sizeLimit`.
- Alert on node conditions `MemoryPressure`/`DiskPressure` before eviction happens.
