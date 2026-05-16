# KubeVirt VM cheat sheet

Use this when adding another KubeVirt VM workload to the homelab. It captures the
repeatable path proven by the `actual` pilot: static local PV storage, CDI image
import, Secret-backed cloud-init, manual VM start, guest-agent validation, and
internal Service validation before any public route is added.

For the full pilot narrative and troubleshooting details, see
[`actual-pilot-deployment.md`](actual-pilot-deployment.md).

## Golden rules

- Keep VM resources GitOps-managed, but keep runtime start/stop explicit with
  `runStrategy: Manual`.
- Do not commit secrets, SSH private keys, app credentials, or first-run tokens.
- Do not add a public Traefik route until internal validation, backups,
  rollback, auth, and monitoring are ready.
- Do not destructively recreate a root disk after accepted app data is migrated
  unless a verified backup or snapshot exists.
- Prefer pinned image versions or digests over `latest` for production cutover.

## Pick VM values

Before writing manifests, choose these values:

| Value | Example | Notes |
|---|---|---|
| VM name | `actual` | Used for VM, Service, labels, and DNS |
| Namespace | `vms` | Keep internet-facing VM workloads isolated |
| Root disk PV | `actual-rootdisk` | Static local PV name |
| Host disk path | `/flash/enc/vms/kubevirt/actual/rootdisk` | Must exist on `blizzard` |
| StorageClass | `kubevirt-local-immediate` | Immediate binding for static local PVs |
| Base image | Debian genericcloud qcow2 | Use normal cloud images, not NixOS MicroVM images |
| App port | `11051` | KubeVirt/Service-facing port |
| Guest container port | `5006` | Only needed for Podman/Docker apps |

## Files to add

Create a directory under `vms/`:

```text
vms/<vm-name>/
  cloudinit-secret.yaml
  datavolume.yaml
  kustomization.yaml
  networkpolicy.yaml
  pv.yaml
  service.yaml
  virtualmachine.yaml
```

Then include the directory from the parent `vms/kustomization.yaml` if it is not
already selected by the existing layout.

## Host storage

Declare the host path in `nix-config` when possible, usually with tmpfiles for
`blizzard`. For a manual preflight or emergency fix:

```bash
sudo install -d -m 0700 -o root -g root /flash/enc/vms/kubevirt/<vm-name>/rootdisk
```

Static local PVs should use node affinity for `blizzard` and `Retain` reclaim
policy. The DataVolume must force binding to that PV:

```yaml
spec:
  pvc:
    storageClassName: kubevirt-local-immediate
    volumeName: <vm-name>-rootdisk
```

Do not remove `volumeName` unless moving away from static `kubernetes.io/no-provisioner`
local PVs.

## Cloud-init baseline

Use a Secret-backed NoCloud volume for anything non-trivial:

```yaml
cloudInitNoCloud:
  secretRef:
    name: <vm-name>-cloudinit
```

For Debian guests, the proven baseline is:

- clamp the default guest interface MTU to `1280` before package downloads;
- use explicit Debian HTTP sources and signed package metadata;
- use short apt timeouts and force IPv4 during bootstrap;
- install `qemu-guest-agent` and enable it;
- install app runtime packages;
- configure UFW before exposing the service;
- create a bootstrap marker such as `/var/lib/<vm-name>-bootstrap.done`.

For Podman-based app VMs with `--no-install-recommends`, include `nftables`:

```bash
apt-get install -y --no-install-recommends ca-certificates nftables podman qemu-guest-agent ufw
```

If Podman publishes a host port to a container bridge, UFW sees the app traffic
as routed traffic from the VM interface to `podman0`. Keep routed default deny,
but add a narrow forward allow for the k3s pod CIDR and container port:

```bash
ufw route allow \
  in on "$default_iface" \
  out on podman0 \
  proto tcp \
  from 10.42.0.0/16 \
  to 10.88.0.0/16 \
  port <container-port>
```

Update the CIDRs if the k3s pod CIDR or Podman bridge subnet changes.

## VM manifest checklist

In `virtualmachine.yaml`:

- set `runStrategy: Manual`;
- use the DataVolume as the root disk;
- use masquerade networking unless a VM has a specific reason not to;
- expose only required ports;
- apply stable labels such as:
  - `app.kubernetes.io/name: <vm-name>`
  - `app.kubernetes.io/part-of: kubevirt-migration`
  - `vm.kubevirt.io/name: <vm-name>` is added by KubeVirt and commonly used by
    Services.

## NetworkPolicy checklist

Start with default deny for the namespace/workload. Then add only what is needed:

- ingress from the `ingress` namespace to the app port;
- DNS egress needed during bootstrap;
- public HTTP/HTTPS egress for package/image downloads, excluding private CIDRs;
- any workload-specific egress after the app is understood.

For KubeVirt masquerade guests, DNS to CoreDNS pods alone may not be enough.
The `actual` pilot allows both kube-dns pods and the kube-dns ClusterIP for
cluster DNS diagnostics, while bootstrap uses pinned public resolvers with a
narrow DNS egress rule.

## Reconcile and import

After the manifests are committed and pushed by an operator:

```bash
flux reconcile kustomization storage -n flux-system --with-source
flux reconcile kustomization kubevirt-config -n flux-system --with-source
flux reconcile kustomization cdi-config -n flux-system --with-source
flux reconcile kustomization vms -n flux-system --with-source
```

