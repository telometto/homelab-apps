# homelab-apps

GitOps repository for the blizzard k3s cluster. Managed by Flux.

## Structure

- `flux/` — Flux bootstrap manifests and Kustomizations
- `namespaces/` — Namespace definitions with Pod Security Standards
- `network/` — CNI (Cilium), MetalLB, NetworkPolicies
- `kubevirt/` — KubeVirt operator, CR, RuntimeClasses
- `cdi/` — Containerized Data Importer operator and CR
- `sealed-secrets/` — Sealed Secrets controller
- `ingress/` — Traefik, cloudflared
- `secrets/` — SealedSecret manifests (encrypted, safe to commit)
