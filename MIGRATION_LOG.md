# Migration Log

## Phase 1: Cluster Bootstrap

- homelab-apps repo initialized
- Added explicit Kustomize entrypoints for Flux-managed directories
- Split operator installation from CR/config resources for Network, KubeVirt, and CDI
- Removed the gVisor RuntimeClass from the active KubeVirt path
- Added `kubevirt-local` single-node local storage class
- Added `vms/actual` as a halted KubeVirt pilot VM using a Debian cloud image
- Split the `actual` boot image import into a standalone `DataVolume` so CDI can import before the VM is manually started
- Added `docs/actual-pilot-deployment.md` as the step-by-step pilot deployment runbook
