"""Exercise all reserved migrations without starting a VM or touching host data."""

import base64
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
import yaml

from test_pilot import ROOT, build

MIGRATIONS = ROOT / "migrations"
IDS = {
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
IMAGE = (
    "docker://quay.io/containerdisks/debian@sha256:"
    "90b09aed0bbca79adf4ebb2445b548332dee59a600de20038639747526a44ba7"
)
PUBLIC_EXCLUSIONS = {
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
}


def module(name, path):
    if not path.is_file():
        raise AssertionError(f"missing implementation: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def service_objects(objects, kind):
    return [o for o in objects if o["kind"] == kind]


def check_contract(test, profile, storage, app):
    """Independently inspect rendered API objects, including negative mutations."""
    namespace = profile["id"] + "-preview"
    disks = {"root": 20480} | {v["name"]: v["sizeMiB"] for v in profile["stateVolumes"]}
    test.assertEqual(
        {o["kind"] for o in storage},
        {
            "Namespace",
            "StorageClass",
            "PersistentVolume",
            "DataVolume",
            "NetworkPolicy",
        },
    )
    test.assertEqual(
        {o["kind"] for o in app},
        {"VirtualMachine", "Service", "NetworkPolicy", "ConfigMap", "Secret"},
    )
    test.assertEqual(
        service_objects(storage, "Namespace")[0]["metadata"]["name"], namespace
    )
    for obj in app + storage:
        if obj["kind"] not in {"Namespace", "StorageClass", "PersistentVolume"}:
            test.assertEqual(obj["metadata"]["namespace"], namespace)
    storage_class = service_objects(storage, "StorageClass")[0]
    test.assertEqual(storage_class["provisioner"], "kubernetes.io/no-provisioner")
    test.assertEqual(storage_class["reclaimPolicy"], "Retain")
    test.assertEqual(storage_class["volumeBindingMode"], "Immediate")
    pvs = {
        p["metadata"]["name"]: p for p in service_objects(storage, "PersistentVolume")
    }
    dvs = {d["metadata"]["name"]: d for d in service_objects(storage, "DataVolume")}
    test.assertEqual(set(pvs), {f"{namespace}-{disk}" for disk in disks})
    test.assertEqual(set(dvs), set(pvs))
    for disk, size in disks.items():
        name = f"{namespace}-{disk}"
        pv, dv = pvs[name]["spec"], dvs[name]["spec"]
        test.assertEqual(
            pv["local"]["path"], f"/flash/enc/vms/kubevirt-pilot/{namespace}/{disk}"
        )
        test.assertEqual(pv["capacity"], {"storage": f"{size}Mi"})
        test.assertEqual(pv["claimRef"], {"namespace": namespace, "name": name})
        test.assertEqual(pv["persistentVolumeReclaimPolicy"], "Retain")
        test.assertEqual(pv["accessModes"], ["ReadWriteOnce"])
        test.assertEqual(dv["pvc"]["volumeName"], name)
        test.assertEqual(
            dv["pvc"]["storageClassName"], storage_class["metadata"]["name"]
        )
        test.assertEqual(dv["pvc"]["resources"]["requests"]["storage"], f"{size}Mi")
        test.assertEqual(dv["pvc"]["accessModes"], ["ReadWriteOnce"])
        test.assertEqual(
            dv["source"],
            {"registry": {"url": IMAGE, "pullMethod": "node"}}
            if disk == "root"
            else {"blank": {}},
        )
    machines = service_objects(app, "VirtualMachine")
    test.assertEqual(len(machines), 1)
    vm = machines[0]
    test.assertEqual(vm["spec"]["runStrategy"], "Halted")
    test.assertNotIn("dataVolumeTemplates", vm["spec"])
    guest = vm["spec"]["template"]["spec"]
    test.assertEqual(guest["readinessProbe"]["exec"], {"command": ["/usr/bin/false"]})
    test.assertNotIn("httpGet", guest["readinessProbe"])
    test.assertEqual(guest["domain"]["cpu"]["cores"], profile["cpu"])
    test.assertEqual(guest["domain"]["memory"]["guest"], f"{profile['memoryMiB']}Mi")
    test.assertEqual(guest["networks"], [{"name": "default", "pod": {}}])
    test.assertEqual(
        guest["domain"]["devices"]["interfaces"],
        [
            {
                "name": "default",
                "masquerade": {},
                "ports": [
                    {
                        "name": "primary",
                        "port": profile["port"],
                        "protocol": profile["protocol"],
                    }
                ],
            }
        ],
    )
    test.assertNotIn("hostDevices", guest["domain"]["devices"])
    test.assertNotIn("filesystems", guest["domain"]["devices"])
    test.assertEqual(
        {
            v["name"]: v["dataVolume"]["name"]
            for v in guest["volumes"]
            if "dataVolume" in v
        },
        {disk: f"{namespace}-{disk}" for disk in disks},
    )
    test.assertEqual(len(guest["volumes"]), len(disks) + 2)
    test.assertTrue(
        all(
            set(v)
            in (
                {"name", "dataVolume"},
                {"name", "configMap"},
                {"name", "cloudInitNoCloud"},
            )
            for v in guest["volumes"]
        )
    )
    devices = guest["domain"]["devices"]["disks"]
    serials = [d["serial"] for d in devices]
    test.assertEqual(len(set(serials)), len(serials))
    test.assertTrue(all(0 < len(s) <= 20 for s in serials))
    config_disk = next(d for d in devices if d["name"] == "desired-config")
    test.assertIs(config_disk["disk"]["readonly"], True)
    config_volume = next(v for v in guest["volumes"] if v["name"] == "desired-config")
    test.assertEqual(config_volume["configMap"]["name"], namespace + "-desired")
    configs = service_objects(app, "ConfigMap")
    test.assertEqual(len(configs), 1)
    config = configs[0]["data"]
    plan = json.loads(config["plan.json"])
    test.assertEqual(plan["serviceId"], profile["id"])
    test.assertIs(plan["runtimeReady"], False)
    test.assertEqual(plan["freshDisks"], list(disks))
    test.assertIn("unimplemented", config["runtime.sh"])
    test.assertIn("exit 78", config["runtime.sh"])
    seed = service_objects(app, "Secret")[0]
    data = yaml.safe_load(base64.b64decode(seed["data"]["userdata"]))
    test.assertIs(data["ssh_pwauth"], False)
    test.assertIs(data["disable_root"], True)
    test.assertNotIn("packages", data)
    test.assertNotIn("mounts", data)
    files = {f["path"]: f["content"] for f in data["write_files"]}
    test.assertEqual(
        files["/usr/local/lib/kubevirt-groundwork/loader.py"],
        (MIGRATIONS / "templates/loader.py").read_text(),
    )
    test.assertIn("Name=enp1s0", files["/etc/systemd/network/05-groundwork.network"])
    test.assertIn("DHCP=yes", files["/etc/systemd/network/05-groundwork.network"])
    runtime_unit = files["/etc/systemd/system/kubevirt-groundwork.service"]
    test.assertIn(f" --service {profile['id']}", runtime_unit)
    test.assertIn("WantedBy=multi-user.target", runtime_unit)
    mount = next(c for p, c in files.items() if p.endswith(".mount"))
    test.assertIn("Options=ro,nodev,nosuid,noexec", mount)
    test.assertIn("virtio-" + config_disk["serial"], mount)
    service = service_objects(app, "Service")[0]["spec"]
    test.assertEqual(set(service), {"type", "selector", "ports"})
    test.assertEqual(service["type"], "ClusterIP")
    test.assertEqual(
        service["ports"],
        [
            {
                "name": "primary",
                "port": profile["port"],
                "targetPort": profile["port"],
                "protocol": profile["protocol"],
            }
        ],
    )
    policies = {
        p["metadata"]["name"]: p["spec"]
        for p in service_objects(storage + app, "NetworkPolicy")
    }
    test.assertEqual(
        set(policies), {"default-deny", "cdi-importer-egress", namespace + "-ingress"}
    )
    test.assertEqual(
        policies["default-deny"],
        {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    )
    importer = policies["cdi-importer-egress"]
    test.assertEqual(
        importer["podSelector"], {"matchLabels": {"cdi.kubevirt.io": "importer"}}
    )
    test.assertEqual(importer["policyTypes"], ["Egress"])
    test.assertEqual(len(importer["egress"]), 2)
    dns, https = importer["egress"]
    test.assertEqual(
        dns,
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                },
                {"ipBlock": {"cidr": "10.45.0.10/32"}},
            ],
            "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
        },
    )
    test.assertEqual(https["ports"], [{"port": 443, "protocol": "TCP"}])
    test.assertEqual(len(https["to"]), 1)
    test.assertEqual(https["to"][0]["ipBlock"]["cidr"], "0.0.0.0/0")
    test.assertEqual(set(https["to"][0]["ipBlock"]["except"]), PUBLIC_EXCLUSIONS)
    ingress = policies[namespace + "-ingress"]
    test.assertEqual(
        ingress["podSelector"], {"matchLabels": {"app.kubernetes.io/name": namespace}}
    )
    test.assertEqual(guest["domain"]["devices"].get("hostDevices", []), [])
    test.assertNotIn("cdi.kubevirt.io", vm["spec"]["template"]["metadata"]["labels"])
    test.assertEqual(ingress["policyTypes"], ["Ingress"])
    test.assertNotIn("egress", ingress)
    test.assertEqual(
        ingress["ingress"],
        [
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
                            "matchLabels": {"kubernetes.io/metadata.name": "kubevirt"}
                        },
                        "podSelector": {"matchLabels": {"kubevirt.io": "virt-api"}},
                    },
                ],
                "ports": [{"port": profile["port"], "protocol": profile["protocol"]}],
            }
        ],
    )


