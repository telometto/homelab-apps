# Pinned custom API schemas

These generated schemas validate the new pilot without contacting a cluster.
They are validation inputs, not additional CRDs to deploy.

- `virtualmachine.json`: the `virtualmachine` entry in `CRDsValidation`,
  `pkg/virt-operator/resource/generate/components/validations_generated.go`
  at [KubeVirt v1.9.1](https://github.com/kubevirt/kubevirt/blob/v1.9.1/pkg/virt-operator/resource/generate/components/validations_generated.go).
- `datavolume.json`: the v1beta1 OpenAPI schema of the `datavolume` entry in
  `CDICRDs`, `pkg/operator/resources/crds_generated.go` at
  [CDI v1.66.1](https://github.com/kubevirt/containerized-data-importer/blob/v1.66.1/pkg/operator/resources/crds_generated.go).

The test also extracts Flux, KubeVirt operator, and CDI operator CR schemas
from the exact release manifests in the new cluster tree. `pins.json`
records and tests the original release-manifest SHA256 hashes.
