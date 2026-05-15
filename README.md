# homelab-apps

GitOps repository for the blizzard k3s cluster. Managed by Flux.

## Structure

- `flux/` — Flux bootstrap manifests and Kustomizations
- `namespaces/` — Namespace definitions with Pod Security Standards
- `network/` — CNI (Cilium), MetalLB, NetworkPolicies
- `network/config/` — CRD-backed network resources applied after operators are ready
- `kubevirt/` — KubeVirt operator manifests
- `kubevirt/config/` — KubeVirt CR applied after the operator CRDs exist
- `cdi/` — Containerized Data Importer operator manifests
- `cdi/config/` — CDI CR applied after the operator CRDs exist
- `storage/` — Single-node local storage classes for KubeVirt pilot disks
- `sealed-secrets/` — Sealed Secrets controller
- `ingress/` — Traefik, cloudflared
- `vms/` — KubeVirt VM inventory and pilot VM manifests
- `docs/` — Deployment and operations runbooks

## Migration status

This repo is migrating all legacy `nix-config` MicroVM workloads to KubeVirt VMs.
The first applied VM is `vms/actual`, which uses `runStrategy: Manual` so Flux
can reconcile the manifest without auto-starting it or reverting an operator-run
`virtctl start`.

Public routes are not cut over by default. Add Traefik routes only after matching
the old NixOS Traefik security middleware and validating rollback.

## Runbooks

- [Actual pilot VM deployment](docs/actual-pilot-deployment.md)
