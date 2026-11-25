"""Populate one reserved service slot with inert migration groundwork."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

MIGRATIONS = Path(__file__).resolve().parent
IDS = frozenset(
    {
        "bazarr",
        "cross-seed",
        "firefox",
        "gitea",
        "immich",
        "jellyfin",
        "matrix-synapse",
        "pocket-id",
        "prowlarr",
        "qbittorrent",
        "radarr",
        "readarr",
        "sabnzbd",
        "seerr",
        "sonarr",
        "wireguard",
    }
)
IMAGE = (
    "docker://quay.io/containerdisks/debian@sha256:"
    "90b09aed0bbca79adf4ebb2445b548332dee59a600de20038639747526a44ba7"
)
EXCLUSIONS = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
]
ANNOTATIONS = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _string(dumper, value):
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    )


Dumper.add_representer(str, _string)


def _yaml(*objects):
    return yaml.dump_all(objects, Dumper=Dumper, sort_keys=False)


def _object(api, kind, name, namespace=None, **fields):
    metadata = {"name": name, "annotations": dict(ANNOTATIONS)}
    if namespace:
        metadata["namespace"] = namespace
    return {"apiVersion": api, "kind": kind, "metadata": metadata, **fields}


def serial(service, disk):
    # Virtio serials truncate at 20 bytes. Include the full identity in the
    # digest instead of truncating long IDs such as matrix-synapse-preview.
    return "gw" + hashlib.sha256(f"{service}/{disk}".encode()).hexdigest()[:18]


def catalog():
    data = json.loads((MIGRATIONS / "catalog.json").read_text())
    if (
        type(data.get("schemaVersion")) is not int
        or data["schemaVersion"] != 1
        or data.get("rootImage") != IMAGE
    ):
        raise ValueError("unsupported catalog or unreviewed root image")
    services = data.get("services", [])
    if {s["id"] for s in services} != IDS or len(services) != len(IDS):
        raise ValueError("catalog must contain exactly the reviewed sixteen IDs")
    for service in services:
        if service["namespace"] != service["id"] + "-preview":
            raise ValueError("service namespace must be independent")
        for key in ("cpu", "memoryMiB", "port", "rootSizeMiB"):
            if type(service[key]) is not int or service[key] <= 0:
                raise ValueError(f"invalid catalog budget: {key}")
        if service["rootSizeMiB"] != 20480 or service["port"] > 65535:
            raise ValueError("unreviewed root size or primary port")
        if service["protocol"] not in ("TCP", "UDP"):
            raise ValueError("unsupported primary protocol")
        names = {"root", "cloudinit", "desired-config"}
        for volume in service["stateVolumes"]:
            name = volume["name"]
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", name) or name in names:
                raise ValueError("invalid or colliding state volume name")
            names.add(name)
            if type(volume["sizeMiB"]) is not int or volume["sizeMiB"] <= 0:
                raise ValueError("invalid state volume size")
    return data


def _kustomization(resources, **fields):
    return _yaml(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": resources,
            **fields,
        }
    )


def render(service_id):
    if service_id not in IDS:
        raise ValueError(f"service is outside the reviewed fleet: {service_id}")
    data = catalog()
    profile = next(s for s in data["services"] if s["id"] == service_id)
    namespace = profile["namespace"]
    labels = {"app.kubernetes.io/name": namespace}
    disks = [("root", profile["rootSizeMiB"])] + [
        (v["name"], v["sizeMiB"]) for v in profile["stateVolumes"]
    ]
    storage_class = namespace + "-local"
    volumes = []
    for disk, size in disks:
        name = f"{namespace}-{disk}"
        volumes += [
            _object(
                "v1",
                "PersistentVolume",
                name,
                spec={
                    "capacity": {"storage": f"{size}Mi"},
                    "volumeMode": "Filesystem",
                    "accessModes": ["ReadWriteOnce"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": storage_class,
                    "local": {
                        "path": f"/flash/enc/vms/kubevirt-pilot/{namespace}/{disk}"
                    },
                    "claimRef": {"namespace": namespace, "name": name},
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": ["blizzard"],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
            ),
            _object(
                "cdi.kubevirt.io/v1beta1",
                "DataVolume",
                name,
                namespace,
                spec={
                    "source": {
                        "registry": {"url": data["rootImage"], "pullMethod": "node"}
                    }
                    if disk == "root"
                    else {"blank": {}},
                    "pvc": {
                        "storageClassName": storage_class,
                        "volumeName": name,
                        "volumeMode": "Filesystem",
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": f"{size}Mi"}},
                    },
                },
            ),
        ]
    policy_api = "networking.k8s.io/v1"
    deny = _object(
        policy_api,
        "NetworkPolicy",
        "default-deny",
        namespace,
        spec={
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
        },
    )
    importer = _object(
        policy_api,
        "NetworkPolicy",
        "cdi-importer-egress",
        namespace,
        spec={
            "podSelector": {"matchLabels": {"cdi.kubevirt.io": "importer"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        },
                        {"ipBlock": {"cidr": "10.45.0.10/32"}},
                    ],
                    "ports": [
                        {"port": 53, "protocol": "UDP"},
                        {"port": 53, "protocol": "TCP"},
                    ],
                },
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": EXCLUSIONS}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                },
            ],
        },
    )
    primary = {"port": profile["port"], "protocol": profile["protocol"]}
    ingress = _object(
        policy_api,
        "NetworkPolicy",
        namespace + "-ingress",
        namespace,
        spec={
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "actual-pilot-probes"
                                }
                            }
                        },
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kubevirt"
                                }
                            },
                            "podSelector": {"matchLabels": {"kubevirt.io": "virt-api"}},
                        },
                    ],
                    "ports": [primary],
                }
            ],
        },
    )
    vm = _object(
        "kubevirt.io/v1",
        "VirtualMachine",
        namespace,
        namespace,
        spec={
            "runStrategy": "Halted",
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": "blizzard"},
                    "terminationGracePeriodSeconds": 120,
                    "domain": {
                        "cpu": {"cores": profile["cpu"]},
                        "memory": {"guest": f"{profile['memoryMiB']}Mi"},
                        "resources": {
                            "requests": {
                                "memory": f"{profile['memoryMiB']}Mi",
                                "cpu": "250m",
                            },
                            "limits": {
                                "memory": f"{profile['memoryMiB'] + 512}Mi",
                                "cpu": str(profile["cpu"]),
                            },
                        },
                        "devices": {
                            "disks": [
                                {
                                    "name": disk,
                                    "disk": {"bus": "virtio"},
                                    "serial": serial(service_id, disk),
                                }
                                for disk, _ in disks
                            ]
                            + [
                                {
                                    "name": "desired-config",
                                    "disk": {"bus": "virtio", "readonly": True},
                                    "serial": serial(service_id, "config"),
                                },
                                {
                                    "name": "cloudinit",
                                    "disk": {"bus": "virtio"},
                                    "serial": serial(service_id, "cloudinit"),
                                },
                            ],
                            "interfaces": [
                                {
                                    "name": "default",
                                    "masquerade": {},
                                    "ports": [{"name": "primary", **primary}],
                                }
                            ],
                        },
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {"name": disk, "dataVolume": {"name": f"{namespace}-{disk}"}}
                        for disk, _ in disks
                    ]
                    + [
                        {
                            "name": "desired-config",
                            "configMap": {
                                "name": namespace + "-desired",
                                "volumeLabel": "GROUNDCFG",
                            },
                        },
                        {
                            "name": "cloudinit",
                            "cloudInitNoCloud": {
                                "secretRef": {"name": namespace + "-cloudinit"}
                            },
                        },
                    ],
                    "readinessProbe": {
                        "exec": {"command": ["/usr/bin/false"]},
                        "initialDelaySeconds": 15,
                        "periodSeconds": 10,
                    },
                },
            },
        },
    )
    vm["metadata"]["labels"] = labels
    service = _object(
        "v1",
        "Service",
        namespace,
        namespace,
        spec={
            "type": "ClusterIP",
            "selector": labels,
            "ports": [{"name": "primary", **primary, "targetPort": profile["port"]}],
        },
    )
    templates = MIGRATIONS / "templates"
    guest_files = {
        "/etc/systemd/network/05-groundwork.network": (
            templates / "05-groundwork.network"
        ).read_text(),
        "/etc/cloud/cloud.cfg.d/99-groundwork-network.cfg": "network: {config: disabled}\n",
        "/usr/local/lib/kubevirt-groundwork/loader.py": (
            templates / "loader.py"
        ).read_text(),
        "/etc/systemd/system/kubevirt-groundwork.service": (
            templates / "kubevirt-groundwork.service"
        )
        .read_text()
        .replace("@SERVICE@", service_id),
        "/etc/systemd/system/run-kubevirt\\x2dgroundwork\\x2dconfig.mount": (
            templates / "config.mount"
        )
        .read_text()
        .replace("@SERIAL@", serial(service_id, "config")),
    }
    seed = {
        "disable_root": True,
        "ssh_pwauth": False,
        "users": ["default"],
        "write_files": [
            {"path": name, "permissions": "0644", "content": content}
            for name, content in guest_files.items()
        ],
        "runcmd": [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "systemd-networkd.service"],
            ["systemctl", "enable", "kubevirt-groundwork.service"],
            ["systemctl", "start", "--no-block", "kubevirt-groundwork.service"],
        ],
    }
    plan = {
        "schemaVersion": 1,
        "serviceId": service_id,
        "runtimeReady": False,
        "status": "unimplemented-groundwork",
        "namespace": namespace,
        "freshDisks": [disk for disk, _ in disks],
        "profile": profile,
        "gates": data["commonGates"] + profile["gates"],
        "storageNotes": data["storageNotes"],
    }
    files = {
        "storage/kustomization.yaml": _kustomization(
            [
                "namespace.yaml",
                "storageclass.yaml",
                "volumes.yaml",
                "networkpolicy.yaml",
            ]
        ),
        "storage/namespace.yaml": _yaml(_object("v1", "Namespace", namespace)),
        "storage/storageclass.yaml": _yaml(
            _object(
                "storage.k8s.io/v1",
                "StorageClass",
                storage_class,
                provisioner="kubernetes.io/no-provisioner",
                reclaimPolicy="Retain",
                volumeBindingMode="Immediate",
            )
        ),
        "storage/volumes.yaml": _yaml(*volumes),
        "storage/networkpolicy.yaml": _yaml(deny, importer),
        "app/kustomization.yaml": _kustomization(
            ["virtualmachine.yaml", "service.yaml", "networkpolicy.yaml"],
            configMapGenerator=[
                {
                    "name": namespace + "-desired",
                    "namespace": namespace,
                    "files": ["plan.json", "runtime.sh"],
                }
            ],
            secretGenerator=[
                {
                    "name": namespace + "-cloudinit",
                    "namespace": namespace,
                    "files": ["userdata=user-data.yaml"],
                }
            ],
            generatorOptions={
                "disableNameSuffixHash": True,
                "annotations": ANNOTATIONS,
            },
        ),
        "app/virtualmachine.yaml": _yaml(vm),
        "app/service.yaml": _yaml(service),
        "app/networkpolicy.yaml": _yaml(ingress),
        "app/plan.json": json.dumps(plan, indent=2) + "\n",
        "app/runtime.sh": (templates / "runtime.sh").read_text(),
        "app/loader.py": guest_files["/usr/local/lib/kubevirt-groundwork/loader.py"],
        "app/user-data.yaml": "#cloud-config\n" + _yaml(seed),
        "README.md": f"""# {service_id} migration groundwork

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
""",
    }
    return files


def _no_symlinks(path):
    if ".." in path.parts:
        raise ValueError("output must not use parent traversal")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise ValueError("output must not contain symlinks")


def scaffold(service_id, output=None):
    files = render(service_id)
    destination = Path(output) if output is not None else MIGRATIONS / service_id
    _no_symlinks(destination)
    allowed = {"storage/kustomization.yaml", "app/kustomization.yaml"}
    if destination.exists():
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise ValueError("reserved slot contains a symlink")
            if path.is_dir():
                if path.relative_to(destination).as_posix() not in {"storage", "app"}:
                    raise ValueError("slot is already populated")
                continue
            if path.relative_to(destination).as_posix() not in allowed:
                raise ValueError("slot is already populated")
            config = yaml.safe_load(path.read_text())
            if config != {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": [],
            }:
                raise ValueError("slot is already populated")
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=sorted(IDS))
    parser.add_argument(
        "--output", type=Path, help="fresh/reserved slot for disposable fixtures"
    )
    args = parser.parse_args()
    print(scaffold(args.service, args.output))


if __name__ == "__main__":
    main()
