# Blizzard Actual pilot

This tree is self-contained. Flux must reconcile this directory, never the
legacy `flux/` entrypoint. It installs no Cilium, MetalLB, Traefik, or
Cloudflare resources. Source authentication uses public GitHub HTTPS.

The corresponding host configuration and operations runbook are in
[`nix-config/docs/actual-kubevirt-pilot.md`](https://github.com/telometto/nix-config/blob/main/docs/actual-kubevirt-pilot.md).
The private pilot passed Git stop/start, stopped-disk backup and a separate
restore boot/restart with a budget comparison on 2026-11-24. Host-reboot safety
and off-host backup require their own acceptance checks.

## Reconciliation order

1. Root creates namespaces and the source/child Kustomizations.
1. `pilot-flux` owns Flux components after initial host seeding.
1. Operator layers establish KubeVirt/CDI APIs.
1. Configuration layers wait for the corresponding Available condition.
1. Storage imports the digest-pinned Debian cloud disk and creates the blank
   data disk, waiting for DataVolume success.
1. Actual starts after storage and KubeVirt readiness. VM HTTP readiness and
   guest-agent checks are separate from operator availability.

`pins.json` records exact manifests/digests. Release YAML is vendored
unchanged; do not reformat it. Use a release update to replace it and update
its recorded hash together. The KubeVirt virt-handler patches coordinate
with the host kubelet root `/var/lib/kubelet-actual-pilot`; review them when
upgrading the operator. The canonical `/var/lib/kubelet/device-plugins`
socket directory is mounted separately. The digest-pinned root registry
import uses node pulling, which does not require a dynamic scratch PVC.

## VM management

- Commit `actual/virtualmachine.yaml` `runStrategy: Always` for normal running.
- Commit `runStrategy: Halted` for a durable stop.
- Use the matching host `actual-pilot-virtctl` wrapper for restart,
  guest-agent information, and console diagnostics.
- Keep the private loopback port-forward and SSH tunnel below running while
  using the browser UI.

The Flux health expression accepts a deliberate halt once the VM reports
`Stopped`; it continues waiting during `Stopping`.

The Actual Podman container uses host networking **inside the guest VM**.
There is no host-networked application Pod and no public Service.

For private browser access, run this on Blizzard:

```bash
sudo actual-pilot-virtctl -n actual-pilot port-forward \
  --address=127.0.0.1 vmi/actual-pilot 15006:5006
```

On the client computer, run:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:15006:127.0.0.1:15006 blizzard
```

Open `http://127.0.0.1:15006`. Use the VMI name in regular forwarding commands;
the launcher Pod address changes when the VM restarts.

`virtctl port-forward` connects to the VM through `virt-api`. The HTTP ingress
policy admits probe traffic and TCP 5006 from pods labelled
`kubevirt.io: virt-api` within the `kubevirt` namespace. Both selectors belong
to the same peer, so other pods in that namespace receive no HTTP allowance.

`actual/guest/user-data.yaml` is public provisioning, wrapped in a generated
Kubernetes Secret because KubeVirt accepts cloud-init through `secretRef`.
It contains no credentials. Existing secret manifests elsewhere in the
repository are not dependencies. Keep the embedded helper contents in sync
with `bootstrap.sh` and `prepare_data_disk.py`; the tests catch stale copies.
Guest logs/bootstrap completion and HTTP readiness expose first-boot
failures. A changed cloud-init payload needs a deliberate new root-disk
identity to take effect on an already initialized guest.

## Persistence and policy changes

The root/data disks are separate retained local PVs. No normal Flux prune or
VM stop/restart should erase them. Pruning is disabled for the pilot layers,
so removing a resource from Git leaves it live; a subsequent explicit
decommission or policy removal is needed. In particular, delete reviewed
obsolete NetworkPolicies when tightening access, since policy allowances
are additive.

Backup requires a stopped VM and independently stored copies of both disk
images. Restore into separate new PV/PVCs and a separately named VM. Do not
re-import over restored images. See the host runbook for the acceptance
sequence and preservation gates.
Give the restored VM its own Service and policy selectors; include the
`virt-api` ingress peer when checking browser access through `virtctl`.
If the cluster database is lost while images remain, the host startup guard
refuses automatic re-import. Preserve both images and recover their PVC
bindings deliberately; guest checks cannot protect an image from CDI.

## Verification

```bash
XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}/codex/actual-pilot-nix" \
  nix-build tests/actual-pilot --no-out-link
```

The public standalone check copies only this tree and its new tests into the
Nix sandbox. It builds each root, validates custom resources using pinned
primary schemas, verifies release hashes, and tests storage/network
boundaries, real ext4 label reuse, fail-closed disk handling, and the guest's
offline systemd dependency graph. Health expressions run against the exact
CEL-Go version used by the pinned Flux controller. It does not read the legacy
secret files or contact a Kubernetes API. A matching GitHub workflow runs
it for changes to the pilot.
