# Runbook: Node NotReady (kubelet / node health)

## Symptoms

- `kubectl get nodes` shows a node with STATUS `NotReady`.
- Pods on the node are stuck in `Terminating`, or get evicted; new pods cannot be scheduled there.
- Events on the node: `NodeStatusUnknown`, `Kubelet stopped posting node status`, reason `NodeNotReady`.
- The last heartbeat (`node.status.conditions[Ready].lastHeartbeatTime`) is stale by minutes.
- Prometheus alert `NodeNotReady` / `InstanceDown` for the affected node exporter target.

## Causes

- kubelet crashed, hung, or failed to start (bad config, certificate expired/rotated incorrectly).
- Node resource exhaustion: memory/disk pressure, PID pressure — kubelet reports conditions `MemoryPressure`, `DiskPressure`, `PIDPressure=True`; root disk full is a classic cause.
- Container runtime failure (containerd/docker daemon down) — kubelet cannot talk to CRI socket.
- Network partition between node and API server, or clock skew breaking TLS validation.
- Cloud instance stopped/rebooted, IAM/security-group changes blocking port 10250.
- Kernel panic / OOM killer killing critical system processes.

## How to fix (step-by-step)

1. Get the node's conditions and events:
   ```bash
   kubectl describe node <node-name> | sed -n '/Conditions/,/Events/p'
   kubectl get events --field-selector involvedObject.name=<node-name>
   ```
2. Check the Ready condition detail: `False` + message often says exactly what is wrong (e.g., `PLEG not healthy`, runtime unreachable).
3. SSH into the node (or use the cloud console/serial access if SSH is dead):
   ```bash
   systemctl status kubelet containerd
   journalctl -u kubelet --since "-30m" --no-pager | tail -100
   df -h / /var/lib/kubelet /var/lib/containerd   # look for 100% usage
   free -m; dmesg | tail -50                       # OOM / kernel errors
   ```
4. Fix the concrete problem:
   - Disk full → garbage-collect images/logs, expand the disk;
   - Runtime down → `systemctl restart containerd`, then `systemctl restart kubelet`;
   - Expired kubelet certs → follow the control-plane certificate rotation procedure for the node;
   - Clock skew → sync NTP/chrony;
   - Network/firewall → ensure port 10250 reachability from the control plane.
5. Watch the node recover: `kubectl get nodes -w` until STATUS returns to `Ready`; verify pods rescheduled/healthy.
6. If the node is unsalvageable: cordon and drain it, then replace the instance:
   ```bash
   kubectl cordon <node-name>
   kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
   ```
7. Confirm no lingering taints (`node.kubernetes.io/unreachable`, `not-ready`) block scheduling after recovery.

## Prevention

- Alert early on `DiskPressure`/`MemoryPressure` conditions and on kubelet heartbeat age.
- Automate log/image rotation (`containerLogMaxSize`, image GC thresholds).
- Monitor certificate expiry exporters; rotate before expiration.
- Keep nodes as cattle: autoscaling groups / machine pools with fast replacement.
