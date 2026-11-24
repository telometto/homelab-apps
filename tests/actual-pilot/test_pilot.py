"""Build real deployment artifacts and check data/network safety boundaries."""

import base64
import hashlib
import importlib.util
import ipaddress
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "clusters/blizzard-actual-pilot"


def build(path):
    return [
        d
        for d in yaml.safe_load_all(
            subprocess.check_output(["kustomize", "build", str(path)], text=True)
        )
        if d
    ]


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Inspect only our new tree before Kustomize can follow resource paths.
        for config in ROOT.rglob("kustomization.yaml"):
            for item in yaml.safe_load(config.read_text()).get("resources", []):
                if not (config.parent / item).resolve().is_relative_to(ROOT):
                    raise ValueError(f"resource escapes the pilot tree: {item}")
        cls.root = build(ROOT)
        cls.layers = [
            d
            for d in cls.root
            if d["kind"] == "Kustomization" and d["metadata"]["name"] != "actual-pilot"
        ]
        cls.resources = []
        for layer in cls.layers:
            path = (REPO / layer["spec"]["path"]).resolve()
            if not path.is_relative_to(ROOT):
                raise ValueError("Flux path escapes pilot")
            cls.resources.extend(build(path))

    def test_public_source_and_dependency_closure(self):
        source = next(d for d in self.root if d["kind"] == "GitRepository")
        self.assertEqual(
            source["spec"]["url"], "https://github.com/telometto/homelab-apps.git"
        )
        self.assertNotIn("secretRef", source["spec"])
        for layer in self.layers:
            for dependency in layer["spec"].get("dependsOn", []):
                self.assertIn(
                    dependency["name"], {d["metadata"]["name"] for d in self.layers}
                )

    def test_one_vm_with_retained_separate_disks_and_git_power(self):
        vms = [d for d in self.resources if d["kind"] == "VirtualMachine"]
        self.assertEqual(len(vms), 1)
        self.assertIn(vms[0]["spec"]["runStrategy"], ("Always", "Halted"))
        volumes = vms[0]["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(
            [v["dataVolume"]["name"] for v in volumes if "dataVolume" in v],
            ["actual-pilot-root", "actual-pilot-data"],
        )
        pvs = [d for d in self.resources if d["kind"] == "PersistentVolume"]
        self.assertEqual(len(pvs), 2)
        for pv in pvs:
            self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
            self.assertTrue(
                pv["spec"]["local"]["path"].startswith(
                    "/flash/enc/vms/kubevirt-pilot/actual/"
                )
            )
        layer = next(d for d in self.layers if d["metadata"]["name"] == "pilot-storage")
        self.assertFalse(layer["spec"]["prune"])
        dvs = [d for d in self.resources if d["kind"] == "DataVolume"]
        self.assertEqual(
            {d["spec"]["pvc"]["volumeName"] for d in dvs},
            {d["metadata"]["name"] for d in pvs},
        )
        root = next(d for d in dvs if d["metadata"]["name"] == "actual-pilot-root")
        self.assertIn("@sha256:", root["spec"]["source"]["registry"]["url"])

    def test_no_public_services_or_ingress_stack(self):
        for obj in self.resources:
            if obj["kind"] == "Service":
                self.assertEqual(obj["spec"].get("type", "ClusterIP"), "ClusterIP")
                self.assertNotIn("externalIPs", obj["spec"])
                self.assertTrue(all("nodePort" not in p for p in obj["spec"]["ports"]))
            self.assertNotIn(
                obj["kind"], {"Ingress", "IngressRoute", "IPAddressPool", "HelmRelease"}
            )
        service = next(d for d in build(ROOT / "actual") if d["kind"] == "Service")
        self.assertEqual(service["spec"]["ports"][0]["port"], 5006)

    def test_policies_deny_access_to_production_private_networks(self):
        policies = [
            d
            for d in self.resources
            if d["kind"] == "NetworkPolicy"
            and d["metadata"].get("namespace") == "actual-pilot"
        ]
        self.assertTrue(
            any(
                d["spec"]["podSelector"] == {}
                and set(d["spec"]["policyTypes"]) == {"Ingress", "Egress"}
                for d in policies
            )
        )
        policy = next(
            d for d in policies if d["metadata"]["name"] == "actual-pilot-egress"
        )
        internet = [
            peer["ipBlock"]
            for rule in policy["spec"]["egress"]
            for peer in rule.get("to", [])
            if peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
        ]
        self.assertEqual(len(internet), 1)
        for address in (
            "10.100.0.20",
            "192.168.2.100",
            "100.85.254.99",
            "169.254.169.254",
            "127.0.0.1",
        ):
            self.assertTrue(
                any(
                    ipaddress.ip_address(address) in ipaddress.ip_network(n)
                    for n in internet[0]["except"]
                )
            )

    def test_private_http_allows_probes_and_only_kubevirt_api_forwarders(self):
        ingress = [
            d
            for d in self.resources
            if d["kind"] == "NetworkPolicy"
            and d["metadata"].get("namespace") == "actual-pilot"
            and "Ingress" in d["spec"].get("policyTypes", [])
        ]
        self.assertCountEqual(
            [d["metadata"]["name"] for d in ingress],
            ["default-deny", "actual-pilot-ingress"],
        )
        policy = next(
            d for d in ingress if d["metadata"]["name"] == "actual-pilot-ingress"
        )["spec"]
        self.assertEqual(
            policy["podSelector"],
            {"matchLabels": {"app.kubernetes.io/name": "actual-pilot"}},
        )
        self.assertEqual(len(policy["ingress"]), 1)
        rule = policy["ingress"][0]
        self.assertEqual(rule["ports"], [{"port": 5006, "protocol": "TCP"}])
        # Namespace and pod selectors must share one peer: splitting them
        # into separate entries would allow every pod in the namespace.
        self.assertCountEqual(
            rule["from"],
            [
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
        )

    def test_cloud_init_is_nonsecret_provisioning_with_a_pinned_app(self):
        secrets = [d for d in self.resources if d["kind"] == "Secret"]
        self.assertEqual(len(secrets), 1)
        self.assertEqual(secrets[0]["metadata"]["name"], "actual-pilot-cloudinit")
        data = yaml.safe_load(base64.b64decode(secrets[0]["data"]["userdata"]))
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(data["disable_root"])
        self.assertNotIn("password", data)
        files = {f["path"]: f for f in data["write_files"]}
        self.assertIn("/usr/local/lib/actual-pilot/prepare_data_disk.py", files)
        self.assertIn(
            "@sha256:", files["/etc/systemd/system/actual-pilot.service"]["content"]
        )
        for name in ("bootstrap.sh", "prepare_data_disk.py"):
            self.assertEqual(
                files["/usr/local/lib/actual-pilot/" + name]["content"],
                (ROOT / "actual/guest" / name).read_text(),
            )

    def test_release_artifacts_match_recorded_hashes(self):
        for artifact in json.loads((ROOT / "pins.json").read_text())["assets"]:
            self.assertEqual(
                hashlib.sha256((ROOT / artifact["path"]).read_bytes()).hexdigest(),
                artifact["sha256"],
            )

    def test_custom_resources_validate_against_pinned_primary_schemas(self):
        schemas = {}
        for obj in self.resources:
            if obj["kind"] == "CustomResourceDefinition":
                for version in obj["spec"]["versions"]:
                    schemas[
                        obj["spec"]["group"] + "/" + version["name"],
                        obj["spec"]["names"]["kind"],
                    ] = version["schema"]["openAPIV3Schema"]
        for api, kind, filename in (
            ("kubevirt.io/v1", "VirtualMachine", "virtualmachine.json"),
            ("cdi.kubevirt.io/v1beta1", "DataVolume", "datavolume.json"),
        ):
            schemas[api, kind] = json.loads(
                (Path(__file__).parent / "schemas" / filename).read_text()
            )
        for obj in self.root + self.resources:
            key = obj["apiVersion"], obj["kind"]
            if key in schemas:
                with self.subTest(resource=key, name=obj["metadata"]["name"]):
                    jsonschema.Draft4Validator(schemas[key]).validate(obj)


class DiskSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "prepare_disk", ROOT / "actual/guest/prepare_data_disk.py"
        )
        cls.disk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.disk)

    def test_created_filesystem_is_recognized_after_guest_reboot(self):
        # A real ext4 round trip catches label truncation that a dict fixture misses.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh-pilot-image"
            with path.open("wb") as image:
                image.truncate(16 * 1024 * 1024)
            subprocess.run(
                ["mkfs.ext4", "-q", "-F", "-L", self.disk.LABEL, str(path)], check=True
            )
            output = subprocess.check_output(
                ["blkid", "-p", "-o", "export", str(path)], text=True
            )
            info = dict(
                line.split("=", 1) for line in output.splitlines() if "=" in line
            )
            info.pop("DEVNAME", None)
            self.assertEqual(self.disk.filesystem_action(info), "mount")

    def test_existing_pilot_filesystem_is_reused(self):
        self.assertEqual(
            self.disk.filesystem_action({"TYPE": "ext4", "LABEL": "actual-pilot"}),
            "mount",
        )

    def test_unknown_filesystem_partition_table_and_label_are_rejected(self):
        for info in (
            {"TYPE": "xfs"},
            {"TYPE": "ext4", "LABEL": "important"},
            {"PTTYPE": "gpt"},
            {"UUID": "unknown"},
        ):
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.disk.filesystem_action(info)

    def test_unknown_nonzero_data_is_not_blank(self):
        self.assertEqual(self.disk.filesystem_action({}), "format")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disk"
            path.write_bytes(bytes(4096))
            self.disk.verify_blank(path, 4096)
            path.write_bytes(bytes(2048) + b"existing-data" + bytes(2035))
            with self.assertRaises(ValueError):
                self.disk.verify_blank(path, 4096)

    def test_unexpected_size_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disk"
            path.write_bytes(bytes(4096))
            with self.assertRaises(ValueError):
                self.disk.verify_blank(path, 8192)


if __name__ == "__main__":
    unittest.main()
