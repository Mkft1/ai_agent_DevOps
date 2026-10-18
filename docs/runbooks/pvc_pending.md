# Runbook: PVC Pending (PersistentVolumeClaim stuck in Pending)

## Symptoms

- `kubectl get pvc -n <namespace>` shows STATUS `Pending` for longer than expected.
- Pod referencing the PVC hangs in `ContainerCreating` with events like:
  `Waiting for a volume to be created, either by external provisioner ... or manually created by system administrator`.
- `kubectl describe pvc <name>` reports `ProvisioningFailed`, `storageclass.storage.k8s.io "<name>" not found`, or `no volume plugin matched`.
- CSI driver pods (`csi-provisioner`, `pd-csi-driver`, etc.) log errors or are not running.

## Causes

- Requested StorageClass does not exist or was deleted/renamed; PVC has no `storageClassName` and the cluster default is missing.
- Provisioner (external CSI) is down, misconfigured, or lacks RBAC/IAM permissions to create volumes.
- Quota / limits exceeded (volume count, total GB per project or namespace).
- Unsupported access mode or volume mode combination (e.g., `ReadWriteMany` on a block-storage class).
- Node topology mismatch: `volumeBindingMode: WaitForFirstConsumer` + pod unschedulable in zones where the storage exists.
- Backend storage failure (cloud API throttling, disk type unavailable in region).

## How to fix (step-by-step)

1. Read the PVC status and events — they usually name the exact reason:
   ```bash
   kubectl -n <namespace> describe pvc <pvc-name>
   kubectl -n <namespace> get events --field-selector involvedObject.name=<pvc-name>
   ```
2. Verify the StorageClass exists and its provisioner is correct:
   ```bash
   kubectl get storageclass
   kubectl describe storageclass <sc-name>   # check "provisioner" and parameters
   ```
   If the PVC omitted `storageClassName`, patch it or set a cluster default SC.
3. Check the provisioner pods and their logs:
   ```bash
   kubectl -n kube-system get pods -l app=csi-provisioner   # adjust label per driver
   kubectl -n kube-system logs <provisioner-pod> --tail=100
   ```
4. Inspect PV pool: an existing suitable `Available` PersistentVolume may need manual binding (static provisioning):
   ```bash
   kubectl get pv | grep Available
   ```
5. Validate quotas/limits at the cloud-provider level (disk count/GB per zone) and request an increase if needed.
6. For `WaitForFirstConsumer`: schedule the pod first (fix node selectors/taints); binding happens automatically once the pod lands on a node.
7. After fixing the root cause, delete/recreate the PVC only if it is safe (data loss!) — otherwise wait for re-provisioning; watch the PVC transition `Pending -> Bound`.

## Prevention

- Ship StorageClasses as versioned manifests; never delete a class that PVCs reference.
- Alert on `kube_persistentvolumeclaim_status_phase{phase="Pending"}` older than N minutes.
- Test every new driver with a scratch PVC in CI (dynamic provisioning e2e).
