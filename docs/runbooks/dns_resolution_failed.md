# Runbook: DNS Resolution Failed (CoreDNS)

## Symptoms

- Pods fail with errors like `lookup <service>.<namespace>.svc.cluster.local: no such host`, `SERVFAIL`, or `Temporary failure in name resolution`.
- `kubectl exec` into a pod shows `/etc/resolv.conf` pointing to the cluster DNS Service IP, but queries time out.
- ClusterIP services resolve intermittently; external domains may still work via hostNetwork pods.
- CoreDNS pods show high restart counts, CrashLoopBackOff, or `ReadinessProbe failed`; `kubectl get pods -n kube-system -l k8s-app=kube-dns`.
- Prometheus alerts: `CorednsPodUnavailable`, `CoreDNSForwardErrorRatioHigh`, rising `coredns_dns_responses_total{rcode="SERVFAIL"}`.

## Causes

- CoreDNS pods crashed / evicted / not scheduled (resource limits, node pressure).
- ConfigMap `kube-dns` corrupted or edited incorrectly (loop forwarder, broken `forward . <upstream>` target).
- NetworkPolicy blocking UDP 53/9153 traffic to kube-dns/CoreDNS endpoints.
- kube-proxy/ipvs conntrack issues, stale service endpoints (`Endpoints` object for `kube-dns` is empty).
- Upstream DNS (VPC resolver / node `/etc/resolv.conf`) unreachable; ndots/search-domain misconfiguration causing query storms.

## How to fix (step-by-step)

1. Check CoreDNS health:
   ```bash
   kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide
   kubectl -n kube-system logs deploy/coredns --tail=100
   kubectl -n kube-system describe pods -l k8s-app=kube-dns | grep -A5 Events
   ```
2. Verify the service and endpoints exist and are populated:
   ```bash
   kubectl -n kube-system get svc kube-dns
   kubectl -n kube-system get endpoints kube-dns   # must list CoreDNS pod IPs
   ```
3. Test resolution from inside a debug pod:
   ```bash
   kubectl run dns-debug --rm -it --image=busybox:1.36 --restart=Never -- sh
   nslookup kubernetes.default.svc.cluster.local <cluster-dns-ip>
   nslookup google.com
   ```
4. Inspect Corefile for syntax/logic errors:
   ```bash
   kubectl -n kube-system get configmap coredns -o yaml
   ```
   Fix `forward`, remove loops, then roll the deployment: `kubectl -n kube-system rollout restart deploy coredns`.
5. Audit NetworkPolicies that could block DNS: any policy selecting CoreDNS pods or the client pods must allow **UDP/TCP port 53** to the kube-dns Service CIDR.
6. If CoreDNS pods are healthy but resolution fails on nodes, check conntrack/kube-proxy and restart kube-proxy DaemonSet as a last resort.
7. Validate recovery: re-run `nslookup` from step 3; watch `coredns_dns_responses_total` return to normal rcode mix.

## Prevention

- Keep at least 2 CoreDNS replicas across nodes; enable PodDisruptionBudget.
- Gate Corefile changes through review + `kubectl apply --dry-run=server`.
- Alert on SERVFAIL ratio and on empty `kube-dns` Endpoints.
