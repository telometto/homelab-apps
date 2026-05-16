# Actual pilot VM deployment

This runbook deploys the first KubeVirt pilot VM: `actual` in the `vms` namespace. The pilot intentionally does not create a public Traefik route; it proves the operator stack, local PV binding, CDI image import, VM boot, qemu-guest-agent, and internal service reachability first.

## What this deploys

| Resource | Location | Purpose |
|---|---|---|
| `StorageClass/kubevirt-local-immediate` | `storage/storageclass-immediate.yaml` | Immediate-binding class for pre-imported local boot disks |
| `PersistentVolume/actual-rootdisk` | `vms/actual/pv.yaml` | Binds the VM boot disk to `/flash/enc/vms/kubevirt/actual/rootdisk` on `blizzard` |
| `DataVolume/actual-rootdisk` | `vms/actual/datavolume.yaml` | Imports the Debian Stable cloud image into the root disk PVC |
| `VirtualMachine/actual` | `vms/actual/virtualmachine.yaml` | Manual-control KubeVirt VM; starts only when explicitly requested |
| `Service/actual` | `vms/actual/service.yaml` | ClusterIP service for the Actual HTTP port `11051` |
| `NetworkPolicy/actual-*` | `vms/actual/networkpolicy.yaml` | Allows Traefik ingress and temporary bootstrap egress for DNS/HTTP/HTTPS |

## Prerequisites

1. `blizzard` has the latest `nix-config` `dev-kubevirt` host config applied.
1. k3s is running with Cilium and Flux bootstrapped.
1. Flux can read this repository from `ssh://git@github.com/telometto/homelab-apps`.
1. `/flash/enc/vms/kubevirt/actual/rootdisk` exists on `blizzard`.
1. KubeVirt and CDI are installed and available.
1. The `vms` namespace exists.

The NixOS host config should create the required local PV path with tmpfiles. If the path is missing after rebuild, create it before reconciling the VM resources:

```bash
sudo install -d -m 0700 -o root -g root /flash/enc/vms/kubevirt/actual/rootdisk
```

## Deploy host bootstrap

From `nix-config` on `blizzard`:

```bash
git checkout dev-kubevirt
git pull
sudo nixos-rebuild switch --flake .#blizzard
```

Wait for k3s and the bootstrap service:

```bash
systemctl status k3s --no-pager
systemctl status k3s-helm-bootstrap --no-pager
journalctl -u k3s-helm-bootstrap -b --no-pager
```

## Verify cluster operators

```bash
kubectl get nodes
kubectl -n kube-system rollout status daemonset/cilium --timeout=5m
kubectl -n flux-system get pods
kubectl get kustomizations -A
kubectl get kubevirt -n kubevirt
kubectl get cdi
```

Expected:

- `blizzard` is `Ready`.
- Cilium DaemonSet is rolled out.
- Flux controllers are running.
- `kubevirt-config` and `cdi-config` Kustomizations are ready.
- KubeVirt and CDI report available/ready status.

## Upgrade from earlier pilot manifests

Skip this section on a fresh cluster. Run it only if an earlier revision of this
pilot already created `actual-rootdisk` with the `kubevirt-local` StorageClass.
Kubernetes does not allow changing `PersistentVolume` or `PersistentVolumeClaim`
`storageClassName` in place, so the pre-acceptance pilot root disk must be
deleted and recreated before reconciling the `kubevirt-local-immediate` version.
Run the cleanup after this revision has been pushed and fetched by Flux; if you
clean up earlier, keep the `vms` Kustomization suspended until the new revision
is ready so Flux does not recreate the old `kubevirt-local` objects.

Check the current storage class first:

```bash
kubectl get pv actual-rootdisk -o jsonpath='{.spec.storageClassName}{"\n"}' 2>/dev/null || true
kubectl -n vms get pvc actual-rootdisk -o jsonpath='{.spec.storageClassName}{"\n"}' 2>/dev/null || true
```

If either command prints `kubevirt-local`, suspend VM reconciliation, remove the
old pilot resources, and then resume reconciliation. Do not run this after
migrating accepted Actual data unless you have a tested backup or snapshot.

```bash
flux suspend kustomization vms -n flux-system
virtctl stop actual -n vms || true
kubectl -n vms delete vmi actual --ignore-not-found --wait=true
kubectl -n vms delete vm actual --ignore-not-found --wait=true
kubectl -n vms delete dv actual-rootdisk --ignore-not-found --wait=true
kubectl -n vms delete pvc actual-rootdisk --ignore-not-found --wait=true
kubectl delete pv actual-rootdisk --ignore-not-found --wait=true
flux resume kustomization vms -n flux-system
```

