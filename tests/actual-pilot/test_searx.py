"""The staged SearxNG preview must be inert, private and updateable on one root."""

import base64
import importlib.util
import ipaddress
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from test_pilot import ROOT, build

PREVIEW = ROOT / "searx-preview"
IMAGE_A = "ghcr.io/searxng/searxng@sha256:" + "a" * 64
IMAGE_B = "ghcr.io/searxng/searxng@sha256:" + "b" * 64


class SearxManifestTests(unittest.TestCase):
    def resources(self, part):
        self.assertTrue((PREVIEW / part / "kustomization.yaml").is_file())
        return build(PREVIEW / part)

    def test_publishing_leaves_storage_and_application_suspended(self):
        layers = {
            d["metadata"]["name"]: d
            for d in build(ROOT)
            if d["kind"] == "Kustomization"
        }
        for name in ("pilot-searx-storage", "pilot-searx"):
            self.assertIn(name, layers)
            self.assertIs(layers[name]["spec"]["suspend"], True)
            self.assertIs(layers[name]["spec"]["prune"], False)
        self.assertIn(
            {"name": "pilot-searx-storage"}, layers["pilot-searx"]["spec"]["dependsOn"]
        )
        # Exercise the same real CEL engine cases used for Actual power states.
        self.assertEqual(
            layers["pilot-searx"]["spec"]["healthCheckExprs"],
            layers["pilot-actual"]["spec"]["healthCheckExprs"],
        )

    def test_root_is_separate_retained_prebound_and_imported_without_scratch(self):
        objects = self.resources("storage")
        pv = next(d for d in objects if d["kind"] == "PersistentVolume")
        dv = next(d for d in objects if d["kind"] == "DataVolume")
        self.assertEqual(
            pv["spec"]["local"]["path"],
            "/flash/enc/vms/kubevirt-pilot/searx-preview/root",
        )
        self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(pv["spec"]["capacity"]["storage"], "20Gi")
        self.assertEqual(dv["spec"]["pvc"]["volumeName"], pv["metadata"]["name"])
        self.assertEqual(
            pv["spec"]["claimRef"],
            {"namespace": "searx-preview", "name": dv["metadata"]["name"]},
        )
        self.assertEqual(dv["spec"]["source"]["registry"]["pullMethod"], "node")
        self.assertIn("@sha256:", dv["spec"]["source"]["registry"]["url"])

    def test_vm_uses_config_iso_and_exposes_only_private_http(self):
        objects = self.resources("app")
        vm = next(d for d in objects if d["kind"] == "VirtualMachine")
        template = vm["spec"]["template"]["spec"]
        self.assertEqual(template["domain"]["cpu"]["cores"], 1)
        self.assertEqual(template["domain"]["memory"]["guest"], "2Gi")
        self.assertEqual(
            template["domain"]["devices"]["interfaces"][0]["ports"],
            [{"name": "http", "port": 8080, "protocol": "TCP"}],
        )
        volume = next(v for v in template["volumes"] if v["name"] == "desired-config")
        self.assertEqual(volume["configMap"]["name"], "searx-preview-desired")
        for disk in template["domain"]["devices"]["disks"]:
            self.assertLessEqual(len(disk.get("serial", "")), 20)
        service = next(d for d in objects if d["kind"] == "Service")
        self.assertEqual(service["spec"].get("type", "ClusterIP"), "ClusterIP")
        self.assertEqual(
            service["spec"]["ports"],
            [{"name": "http", "port": 8080, "targetPort": 8080}],
        )

    def test_private_forwarder_peer_and_egress_exclusions(self):
        objects = self.resources("app") + self.resources("storage")
        policies = {
            d["metadata"]["name"]: d["spec"]
            for d in objects
            if d["kind"] == "NetworkPolicy"
        }
        self.assertEqual(policies["default-deny"]["podSelector"], {})
        rule = policies["searx-preview-ingress"]["ingress"][0]
        self.assertEqual(rule["ports"], [{"port": 8080, "protocol": "TCP"}])
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
        public = policies["searx-preview-egress"]["egress"][1]
        self.assertCountEqual(
            public["ports"],
            [{"port": 80, "protocol": "TCP"}, {"port": 443, "protocol": "TCP"}],
        )
        exclusions = public["to"][0]["ipBlock"]["except"]
        for address in (
            "10.100.0.12",
            "192.168.2.100",
            "172.16.1.1",
            "100.85.254.99",
            "169.254.169.254",
            "127.0.0.1",
        ):
            self.assertTrue(
                any(
                    ipaddress.ip_address(address) in ipaddress.ip_network(n)
                    for n in exclusions
                )
            )

    def test_cloud_init_and_desired_config_keep_identity_guest_local(self):
        objects = self.resources("app")
        config = next(d for d in objects if d["kind"] == "ConfigMap")
        image = json.loads(config["data"]["app.json"])["image"]
        self.assertRegex(image, r"^ghcr\.io/searxng/searxng@sha256:[0-9a-f]{64}$")
        settings = yaml.safe_load(config["data"]["settings.yml"])
        self.assertNotIn("secret_key", settings["server"])
        self.assertTrue(settings["server"]["limiter"])
        self.assertTrue(settings["server"]["image_proxy"])
        self.assertEqual(settings["server"]["method"], "POST")
        self.assertFalse(settings["server"]["public_instance"])
        self.assertEqual(settings["valkey"]["url"], "valkey://127.0.0.1:6379/0")
        self.assertNotIn("enabled_plugins", settings)
        for plugin in (
            "tracker_url_remover",
            "hostnames",
            "oa_doi_rewrite",
            "calculator",
            "hash_plugin",
            "tor_check",
            "unit_converter",
        ):
            self.assertTrue(
                settings["plugins"][f"searx.plugins.{plugin}.SXNGPlugin"]["active"]
            )
        seed = next(d for d in objects if d["kind"] == "Secret")
        data = yaml.safe_load(base64.b64decode(seed["data"]["userdata"]))
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(data["disable_root"])
        files = {f["path"]: f["content"] for f in data["write_files"]}
        for name in ("bootstrap.sh", "runner.py"):
            self.assertEqual(
                files[f"/usr/local/lib/searx-preview/{name}"],
                (PREVIEW / "guest" / name).read_text(),
            )
        self.assertIn(
            "Name=enp1s0", files["/etc/systemd/network/05-searx-preview.network"]
        )
        self.assertIn(
            "Options=ro,nodev,nosuid,noexec",
            files["/etc/systemd/system/run-searx\\x2dpreview\\x2dconfig.mount"],
        )
        self.assertIn("bind 127.0.0.1", files["/etc/searx-preview/valkey.conf"])
        self.assertNotIn(
            "After=searx-preview-bootstrap.service",
            files["/etc/systemd/system/searx-preview.service"],
        )

    def test_enabled_guest_services_have_no_startup_cycle(self):
        from test_recovery import verify_guest_units

        self.assertTrue((PREVIEW / "app/user-data.yaml").is_file())
        files = {
            f["path"]: f["content"]
            for f in yaml.safe_load((PREVIEW / "app/user-data.yaml").read_text())[
                "write_files"
            ]
        }
        names = [p.name for p in (PREVIEW / "guest/units").iterdir()]
        verify_guest_units(
            files,
            ["searx-preview.service"]
            + [n for n in names if n != "searx-preview.service"],
        )

    def test_machine_readiness_uses_health_endpoint_and_cache_is_gated(self):
        objects = self.resources("app")
        vm = next(d for d in objects if d["kind"] == "VirtualMachine")
        # The pinned application's limiter rejects bootstrap's curl at /.
        self.assertEqual(
            vm["spec"]["template"]["spec"]["readinessProbe"]["httpGet"]["path"],
            "/healthz",
        )
        self.assertIn(
            "http://127.0.0.1:8080/healthz",
            (PREVIEW / "guest/bootstrap.sh").read_text(),
        )
        unit = (PREVIEW / "guest/units/searx-preview.service").read_text()
        setup = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]
        self.assertTrue(setup[0].endswith("runner.py prepare"))
        self.assertTrue(setup[-1].endswith("runner.py wait-cache"))

    def test_seed_matches_canonical_files_and_reference_namespaces_have_two_vms(self):
        path = PREVIEW / "guest/render.py"
        self.assertTrue(path.is_file())
        spec = importlib.util.spec_from_file_location("render_searx", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual((PREVIEW / "app/user-data.yaml").read_text(), module.render())
        layers = [
            d
            for d in build(ROOT)
            if d["kind"] == "Kustomization" and d["metadata"]["name"] != "actual-pilot"
        ]
        vms = [
            r
            for layer in layers
            for r in build(ROOT.parents[1] / layer["spec"]["path"])
            if r["kind"] == "VirtualMachine"
            and r["metadata"]["namespace"] in {"actual-pilot", "searx-preview"}
        ]
        self.assertCountEqual(
            [(v["metadata"]["namespace"], v["metadata"]["name"]) for v in vms],
            [("actual-pilot", "actual-pilot"), ("searx-preview", "searx-preview")],
        )


class SearxRunnerTests(unittest.TestCase):
    def runner(self):
        path = PREVIEW / "guest/runner.py"
        self.assertTrue(path.is_file(), "per-boot runner is missing")
        spec = importlib.util.spec_from_file_location("searx_runner", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def config(self, root, image=IMAGE_A, title="Preview A"):
        path = root / "config-iso"
        path.mkdir(exist_ok=True)
        (path / "app.json").write_text(json.dumps({"image": image}))
        (path / "settings.yml").write_text(f"general:\n  instance_name: {title}\n")
        (path / "limiter.toml").write_text(
            "[botdetection.ip_limit]\nlink_token = true\n"
        )
        return path

    def test_next_boot_reads_config_changes_and_reversal_preserving_identity(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "retained-state"
            config = self.config(root)
            first_identity = None
            for image, title in (
                (IMAGE_A, "Preview A"),
                (IMAGE_B, "Preview B"),
                (IMAGE_A, "Preview A"),
            ):
                self.config(root, image, title)
                actual = runner.prepare(config, state, uid=os.getuid(), gid=os.getgid())
                self.assertEqual(actual, image)
                identity = (state / "secret.env").read_bytes()
                first_identity = first_identity or identity
                self.assertEqual(identity, first_identity)
                self.assertEqual((state / "secret.env").stat().st_mode & 0o777, 0o600)
                self.assertIn(title, (state / "config/settings.yml").read_text())
                command = runner.run_command(image, state)
                self.assertEqual(command[-1], image)
                self.assertIn("--env-file=" + str(state / "secret.env"), command)
                self.assertNotIn(identity.decode().strip(), " ".join(command))
                self.assertIn("--user=977:977", command)
                self.assertIn("--cap-drop=all", command)
                self.assertIn("--replace", command)

    def test_malformed_image_and_symlink_identity_fail_without_overwrite(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "retained-state"
            config = self.config(root, "ghcr.io/searxng/searxng:latest")
            with self.assertRaises(ValueError):
                runner.prepare(config, state, uid=os.getuid(), gid=os.getgid())
            self.assertFalse(state.exists())
            self.config(root)
            state.mkdir()
            outside = root / "unchanged-fixture"
            outside.write_text("fixture only")
            (state / "secret.env").symlink_to(outside)
            with self.assertRaises(ValueError):
                runner.prepare(config, state, uid=os.getuid(), gid=os.getgid())
            self.assertEqual(outside.read_text(), "fixture only")

    def test_cached_image_starts_offline_and_missing_image_uses_plain_pull(self):
        runner = self.runner()
        for cached, online, succeeds in (
            (True, False, True),
            (False, True, True),
            (False, False, False),
        ):
            with (
                self.subTest(cached=cached, online=online),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory)
                image = path / "cached-image"
                if cached:
                    image.touch()
                podman = path / "podman"
                podman.write_text(f"""#!/bin/sh
case "$1" in
  image) [ "$#" -eq 3 ] && [ "$2" = exists ] && test -f '{image}' ;;
  pull) [ "$#" -eq 2 ] && [ '{int(online)}' = 1 ] && touch '{image}' ;;
  *) exit 125 ;;
esac
""")
                podman.chmod(0o755)
                if succeeds:
                    runner.ensure_image(IMAGE_A, podman=str(podman))
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        runner.ensure_image(IMAGE_A, podman=str(podman))
                self.assertEqual(image.exists(), succeeds)

    def test_cache_ping_waits_for_readiness_and_fails_closed(self):
        runner = self.runner()
        self.assertTrue(
            hasattr(runner, "wait_cache"),
            "cache readiness must gate application startup",
        )
        for ready_after, response, succeeds in (
            (3, "PONG", True),
            (99, "PONG", False),
            (1, "wrong reply", False),
        ):
            with (
                self.subTest(ready_after=ready_after, response=response),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory)
                counter = path / "attempts"
                counter.write_text("0")
                cli = path / "valkey-cli"
                cli.write_text(f"""#!/bin/sh
count=$(cat '{counter}')
count=$((count + 1))
printf '%s' "$count" > '{counter}'
[ "$count" -ge {ready_after} ] || exit 1
printf '%s\\n' '{response}'
""")
                cli.chmod(0o755)
                if succeeds:
                    runner.wait_cache(cli=str(cli), attempts=3, delay=0)
                else:
                    with self.assertRaisesRegex(RuntimeError, "cache"):
                        runner.wait_cache(cli=str(cli), attempts=3, delay=0)
                self.assertEqual(counter.read_text(), "3")


if __name__ == "__main__":
    unittest.main()
