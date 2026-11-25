# Independent service migration groundwork

The sixteen service directories are reservation slots on the foundation branch.
Their empty Kustomizations build successfully. Each pair is registered in
[`../migration-layers.yaml`](../migration-layers.yaml) with `suspend: true` and
`prune: false`; registering an empty slot is not an application implementation.
Actual remains the accepted reference on the common base. SearxNG and every
other service implementation remain on independent service branches; no optional
service is inherited merely by choosing this common base.

[`catalog.json`](catalog.json) is the byte-identical public catalog shared with
`nix-config/lib/kubevirt-groundwork/catalog.json`. It records declaration
provenance, CPU/memory, primary port/protocol, fresh state-volume budgets, legacy
state/shares and outstanding gates. Legacy paths are planning metadata only.
Catalog fields do not establish live service status, measured disk use or
completed runtime support. Seerr preserves legacy `overseerr` state identity;
Tautulli and disabled registry entries are outside this batch.

The common umbrella is #14, head `dev/kubevirt-groundwork`, targeting `main`.
All application service PRs, including SearxNG #13, target that common head.
The host counterpart is #5894: service PRs target its actual head `dev-kubevirt`;
canonical `dev/kubevirt` stays synchronized. The umbrellas remain unmerged to
main, and service selection is independent.

From a service branch based on `dev/kubevirt-groundwork`, populate its owned slot:

```sh
python3 clusters/blizzard-actual-pilot/migrations/scaffold.py bazarr
nix-build tests/actual-pilot --no-out-link
```

The generator refuses unknown services, symlinks and populated slots. It writes
only `migrations/SERVICE`; no shared registration edit is needed. The optional
`--output FRESH_SLOT` is for disposable fixture directories. The foundation tests
render all sixteen profiles into temporary fixtures, build every storage/app
directory, and verify the complete fleet can coexist. On a service branch they
also validate the actual populated slot. They check the pinned KubeVirt/CDI/Flux
schemas, real Flux CEL health expressions, systemd boot graph, storage/identity
separation, gate refusal and negative policy/resource mutations.

Every populated slot contains real resources, still deliberately inert:

- A namespace, static Retain storage class, prebound Retain local PVs and CDI
  DataVolumes under `/flash/enc/vms/kubevirt-pilot/SERVICE-preview/DISK`. The 20 GiB
  root uses the already pinned Debian registry image with CDI node pulling. All
  catalog state volumes are fresh and blank. They do not import legacy images,
  mount external shares, expose production paths or attach host devices.
- A halted VM matching declared CPU/memory. Its exec readiness probe runs
  `/usr/bin/false`. A reconciled halted VM is stopped; its Flux health result
  must not be interpreted as a running or application-ready guest.
- Default-deny ingress/egress, an importer-only DNS/public HTTPS exception, and
  private primary-port ingress from the labeled probe namespace or the
  `kubevirt` namespace **and** `virt-api` pod selector in one peer. Guest egress
  remains denied. WireGuard retains UDP; TCP port-forward does not forward it.
- A read-only public configuration ISO carrying `app/plan.json` and the owned
  `app/runtime.sh`. The plan ships `runtimeReady: false`. The shell script itself
  exits 78 as unimplemented even if someone changes that flag.

The cloud-init seed installs only a stable named-NIC DHCP profile, a Python
loader and systemd units. It installs no application or packages, formats or
mounts no state disk, and reads no credentials. The service unit invokes the
loader on every boot of the retained root. It reads the configuration ISO again,
rejects symlinked directories/files, malformed/duplicate JSON, mismatched service
identity and any gate other than boolean `true`, and then passes the already
read script bytes to `sh`. An executable script cannot bypass the gate by being
on the `noexec` ISO. All virtio serials are unique short hashes, at most 20 bytes.
The source loader is copied into each owned `app/` directory and embedded in
its seed; Kustomize never reads files outside that slot.

A future service agent owns its directory and matching disabled host module.
The shared catalog, generator, templates, registrations and production modules
remain coordinator-owned. Replace the owned runtime/plan only after designing
native startup, exact storage ownership/restore, private credential delivery,
real service readiness and narrowly reviewed peers. Keep `runtimeReady` false
until those gates are satisfied. Seed/loader changes on an existing retained
root need an explicit upgrade design; editing first-boot cloud-init alone does
not update it. Runtime/plan changes are consumed on a later reviewed Git
stop/start; the current halted, suspended source does not authorize that step.

Live Flux/CDI reconciliation, VM launch, persistence, identity preservation,
VPN leak/return-route behavior, media ACLs, GPU access and application workflows
remain unverified. Backups remain deferred to the later rsync.net design.
Do not merge, activate, apply, resume Flux, restart production services, access
private state/secrets or retire a MicroVM under the groundwork authorization.