If a failed or partial import left files in `/flash/enc/vms/kubevirt/actual/rootdisk`,
inspect and clean that directory manually before retrying the import. The
directory itself must continue to exist.

## Reconcile homelab-apps

After committing and pushing this repo:

```bash
flux reconcile source git flux-system -n flux-system
flux reconcile kustomization storage -n flux-system --with-source
flux reconcile kustomization kubevirt-config -n flux-system --with-source
flux reconcile kustomization cdi-config -n flux-system --with-source
flux reconcile kustomization vms -n flux-system --with-source
```

Then check the pilot resources:

```bash
kubectl -n vms get pv,pvc,dv,vm,svc,networkpolicy
kubectl -n vms describe dv actual-rootdisk
kubectl -n vms describe pvc actual-rootdisk
```

Expected:

- `PersistentVolume/actual-rootdisk` exists.
- `PersistentVolumeClaim/actual-rootdisk` is bound.
- `DataVolume/actual-rootdisk` completes successfully using the `kubevirt-local-immediate` StorageClass.
- `VirtualMachine/actual` exists with `runStrategy: Manual`.

The DataVolume uses `spec.pvc.volumeName: actual-rootdisk` to force binding to
the static local PV. Do not remove this unless the storage model changes away
from static `kubernetes.io/no-provisioner` local PVs; without it, Kubernetes may
try dynamic provisioning and leave the DataVolume `Pending` indefinitely.

If Flux later reports `Cannot update DataVolume Spec`, the live DataVolume was
created from an older manifest. CDI treats the DataVolume spec as immutable. For
this pilot, recreate the root disk resources after confirming no accepted
application data has been migrated; do not try to patch the DataVolume in place.

## Start the pilot VM

Only start the VM after the DataVolume import succeeds. The VM manifest uses
`runStrategy: Manual`, so Flux can continue reconciling `./vms` without
drift-correcting the manual start back to a stopped state:

```bash
virtctl start actual -n vms
kubectl -n vms get vmi actual -w
```

If `virtctl` is not installed on the host yet, use the nixpkgs KubeVirt client
package for this command, then rebuild `blizzard` after the matching
`nix-config` package change is applied:

```bash
nix shell nixpkgs#kubevirt -c virtctl start actual -n vms
```

Confirm qemu-guest-agent:

```bash
virtctl guestosinfo actual -n vms
```

If the VM does not boot, inspect:

```bash
kubectl -n vms get pods -l vm.kubevirt.io/name=actual
kubectl -n vms describe vmi actual
kubectl -n vms logs -l vm.kubevirt.io/name=actual --tail=200
```

## Validate the Actual service

Probe the ClusterIP service from a temporary pod in the `ingress` namespace. The
pilot NetworkPolicy permits ingress from that namespace, so this exercises the
same namespace-to-Service path that Traefik will use later without creating a
public route:

```bash
kubectl -n ingress run actual-curl \
  --rm -i \
  --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --overrides='{"spec":{"securityContext":{"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"actual-curl","image":"curlimages/curl:8.10.1","args":["-sv","--connect-timeout","10","http://actual.vms.svc.cluster.local:11051/"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000}}]}}'
```

Do not use `kubectl port-forward svc/actual` as the primary validation for this
masquerade-mode VM. Kubernetes port-forward connects to `localhost` inside the
`virt-launcher` pod network namespace; that can fail with `connection refused`
even when normal ClusterIP Service traffic to the VM datapath is working.

If the Service has an EndpointSlice for the VMI IP but the in-cluster curl probe
returns `connection refused`, Kubernetes routing is working and the guest is not
listening on `11051` yet. Check cloud-init, `qemu-guest-agent`, and the in-guest
`actual-server.service` before changing the Service.

If `virt-launcher` logs show cloud-init repeatedly retrying Debian mirrors, for
example `Ign: https://deb.debian.org/debian trixie InRelease`, cloud-init is
stuck before it installs `qemu-guest-agent`, `podman`, and the Actual systemd
unit. The current manifest avoids cloud-init's package module for the pilot:
the VM references `Secret/actual-cloudinit` via `cloudInitNoCloud.secretRef`
because KubeVirt rejects inline `cloudInitNoCloud.userData` larger than 2048 bytes. That
cloud-init payload writes `actual-bootstrap.service`, then that service replaces
the Debian mirrorlist sources with explicit HTTP Debian sources, forces IPv4,
sets the guest default-interface MTU to `1280` to match the Cilium-backed
`virt-launcher` pod path, applies short apt timeouts, installs the guest packages, starts
`qemu-guest-agent`, and then starts `actual-server.service`. HTTP is acceptable
here because Debian package integrity is enforced by signed release metadata and
package signatures.

