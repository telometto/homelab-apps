"""Exercise restart, lifecycle, and exact upstream integration boundaries."""

import configparser
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2] / "clusters/blizzard-actual-pilot"


def payload_files():
    return {
        f["path"]: f["content"]
        for f in yaml.safe_load((ROOT / "actual/guest/user-data.yaml").read_text())[
            "write_files"
        ]
    }


class RecoveryTests(unittest.TestCase):
    def test_guest_network_profile_survives_launcher_mac_changes(self):
        files = payload_files()
        path = "/etc/systemd/network/05-actual-pilot.network"
        self.assertIn(path, files)
        profile = configparser.ConfigParser()
        profile.optionxform = str
        profile.read_string(files[path])
        # A retained root must configure the same PCI interface when a new
        # launcher supplies a different MAC. Do not inherit a MAC-bound match
        # from cloud-init's first-boot fallback network declaration.
        self.assertEqual(dict(profile["Match"]), {"Name": "enp1s0"})
        self.assertTrue(profile.getboolean("Network", "DHCP"))
        self.assertTrue(profile.getboolean("DHCPv4", "UseMTU"))

    def test_static_image_import_needs_no_unprovisioned_scratch_volume(self):
        root = next(
            d
            for d in yaml.safe_load_all((ROOT / "storage/root.yaml").read_text())
            if d["kind"] == "DataVolume"
        )
        # CDI 1.66.1 exempts node registry pulls from mandatory scratch space.
        self.assertEqual(root["spec"]["source"]["registry"]["pullMethod"], "node")

    def test_flux_health_in_its_cel_engine_including_git_power_states(self):
        cases = []
        layers = list(yaml.safe_load_all((ROOT / "layers.yaml").read_text()))
        for layer in layers:
            for health in layer["spec"].get("healthCheckExprs", []):
                kind = health["kind"]
                if kind in ("KubeVirt", "CDI"):
                    fixtures = [
                        ({}, False, False),
                        ({"conditions": []}, False, False),
                        (
                            {"conditions": [{"type": "Available", "status": "True"}]},
                            True,
                            False,
                        ),
                        (
                            {"conditions": [{"type": "Degraded", "status": "True"}]},
                            False,
                            True,
                        ),
                    ]
                elif kind == "DataVolume":
                    fixtures = [
                        ({}, False, False),
                        ({"phase": "Succeeded"}, True, False),
                        ({"phase": "ImportInProgress"}, False, False),
                        ({"phase": "Failed"}, False, True),
                    ]
                else:
                    continue
                for status, current, failed in fixtures:
                    for key, expected in (("current", current), ("failed", failed)):
                        cases.append(
                            {
                                "name": f"{kind}/{key}/{status}",
                                "expression": health[key],
                                "input": {"status": status},
                                "expected": expected,
                            }
                        )
        health = next(d for d in layers if d["metadata"]["name"] == "pilot-actual")[
            "spec"
        ]["healthCheckExprs"][0]
        for strategy, status, expected in (
            ("Always", {}, False),
            ("Always", {"ready": True}, True),
            ("Always", {"ready": False}, False),
            ("Halted", {"ready": False, "printableStatus": "Stopped"}, True),
            ("Halted", {"ready": True, "printableStatus": "Stopping"}, False),
            ("Always", {"ready": False, "printableStatus": "Stopped"}, False),
        ):
            cases.append(
                {
                    "name": f"VM/{strategy}/{status}",
                    "expression": health["current"],
                    "input": {"spec": {"runStrategy": strategy}, "status": status},
                    "expected": expected,
                }
            )
        for status, failed in (
            ({}, False),
            ({"conditions": [{"type": "Failure", "status": "True"}]}, True),
        ):
            cases.append(
                {
                    "name": f"VM/failed/{status}",
                    "expression": health["failed"],
                    "input": {"status": status},
                    "expected": failed,
                }
            )
        result = subprocess.run(
            ["actual-pilot-cel-check"],
            input=json.dumps(cases),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_handler_sees_canonical_device_plugin_and_isolated_pods(self):
        kubevirt = yaml.safe_load((ROOT / "kubevirt/config/kubevirt.yaml").read_text())
        fixture = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "virt-handler", "namespace": "kubevirt"},
            "spec": {
                "selector": {"matchLabels": {"name": "virt-handler"}},
                "template": {
                    "metadata": {"labels": {"name": "virt-handler"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "virt-handler",
                                "image": "unused:v1.9.1",
                                "args": ["--port", "8443"],
                                "volumeMounts": [
                                    {
                                        "name": "kubelet",
                                        "mountPath": "/var/lib/kubelet",
                                    },
                                    {"name": "kubelet-pods", "mountPath": "/pods"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "kubelet",
                                "hostPath": {"path": "/var/lib/kubelet"},
                            },
                            {
                                "name": "kubelet-pods",
                                "hostPath": {"path": "/var/lib/kubelet/pods"},
                            },
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "handler.yaml").write_text(yaml.safe_dump(fixture))
            patches = []
            for patch in kubevirt["spec"]["customizeComponents"]["patches"]:
                data = json.loads(patch["patch"])
                if patch["type"] == "strategic":
                    data.update(
                        {
                            "apiVersion": "apps/v1",
                            "kind": "DaemonSet",
                            "metadata": {
                                "name": "virt-handler",
                                "namespace": "kubevirt",
                            },
                        }
                    )
                    contents = yaml.safe_dump(data)
                else:
                    contents = json.dumps(data)
                patches.append(
                    {
                        "target": {"kind": "DaemonSet", "name": "virt-handler"},
                        "patch": contents,
                    }
                )
            (path / "kustomization.yaml").write_text(
                yaml.safe_dump({"resources": ["handler.yaml"], "patches": patches})
            )
            handler = yaml.safe_load(
                subprocess.check_output(["kustomize", "build", str(path)], text=True)
            )["spec"]["template"]["spec"]
        mounts = handler["containers"][0]["volumeMounts"]
        volumes = {v["name"]: v["hostPath"]["path"] for v in handler["volumes"]}

        def resolve(container_path):
            mount = max(
                (
                    m
                    for m in mounts
                    if container_path == m["mountPath"]
                    or container_path.startswith(m["mountPath"] + "/")
                ),
                key=lambda m: len(m["mountPath"]),
            )
            return volumes[mount["name"]] + container_path.removeprefix(
                mount["mountPath"]
            )

        self.assertEqual(
            resolve("/var/lib/kubelet/device-plugins/kubelet.sock"),
            "/var/lib/kubelet/device-plugins/kubelet.sock",
        )
        self.assertEqual(
            resolve("/var/lib/kubelet-actual-pilot/pods/id/socket"),
            "/var/lib/kubelet-actual-pilot/pods/id/socket",
        )
        self.assertEqual(
            resolve("/pods/id/socket"), "/var/lib/kubelet-actual-pilot/pods/id/socket"
        )

    def test_enabled_guest_service_has_no_boot_order_cycle(self):
        files = payload_files()
        binary = Path(shutil.which("systemd-analyze")).resolve()
        system_units = binary.parent.parent / "example/systemd/system"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            units = path / "etc/systemd/system"
            units.mkdir(parents=True)
            for unit in system_units.iterdir():
                if unit.is_file():
                    (units / unit.name).write_text(unit.read_text())
            (path / "run/systemd").mkdir(parents=True)
            (path / "bin").mkdir()
            (path / "bin/true").touch(mode=0o755)
            for name in (
                "actual-pilot.service",
                "actual-pilot-prepare-data.service",
                "srv-actual.mount",
            ):
                (units / name).write_text(files["/etc/systemd/system/" + name])
            for unit in units.iterdir():
                # Only replace executable paths absent from the test host. Keep
                # every real boot dependency/default to check the actual graph.
                content = (
                    "\n".join(
                        line.split("=", 1)[0] + "=/bin/true"
                        if line.startswith("Exec")
                        else line
                        for line in unit.read_text().splitlines()
                    )
                    + "\n"
                )
                unit.write_text(content)
            environment = {**os.environ, "SYSTEMD_UNIT_PATH": "/etc/systemd/system"}
            if redirect := os.environ.get("ACTUAL_PILOT_TEST_LIBREDIRECT"):
                # The offline manager still initializes /run/systemd even with
                # --root. Keep that metadata inside scratch in Nix's sandbox.
                environment.update(
                    LD_PRELOAD=redirect, NIX_REDIRECTS=f"/run={path}/run"
                )
            result = subprocess.run(
                [
                    str(binary),
                    "--system",
                    f"--root={path}",
                    "--generators=no",
                    "--man=no",
                    "verify",
                    "actual-pilot.service",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("ordering cycle", result.stderr.lower())

    def test_image_preparation_handles_cache_and_registry_with_debian_podman(self):
        unit = payload_files()["/etc/systemd/system/actual-pilot.service"]
        command = shlex.split(
            next(
                line.split("=", 1)[1]
                for line in unit.splitlines()
                if line.startswith("ExecStartPre=")
            )
        )
        for image_cached, registry_available, succeeds in (
            (True, False, True),
            (False, True, True),
            (False, False, False),
        ):
            with (
                self.subTest(cached=image_cached, registry=registry_available),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory)
                cached = path / "cached-image"
                if image_cached:
                    cached.write_text("existing pinned image")
                podman = path / "podman"
                # Model storage and registry access at the CLI boundary.
                # Debian 13's Podman 5.4.2 has `image exists` and `pull IMAGE`,
                # but cannot accept the newer `pull --policy=missing`.
                podman.write_text(
                    """#!/bin/sh
case "$1" in
  image)
    [ "$#" -eq 3 ] && [ "$2" = exists ] || exit 125
    test -f "$PILOT_CACHED_IMAGE"
    ;;
  pull)
    if [ "$#" -ne 2 ]; then
      echo 'unsupported pull option (Debian Podman 5.4.2)' >&2
      exit 125
    fi
    if [ "$PILOT_REGISTRY_AVAILABLE" != 1 ]; then
      echo 'registry unavailable' >&2
      exit 1
    fi
    printf '%s' 'downloaded pinned image' > "$PILOT_CACHED_IMAGE"
    ;;
  *) exit 125 ;;
esac
"""
                )
                podman.chmod(0o755)
                invocation = [
                    part.replace("/usr/bin/podman", str(podman)) for part in command
                ]
                invocation[0] = shutil.which("timeout")
                if invocation[2] == "/bin/sh":
                    invocation[2] = shutil.which("sh")
                result = subprocess.run(
                    invocation,
                    env={
                        **os.environ,
                        "PILOT_CACHED_IMAGE": str(cached),
                        "PILOT_REGISTRY_AVAILABLE": str(int(registry_available)),
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertEqual(cached.exists(), succeeds)
                if image_cached:
                    self.assertEqual(cached.read_text(), "existing pinned image")


if __name__ == "__main__":
    unittest.main()
