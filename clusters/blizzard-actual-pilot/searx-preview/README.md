# Private SearxNG migration preview

Both `pilot-searx-storage` and `pilot-searx` are **suspended**. Publishing these
files starts no importer or VM. Host disk preparation is a separate, disabled
Nix declaration on `dev/kubevirt-searxng` in telometto/nix-config, stacked on
`dev/kubevirt-base`. See that repository's `docs/searx-kubevirt-preview.md` for
the reviewed rollout gates. Production Searx and its public route are unchanged.

## Storage and lifecycle

- Namespace / VM: `searx-preview`; one core and 2 GiB guest memory.
- Retained root PV/PVC/DataVolume: `searx-preview-root`, 20 GiB, separate local
  directory `/flash/enc/vms/kubevirt-pilot/searx-preview/root` on Blizzard.
- Same immutable Debian base and CDI node import as Actual; no scratch PVC.
- Storage owns default-deny/importer policies and can import with the application
  still suspended. The app layer depends on storage and KubeVirt.
- ClusterIP TCP 8080 only. No Ingress, NodePort, public route or exposed cache.
- A new signing key is generated inside this guest once, with mode 0600 on the
  retained root. No production key/data is copied. Valkey listens on guest
  loopback only; cache data is disposable and bounded.

After a separately approved host deployment, resume storage in Git, wait for
DataVolume `Succeeded`, then resume the app in a second Git change. Verify VM,
guest agent, HTTP and Flux readiness before testing search and preferences.

Private desktop access after deployment:

```bash
ssh -t -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18080:127.0.0.1:18080 zeno@192.168.2.100 \
  'sudo actual-pilot-virtctl -n searx-preview port-forward --address=127.0.0.1 vmi/searx-preview 18080:8080'
```

Ingress permits the probe namespace and only pods labelled `kubevirt.io=virt-api`
in namespace `kubevirt`. DNS and public TCP 80/443 are allowed for package/image
fetches, searches and image proxy requests. Private networks, tailnet ranges,
loopback and metadata addresses remain excluded. No private application APIs are
opened to this preview.

## Desired configuration

`app/app.json` pins the official SearxNG image; `app/settings.yml` and
`app/limiter.toml` are public application settings. Kustomize packages them into
`searx-preview-desired`, which KubeVirt attaches as a read-only configuration ISO.
The runner reads this disk on every start and copies only public settings to the
retained root, preserving the locally generated signing key. The app container
runs as UID/GID 977 with dropped capabilities, a read-only root and writable
preview cache only. Host networking is inside the guest VM.

A ConfigMap edit alone does not update a running VMI's disk. Apply the desired
Git revision, commit `runStrategy: Halted`, wait for Stopped/Flux Ready, then
commit `runStrategy: Always`. Verify a visible setting change and reversal with
the same root/PVC/identity metadata. Exercise a separately tested pinned image
update/reversal before accepting application management. Cached starts use no
registry request; first installation and a missing image require network access.
The bootstrap/runner and Debian OS are installed on first boot; cloud-init edits
do not update an already initialized root.

The pinned image supports the existing engine preferences/weights. Old plugin
names map to supported `searx.plugins.*.SXNGPlugin` classes. Hostname replace and
Hostnames map to its single Hostnames plugin. The preview retains rate limiting,
image proxying, POST search, and the private-instance flag. Its limiter uses
`botdetection` keys and trusts no caller-supplied IP headers; no public reverse
proxy is introduced.

Machine readiness uses `/healthz`: the limiter intentionally rejects curl
User-Agents on ordinary pages. Before starting the container on every
boot, a bounded loopback Valkey PING gate prevents SearxNG's one-shot limiter
initialization from silently starting without a ready cache. Live acceptance
checks browser/search behavior and active bot rejection separately from health.

After changing files in `guest/`, regenerate the public seed:

```bash
nix-shell tests/actual-pilot --run 'python clusters/blizzard-actual-pilot/searx-preview/guest/render.py'
nix-build tests/actual-pilot --no-out-link
```

The standalone suite builds every layer, including suspended ones, checks pinned
KubeVirt/CDI schemas and real Flux CEL expressions, verifies boot dependencies,
and exercises cached/missing image behavior and retained identity across config
changes. Runtime Flux/CDI/NetworkPolicy and application acceptance still require
an authorized Blizzard deployment. The image/source provenance is in `pins.json`.

## Stop and rollback

Commit `runStrategy: Halted`, wait for Stopped, then suspend the app layer.
Suspension alone does not stop a running VM. Keep its PV/PVC/namespace and host
directory: prune is false and the PV uses Retain. Roll back image/settings through
Git and a controlled stop/start. Keep production Searx as rollback throughout.
No production cutover, backup setup or automatic image update is included.