For KubeVirt masquerade networking, do not assume a NetworkPolicy DNS rule that
selects the CoreDNS pods is enough. The Debian guest initially receives the
kube-dns ClusterIP in its routes/resolver configuration, and Cilium can evaluate
the guest-originated packet as `virt-launcher` pod IP -> kube-dns Service IP.
The pilot egress policy therefore allows both the `k8s-app=kube-dns` pods and
the current kube-dns ClusterIP `10.43.0.10/32` on TCP/UDP 53 for diagnostics and
future cluster-DNS use. During bootstrap, however, the guest writes a static
`/etc/resolv.conf` using pinned Cloudflare resolvers (`1.1.1.1` and `1.0.0.1`),
and the NetworkPolicy allows only those public resolver IPs on TCP/UDP 53. If
the Service CIDR, kube-dns IP, or chosen bootstrap resolvers change, update
`vms/actual/networkpolicy.yaml` before recreating or bootstrapping the VM.

If this was changed after the VM already booted once, recreate the pilot root
disk after pushing the fix because cloud-init has already cached first-boot state
in the imported image:

```bash
flux suspend kustomization vms -n flux-system
nix shell nixpkgs#kubevirt -c virtctl stop actual -n vms || true
kubectl -n vms delete vm actual --ignore-not-found --wait=true
kubectl -n vms delete vmi actual --ignore-not-found --wait=true
kubectl -n vms delete dv actual-rootdisk --ignore-not-found --wait=true
kubectl -n vms delete pvc actual-rootdisk --ignore-not-found --wait=true
kubectl delete pv actual-rootdisk --ignore-not-found --wait=true
sudo install -d -m 0700 -o root -g root /flash/enc/vms/kubevirt/actual/rootdisk
sudo find /flash/enc/vms/kubevirt/actual/rootdisk -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
flux resume kustomization vms -n flux-system
flux reconcile kustomization vms -n flux-system --with-source
```

Do not use this destructive cleanup after accepted application data has been
migrated unless you have a verified backup or snapshot.

Expected:

- The service responds over HTTP.
- No public hostname is required.
- No Traefik route has been cut over.

## Stop or restart the pilot

```bash
virtctl stop actual -n vms
virtctl start actual -n vms
```

The root disk is retained by `PersistentVolume/actual-rootdisk` with reclaim policy `Retain`.

## Rollback

The pilot does not disable the legacy Actual MicroVM by itself. To roll back:

1. Stop the KubeVirt VM.
1. Leave the `actual-rootdisk` PV/PVC/DataVolume in place for later inspection.
1. Start or re-enable the legacy Actual MicroVM from `nix-config` if it was stopped manually.
1. Do not add a public Traefik route for the KubeVirt VM until rollback has been tested.

## Manual intervention checklist

The following steps intentionally require operator action:

- Apply the `nix-config` host change on `blizzard`.
- Ensure Flux Git SSH auth is present in `flux-system` if the cluster is fresh.
- Push this `homelab-apps` branch/repo so Flux can reconcile it.
- Delete and recreate old `actual-rootdisk` pilot resources first if a previous revision used `kubevirt-local`.
- Start the VM with `virtctl start`; GitOps leaves it in manual-control mode.
- Add SSH keys or a cloud-init Secret later if interactive guest login is required. No SSH private material belongs in this repo.
- Add a Traefik route only after middleware parity and rollback are validated.

## Acceptance criteria

The pilot is accepted when:

- the local PV directory is created by NixOS or verified manually;
- Flux reconciles `storage`, `kubevirt-config`, `cdi-config`, and `vms`;
- `PersistentVolume/actual-rootdisk` and `PersistentVolumeClaim/actual-rootdisk` use `kubevirt-local-immediate`;
- `DataVolume/actual-rootdisk` imports successfully;
- `VirtualMachine/actual` starts manually and Flux does not revert it to stopped;
- qemu-guest-agent responds;
- `Service/actual` responds from an in-cluster probe in the `ingress` namespace;
- stopping and starting the VM preserves service state;
- the legacy MicroVM rollback path remains available.