class GroundworkLoaderTests(unittest.TestCase):
    def loader(self):
        return module("groundwork_loader", MIGRATIONS / "templates/loader.py")

    def config(self, root, **overrides):
        config = root / "config"
        config.mkdir(exist_ok=True)
        (config / "plan.json").write_text(
            json.dumps({"serviceId": "bazarr", "runtimeReady": True} | overrides)
        )
        (config / "runtime.sh").write_text("printf 'fixture executed\\n'\n")
        return config

    def test_only_matching_boolean_true_can_execute_current_boot_script(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            for content in ("printf first", "printf next-boot", "printf first"):
                (config / "runtime.sh").write_text(content)
                with mock.patch.object(loader.subprocess, "run") as run:
                    loader.execute(config, "bazarr")
                run.assert_called_once_with(
                    ["/bin/sh", "-eu"], input=content, text=True, check=True
                )

    def test_false_wrong_identity_missing_and_nonboolean_gates_never_execute(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            for value in (False, "true", "false", 1, 0, None, [], {}):
                config = self.config(Path(directory), runtimeReady=value)
                with (
                    self.subTest(value=value),
                    mock.patch.object(loader.subprocess, "run") as run,
                ):
                    with self.assertRaises((ValueError, RuntimeError)):
                        loader.execute(config, "bazarr")
                    run.assert_not_called()
            config = self.config(Path(directory), serviceId="radarr")
            with mock.patch.object(loader.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    loader.execute(config, "bazarr")
                run.assert_not_called()
            for plan in (
                {"serviceId": "bazarr"},
                [],
                {"runtimeReady": True},
                {"serviceId": "bazarr", "runtimeReady": True, "schemaVersion": "wrong"},
            ):
                (config / "plan.json").write_text(json.dumps(plan))
                with mock.patch.object(loader.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        loader.execute(config, "bazarr")
                    run.assert_not_called()

    def test_symlink_directory_plan_and_runtime_cannot_execute(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            alias = root / "alias"
            alias.symlink_to(config, target_is_directory=True)
            for unsafe in (alias, alias / "nested"):
                with (
                    self.subTest(unsafe=unsafe),
                    mock.patch.object(loader.subprocess, "run") as run,
                ):
                    with self.assertRaises(ValueError):
                        loader.execute(unsafe, "bazarr")
                    run.assert_not_called()
            for filename in ("plan.json", "runtime.sh"):
                target = root / filename
                original = (config / filename).read_text()
                (config / filename).rename(target)
                (config / filename).symlink_to(target)
                with (
                    self.subTest(filename=filename),
                    mock.patch.object(loader.subprocess, "run") as run,
                ):
                    with self.assertRaises(ValueError):
                        loader.execute(config, "bazarr")
                    run.assert_not_called()
                (config / filename).unlink()
                (config / filename).write_text(original)

    def test_duplicate_json_keys_and_nonregular_files_fail_closed(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            (config / "plan.json").write_text(
                '{"serviceId":"bazarr","runtimeReady":false,"runtimeReady":true}'
            )
            with mock.patch.object(loader.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    loader.execute(config, "bazarr")
                run.assert_not_called()
            self.config(Path(directory))
            (config / "runtime.sh").unlink()
            (config / "runtime.sh").mkdir()
            with self.assertRaises(ValueError):
                loader.execute(config, "bazarr")

    def test_schema_version_is_a_strict_integer_before_execution(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            for value in (True, 1.0, "1", 2):
                config = self.config(Path(directory), schemaVersion=value)
                with (
                    self.subTest(version=value),
                    mock.patch.object(loader.subprocess, "run") as run,
                ):
                    with self.assertRaises(ValueError):
                        loader.execute(config, "bazarr")
                    run.assert_not_called()

    def test_shipped_runtime_refuses_even_if_plan_is_manually_enabled(self):
        loader = self.loader()
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            (config / "runtime.sh").write_text(
                (MIGRATIONS / "templates/runtime.sh").read_text()
            )
            with self.assertRaises(subprocess.CalledProcessError) as result:
                loader.execute(config, "bazarr")
            self.assertEqual(result.exception.returncode, 78)


class GroundworkManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = module("groundwork_scaffold", MIGRATIONS / "scaffold.py")
        cls.catalog = json.loads((MIGRATIONS / "catalog.json").read_text())
        cls.profiles = {s["id"]: s for s in cls.catalog["services"]}
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.fixture = Path(cls.scratch.name) / "pilot"
        shutil.copytree(ROOT, cls.fixture)
        cls.generated = {}
        for service in cls.profiles:
            path = cls.fixture / "migrations" / service
            # Already populated service branches must also pass the real contract.
            if not yaml.safe_load((path / "app/kustomization.yaml").read_text()).get(
                "resources"
            ):
                cls.generator.scaffold(service, output=path)
            cls.generated[service] = build(path / "storage"), build(path / "app")

    def test_catalog_contains_exactly_the_reviewed_fleet(self):
        self.assertEqual(set(self.profiles), IDS)
        self.assertEqual(self.catalog["schemaVersion"], 1)
        self.assertEqual(self.catalog["rootImage"], IMAGE)
        self.assertEqual(self.profiles["seerr"]["legacyVm"], "overseerr")
        for profile in self.profiles.values():
            self.assertEqual(profile["namespace"], profile["id"] + "-preview")
            self.assertTrue(profile["sources"])
            self.assertTrue(profile["gates"])
        # This is the byte-for-byte shared host/app catalog receipt.
        self.assertEqual(
            hashlib.sha256((MIGRATIONS / "catalog.json").read_bytes()).hexdigest(),
            "8885f6c96fb46f1e7a2f30ad54cdea2cd3be8e2ce7f6b66fe3c493f7c357616e",
        )

    def test_reservations_build_and_have_one_suspended_pair_per_service(self):
        layers = {
            d["metadata"]["name"]: d
            for d in build(ROOT)
            if d["kind"] == "Kustomization"
        }
        registration = list(
            yaml.safe_load_all((ROOT / "migration-layers.yaml").read_text())
        )
        self.assertEqual(len(registration), 32)
        for service in self.profiles:
            for part in ("storage", "app"):
                path = MIGRATIONS / service / part
                actual = build(path)
                if actual:
                    check_contract(
                        self,
                        self.profiles[service],
                        build(MIGRATIONS / service / "storage"),
                        build(MIGRATIONS / service / "app"),
                    )
                layer = layers[f"pilot-{service}-{part}"]
                self.assertIs(layer["spec"]["suspend"], True)
                self.assertIs(layer["spec"]["prune"], False)
                self.assertEqual(
                    layer["spec"]["path"],
                    f"./clusters/blizzard-actual-pilot/migrations/{service}/{part}",
                )
                for dep in layer["spec"]["dependsOn"]:
                    self.assertIn(dep["name"], layers)

    def test_all_profiles_render_real_inert_private_retained_resources(self):
        for service, (storage, app) in self.generated.items():
            with self.subTest(service=service):
                check_contract(self, self.profiles[service], storage, app)

    def test_generated_service_deltas_coexist_with_disjoint_paths_and_serials(self):
        paths, serials = [], []
        identities = []
        for storage, app in self.generated.values():
            paths += [
                p["spec"]["local"]["path"]
                for p in service_objects(storage, "PersistentVolume")
            ]
            vm = service_objects(app, "VirtualMachine")[0]
            serials += [
                d["serial"]
                for d in vm["spec"]["template"]["spec"]["domain"]["devices"]["disks"]
            ]
            identities += [
                (
                    o["apiVersion"],
                    o["kind"],
                    o["metadata"].get("namespace"),
                    o["metadata"]["name"],
                )
                for o in storage + app
            ]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(serials), len(set(serials)))
        self.assertEqual(len(identities), len(set(identities)))
        layers = [
            d
            for d in build(self.fixture)
            if d["kind"] == "Kustomization" and d["metadata"]["name"] != "actual-pilot"
        ]
        fleet = [
            o
            for layer in layers
            for o in build(
                self.fixture
                / Path(layer["spec"]["path"]).relative_to(
                    "clusters/blizzard-actual-pilot"
                )
            )
            if o["kind"] == "VirtualMachine"
        ]
        # The common base has no optional Searx implementation. Its own
        # service branch registers it; require it exactly when that layer exists.
        optional = (
            ["searx-preview"]
            if any(layer["metadata"]["name"] == "pilot-searx" for layer in layers)
            else []
        )
        self.assertCountEqual(
            [v["metadata"]["namespace"] for v in fleet],
            ["actual-pilot"] + optional + [s + "-preview" for s in IDS],
        )

    def test_generated_custom_resources_and_flux_layers_match_pinned_schemas(self):
        schemas = {
            kind: json.loads((Path(__file__).parent / "schemas" / filename).read_text())
            for kind, filename in (
                ("VirtualMachine", "virtualmachine.json"),
                ("DataVolume", "datavolume.json"),
            )
        }
        flux = next(
            o
            for o in build(ROOT / "flux-system")
            if o["kind"] == "CustomResourceDefinition"
            and o["spec"]["names"]["kind"] == "Kustomization"
        )
        schemas["Kustomization"] = next(
            v["schema"]["openAPIV3Schema"]
            for v in flux["spec"]["versions"]
            if v["name"] == "v1"
        )
        objects = [
            o for pair in self.generated.values() for part in pair for o in part
        ] + list(yaml.safe_load_all((ROOT / "migration-layers.yaml").read_text()))
        for obj in objects:
            if obj["kind"] in schemas:
                with self.subTest(kind=obj["kind"], name=obj["metadata"]["name"]):
                    jsonschema.Draft4Validator(schemas[obj["kind"]]).validate(obj)

    def test_migration_health_expressions_run_in_the_flux_cel_engine(self):
        cases = []
        for layer in yaml.safe_load_all((ROOT / "migration-layers.yaml").read_text()):
            health = layer["spec"]["healthCheckExprs"][0]
            if health["kind"] == "DataVolume":
                fixtures = [
                    ({}, False, False),
                    ({"phase": "Succeeded"}, True, False),
                    ({"phase": "Failed"}, False, True),
                    ({"phase": "ImportInProgress"}, False, False),
                ]
                spec = {}
            else:
                # A halted VM is reconciled, never application-ready.
                fixtures = [
                    ({}, False, False),
                    ({"printableStatus": "Stopped", "ready": False}, True, False),
                    ({"printableStatus": "Stopping", "ready": True}, False, False),
                    (
                        {"conditions": [{"type": "Failure", "status": "True"}]},
                        False,
                        True,
                    ),
                ]
                spec = {"runStrategy": "Halted"}
            for status, current, failed in fixtures:
                for key, expected in (("current", current), ("failed", failed)):
                    cases.append(
                        {
                            "name": layer["metadata"]["name"]
                            + "/"
                            + key
                            + "/"
                            + str(status),
                            "expression": health[key],
                            "input": {"status": status, "spec": spec},
                            "expected": expected,
                        }
                    )
        result = subprocess.run(
            ["actual-pilot-cel-check"],
            input=json.dumps(cases),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guest_units_form_a_per_boot_graph_without_cloud_init_cycle(self):
        from test_recovery import verify_guest_units

        seed = service_objects(self.generated["matrix-synapse"][1], "Secret")[0]
        files = {
            f["path"]: f["content"]
            for f in yaml.safe_load(base64.b64decode(seed["data"]["userdata"]))[
                "write_files"
            ]
        }
        units = [
            p.rsplit("/", 1)[1] for p in files if p.startswith("/etc/systemd/system/")
        ]
        verify_guest_units(
            files,
            ["kubevirt-groundwork.service"]
            + [n for n in units if n != "kubevirt-groundwork.service"],
        )

    def test_unsafe_manifest_mutations_are_detected(self):
        profile = self.profiles["matrix-synapse"]
        original_storage, original_app = self.generated["matrix-synapse"]

        def objects(storage, app, kind):
            return service_objects(storage + app, kind)

        mutations = {
            "public service": lambda s, a: objects(s, a, "Service")[0]["spec"].update(
                type="NodePort"
            ),
            "guest egress": lambda s, a: objects(s, a, "NetworkPolicy")[-1][
                "spec"
            ].update(egress=[{}]),
            "production path": lambda s, a: objects(s, a, "PersistentVolume")[0][
                "spec"
            ]["local"].update(path="/flash/enc/vms/production"),
            "aliased path": lambda s, a: objects(s, a, "PersistentVolume")[0]["spec"][
                "local"
            ].update(
                path="/flash/enc/vms/kubevirt-pilot/matrix-synapse-preview/../gitea-preview/root"
            ),
            "unpinned import": lambda s, a: next(
                d
                for d in objects(s, a, "DataVolume")
                if "registry" in d["spec"]["source"]
            )["spec"]["source"]["registry"].update(
                url="docker://quay.io/containerdisks/debian:latest"
            ),
            "serial collision": lambda s, a: objects(s, a, "VirtualMachine")[0]["spec"][
                "template"
            ]["spec"]["domain"]["devices"]["disks"][0].update(
                serial=objects(s, a, "VirtualMachine")[0]["spec"]["template"]["spec"][
                    "domain"
                ]["devices"]["disks"][1]["serial"]
            ),
            "enabled VM": lambda s, a: objects(s, a, "VirtualMachine")[0][
                "spec"
            ].update(runStrategy="Always"),
            "lying readiness": lambda s, a: objects(s, a, "VirtualMachine")[0]["spec"][
                "template"
            ]["spec"]["readinessProbe"].update(exec={"command": ["/usr/bin/true"]}),
            "string ready": lambda s, a: objects(s, a, "ConfigMap")[0]["data"].update(
                {
                    "plan.json": json.dumps(
                        {"serviceId": "matrix-synapse", "runtimeReady": "false"}
                    )
                }
            ),
            "broad importer": lambda s, a: next(
                p
                for p in objects(s, a, "NetworkPolicy")
                if p["metadata"]["name"] == "cdi-importer-egress"
            )["spec"].update(podSelector={}),
            "split API peer": lambda s, a: objects(s, a, "NetworkPolicy")[-1]["spec"][
                "ingress"
            ][0]["from"].extend(
                [{"podSelector": {"matchLabels": {"kubevirt.io": "virt-api"}}}]
            ),
            "host data": lambda s, a: objects(s, a, "VirtualMachine")[0]["spec"][
                "template"
            ]["spec"]["volumes"].append(
                {
                    "name": "production",
                    "hostDisk": {"path": "/flash/enc/production.img", "type": "Disk"},
                }
            ),
        }
        for label, mutate in mutations.items():
            storage, app = copy.deepcopy((original_storage, original_app))
            mutate(storage, app)
            with self.subTest(mutation=label), self.assertRaises(AssertionError):
                check_contract(self, profile, storage, app)

    def test_generator_rejects_unknown_profiles_and_protects_populated_or_symlink_slots(
        self,
    ):
        with self.assertRaises(ValueError):
            self.generator.render("tautulli")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = root / "slot"
            self.generator.scaffold("bazarr", output=slot)
            before = {
                str(p.relative_to(slot)): p.read_bytes()
                for p in slot.rglob("*")
                if p.is_file()
            }
            with self.assertRaises(ValueError):
                self.generator.scaffold("bazarr", output=slot)
            self.assertEqual(
                before,
                {
                    str(p.relative_to(slot)): p.read_bytes()
                    for p in slot.rglob("*")
                    if p.is_file()
                },
            )
            alias = root / "alias"
            alias.symlink_to(root / "target", target_is_directory=True)
            with self.assertRaises(ValueError):
                self.generator.scaffold("bazarr", output=alias / "slot")

    def test_cli_changes_only_the_selected_service_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "migrations"
            root.mkdir()
            for name in ("scaffold.py", "catalog.json"):
                shutil.copyfile(MIGRATIONS / name, root / name)
            shutil.copytree(MIGRATIONS / "templates", root / "templates")
            before = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            subprocess.run(
                [sys.executable, str(root / "scaffold.py"), "gitea"],
                check=True,
                capture_output=True,
            )
            after = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            self.assertTrue(
                all(name.startswith("gitea/") for name in after.keys() - before.keys())
            )
            self.assertEqual(before, {name: after[name] for name in before})
            self.assertEqual(
                {
                    p.relative_to(root / "gitea").as_posix()
                    for p in (root / "gitea").rglob("*")
                    if p.is_file()
                },
                set(self.generator.render("gitea")),
            )

    def test_unsafe_catalog_profiles_are_rejected_before_rendering(self):
        mutations = {
            "boolean schema": lambda c: c.update(schemaVersion=True),
            "float schema": lambda c: c.update(schemaVersion=1.0),
            "unpinned image": lambda c: c.update(
                rootImage="docker://quay.io/containerdisks/debian:latest"
            ),
            "shared namespace": lambda c: c["services"][0].update(
                namespace="actual-pilot"
            ),
            "boolean budget": lambda c: c["services"][0].update(cpu=True),
            "invalid port": lambda c: c["services"][0].update(port=65536),
            "wrong protocol": lambda c: c["services"][0].update(protocol="SCTP"),
            "parent path": lambda c: c["services"][0]["stateVolumes"][0].update(
                name="../root"
            ),
            "root collision": lambda c: c["services"][0]["stateVolumes"][0].update(
                name="root"
            ),
            "unknown service": lambda c: c["services"][0].update(id="tautulli"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mutate in mutations.items():
                catalog = copy.deepcopy(self.catalog)
                mutate(catalog)
                (root / "catalog.json").write_text(json.dumps(catalog))
                with (
                    self.subTest(mutation=name),
                    mock.patch.object(self.generator, "MIGRATIONS", root),
                ):
                    with self.assertRaises(ValueError):
                        self.generator.catalog()


if __name__ == "__main__":
    unittest.main()