Wait for the root disk import:

```bash
kubectl -n vms wait --for=jsonpath='{.status.phase}'=Succeeded datavolume/<vm-name>-rootdisk --timeout=20m
kubectl -n vms get dv,pvc -o wide | grep <vm-name>-rootdisk
kubectl get pv <vm-name>-rootdisk
```

## Start and validate the VM

Start manually only after CDI import succeeds:

```bash
virtctl start <vm-name> -n vms
kubectl -n vms wait --for=condition=Ready vmi/<vm-name> --timeout=5m
kubectl -n vms wait --for=condition=AgentConnected vmi/<vm-name> --timeout=10m
virtctl guestosinfo <vm-name> -n vms
```

Check the launcher pod and Service endpoint:

```bash
kubectl -n vms get vmi,pod,svc -l app.kubernetes.io/name=<vm-name> -o wide
kubectl -n vms get endpointslice -l kubernetes.io/service-name=<vm-name> -o wide
```

## Validate the app Service

Validate from the namespace that is supposed to reach the VM, usually `ingress`.
This proves the same namespace-to-Service path Traefik will use later:

```bash
kubectl -n ingress run <vm-name>-curl \
  --rm -i \
  --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --overrides='{"spec":{"securityContext":{"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"curl","image":"curlimages/curl:8.10.1","args":["-sS","-o","/dev/null","-w","http_code=%{http_code}\\n","--connect-timeout","10","--max-time","30","http://<vm-name>.vms.svc.cluster.local:<app-port>/"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000}}]}}'
```

Avoid using `kubectl port-forward svc/<vm-name>` as the primary validation for
masquerade-mode VMs. It can test a different path than normal ClusterIP traffic.

## Temporary UI access

If there is no public route yet, use a short-lived proxy pod in `ingress` and
port-forward to it. Delete it when finished.

```bash
kubectl -n ingress delete pod <vm-name>-ui-proxy --ignore-not-found --wait=true
kubectl -n ingress run <vm-name>-ui-proxy \
  --restart=Never \
  --image=alpine/socat \
  --overrides='{"spec":{"securityContext":{"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"proxy","image":"alpine/socat","args":["-dd","TCP-LISTEN:<app-port>,fork,reuseaddr","TCP:<vm-name>.vms.svc.cluster.local:<app-port>"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000}}]}}'
kubectl -n ingress wait --for=condition=Ready pod/<vm-name>-ui-proxy --timeout=60s
kubectl -n ingress port-forward --address "$(tailscale ip -4)" pod/<vm-name>-ui-proxy <app-port>:<app-port>
```

Open `http://<blizzard-tailscale-ip>:<app-port>/` while the port-forward is
running, then clean up:

```bash
kubectl -n ingress delete pod <vm-name>-ui-proxy --ignore-not-found
```

## Recreate a pilot root disk

Only do this for pre-acceptance pilot disks or when a verified backup exists.
Cloud-init changes require a fresh first boot because the imported image records
first-boot state.

```bash
flux suspend kustomization vms -n flux-system
virtctl stop <vm-name> -n vms || true
kubectl -n vms delete vmi <vm-name> --ignore-not-found --wait=true
kubectl -n vms delete vm <vm-name> --ignore-not-found --wait=true
kubectl -n vms delete dv <vm-name>-rootdisk --ignore-not-found --wait=true
kubectl -n vms delete pvc <vm-name>-rootdisk --ignore-not-found --wait=true
kubectl delete pv <vm-name>-rootdisk --ignore-not-found --wait=true
sudo install -d -m 0700 -o root -g root /flash/enc/vms/kubevirt/<vm-name>/rootdisk
sudo find /flash/enc/vms/kubevirt/<vm-name>/rootdisk -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
flux resume kustomization vms -n flux-system
flux reconcile kustomization vms -n flux-system --with-source
```

## Troubleshooting quick map

| Symptom | Likely cause | First check |
|---|---|---|
| DataVolume stays `Pending` | PV/PVC binding issue | `kubectl -n vms describe dv <name>` |
| DataVolume spec cannot update | CDI immutable spec | Recreate pilot disk resources if safe |
| VMI Ready, no guest agent | Bootstrap has not installed/started qemu agent | `kubectl -n vms logs -l vm.kubevirt.io/name=<vm>` |
| Service returns refused | Guest/app is not listening | `ss -ltnp` inside guest via qemu agent |
| Service times out | Guest firewall or NetworkPolicy path | UFW logs and Cilium drops |
| Podman says `nft` missing | `nftables` package missing | Add `nftables` to bootstrap packages |
| DNS to kube-dns is odd | KubeVirt masquerade/Service IP path | Check kube-dns ClusterIP and egress policy |

## Acceptance criteria

A VM is ready for the next cutover stage when:

- Flux reconciles the VM manifests successfully;
- PV/PVC/DataVolume bind and import cleanly;
- VM starts manually and remains running;
- qemu-guest-agent responds;
- cloud-init/bootstrap completes and has a marker;
- the app listens inside the guest;
- the Kubernetes Service returns HTTP from an allowed namespace;
- no public route exists until rollback, backups, auth, and monitoring are ready.
