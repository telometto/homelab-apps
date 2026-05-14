# Actual pilot VM deployment

This runbook deploys the first KubeVirt pilot VM: `actual` in the `vms` namespace. The pilot intentionally does not create a public Traefik route; it proves the operator stack, local PV binding, CDI image import, VM boot, qemu-guest-agent, and internal service reachability first.

## What this deploys

| Resource | Location | Purpose |
|---|---|---|
| `StorageClass/kubevirt-local-immediate` | `storage/storageclass-immediate.yaml` | Immediate-binding class for pre-imported local boot disks |
| `PersistentVolume/actual-rootdisk` | `vms/actual/pv.yaml` | Binds the VM boot disk to `/flash/enc/kubevirt/actual/rootdisk` on `blizzard` |
| `DataVolume/actual-rootdisk` | `vms/actual/datavolume.yaml` | Imports the Debian Stable cloud image into the root disk PVC |
| `VirtualMachine/actual` | `vms/actual/virtualmachine.yaml` | Manual-control KubeVirt VM; starts only when explicitly requested |
| `Service/actual` | `vms/actual/service.yaml` | ClusterIP service for the Actual HTTP port `11051` |
| `NetworkPolicy/actual-*` | `vms/actual/networkpolicy.yaml` | Allows Traefik ingress and temporary bootstrap egress for DNS/HTTP/HTTPS |

## Prerequisites

1. `blizzard` has the latest `nix-config` `dev-kubevirt` host config applied.
1. k3s is running with Cilium and Flux bootstrapped.
1. Flux can read this repository from `ssh://git@github.com/telometto/homelab-apps`.
1. `/flash/enc/kubevirt/actual/rootdisk` exists on `blizzard`.
1. KubeVirt and CDI are installed and available.
1. The `vms` namespace exists.

The NixOS host config should create the required local PV path with tmpfiles. If the path is missing after rebuild, create it before reconciling the VM resources:

```bash
sudo install -d -m 0700 -o root -g root /flash/enc/kubevirt/actual/rootdisk
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

## Start the pilot VM

Only start the VM after the DataVolume import succeeds. The VM manifest uses
`runStrategy: Manual`, so Flux can continue reconciling `./vms` without
drift-correcting the manual start back to a stopped state:

```bash
virtctl start actual -n vms
kubectl -n vms get vmi actual -w
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

Port-forward the internal ClusterIP service:

```bash
kubectl -n vms port-forward svc/actual 11051:11051
```

From another shell:

```bash
curl -I http://127.0.0.1:11051
```

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
- Start the VM with `virtctl start`; GitOps leaves it in manual-control mode.
- Add SSH keys or a cloud-init Secret later if interactive guest login is required. No SSH private material belongs in this repo.
- Add a Traefik route only after middleware parity and rollback are validated.

## Acceptance criteria

The pilot is accepted when:

- the local PV directory is created by NixOS or verified manually;
- Flux reconciles `storage`, `kubevirt-config`, `cdi-config`, and `vms`;
- `DataVolume/actual-rootdisk` imports successfully;
- `VirtualMachine/actual` starts manually and Flux does not revert it to stopped;
- qemu-guest-agent responds;
- `Service/actual` responds through `kubectl port-forward`;
- stopping and starting the VM preserves service state;
- the legacy MicroVM rollback path remains available.
