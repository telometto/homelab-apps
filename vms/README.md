# KubeVirt VMs

This directory contains KubeVirt VM manifests for the MicroVM migration.

## Current state

- `inventory-configmap.yaml` records the 24 legacy MicroVMs from `nix-config/vms/vm-registry.nix` for migration tracking.
- `flaresolverr` is tracked in the registry inventory but is not currently enabled as a standalone MicroVM; it runs embedded in the legacy `prowlarr` MicroVM and should only become a standalone KubeVirt VM after that split is deliberate.
- `actual/` is the first pilot VM. It has a standalone `DataVolume` using `kubevirt-local-immediate` so the Debian cloud image can import before the VM starts.
- The `actual` VM is intentionally `runStrategy: Manual` so Flux can reconcile the object without auto-starting it or reverting `virtctl start`.

## Pilot workflow

1. Apply the matching `nix-config` change on `blizzard` so tmpfiles creates `/flash/enc/kubevirt/actual/rootdisk`.
1. If an earlier pilot revision already created `actual-rootdisk` with `kubevirt-local`, follow the upgrade cleanup in `docs/actual-pilot-deployment.md` before reconciling this revision.
1. Let Flux reconcile `storage`, `kubevirt-config`, `cdi-config`, and `vms`.
1. Confirm `PersistentVolume/actual-rootdisk`, `PersistentVolumeClaim/actual-rootdisk`, and `DataVolume/actual-rootdisk` are bound/imported.
1. Start the VM only when ready:

   ```bash
   virtctl start actual -n vms
   ```

1. Validate the guest console, qemu-guest-agent, and service port.
1. Copy/migrate old Actual data before exposing a public route.
1. Add a Traefik route only after security middleware parity is confirmed.

## Security notes

The VM namespace uses privileged Pod Security enforcement because KubeVirt `virt-launcher` pods need KVM access. Security is provided by:

- VM isolation
- default-deny NetworkPolicies
- per-VM ingress policies
- in-guest firewalling
- Cloudflare Tunnel and Traefik middleware before public exposure

The pilot includes a temporary HTTP/HTTPS egress allow so CDI can import the Debian cloud image and the guest can install packages. Tighten egress before adding privacy-routed or high-risk services.
