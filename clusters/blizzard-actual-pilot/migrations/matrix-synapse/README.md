# matrix-synapse migration groundwork

This slot is populated; its application runtime is **unimplemented**. The host
registration is disabled, both Flux layers are suspended, the VM is halted,
readiness always fails, and guest egress is denied.

Owned files: this directory and the corresponding disabled host registration.
Shared catalog/generator/layers remain coordinator-owned. See
[`../README.md`](../README.md) for the contract and checks. The public
[`app/plan.json`](app/plan.json) records exact source provenance, disk budgets,
legacy identities/shares and service-specific gates. It contains no credentials.

Implement the owned runtime, private credential delivery, real readiness,
storage ownership/restore and approved peer policy before asking for a live
preview. No guest application, legacy disk, external share or host device is
installed or attached here. Fresh blank disks mirror declared budgets, not
measured use. Backups remain deferred to the later rsync.net design.

Config changes are read on the next reviewed Git stop/start of the retained
root. The first-boot seed installs only the loader, stable NIC profile and units;
future loader/seed changes require an explicit retained-root upgrade design.
Native TCP port-forward cannot forward the WireGuard UDP port; any UDP test
transport and gateway/leak acceptance must be reviewed separately.
