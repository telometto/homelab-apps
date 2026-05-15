# Migration Log

## Phase 1: Cluster Bootstrap

- homelab-apps repo initialized
- Added explicit Kustomize entrypoints for Flux-managed directories
- Split operator installation from CR/config resources for Network, KubeVirt, and CDI
- Removed the gVisor RuntimeClass from the active KubeVirt path
- Added `kubevirt-local` and `kubevirt-local-immediate` single-node local storage classes
- Added `vms/actual` as a manual-control KubeVirt pilot VM using a Debian cloud image
- Split the `actual` boot image import into a standalone `DataVolume` on `kubevirt-local-immediate` so CDI can import before the VM is manually started
- Added `docs/actual-pilot-deployment.md` as the step-by-step pilot deployment runbook
- Documented the required delete/recreate cleanup when an earlier pilot revision already created immutable `actual-rootdisk` PV/PVC resources with `kubevirt-local`
- Corrected `flaresolverr` inventory metadata: it is not an enabled standalone MicroVM today and currently runs embedded in the legacy `prowlarr` MicroVM
