"""Regenerate the public cloud-init seed after changing the bootstrap or units."""

from pathlib import Path

import yaml

GUEST = Path(__file__).resolve().parent


class Dumper(yaml.SafeDumper):
    pass


def string(dumper, value):
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    )


Dumper.add_representer(str, string)


def render():
    actual = yaml.safe_load(
        (GUEST.parents[1] / "actual/guest/user-data.yaml").read_text()
    )
    shared = [
        f
        for f in actual["write_files"]
        if f["path"] == "/etc/apt/sources.list.d/debian.sources"
    ]
    profile = next(
        f["content"]
        for f in actual["write_files"]
        if f["path"] == "/etc/systemd/network/05-actual-pilot.network"
    )
    files = shared + [
        {
            "path": "/etc/systemd/network/05-searx-preview.network",
            "permissions": "0644",
            "content": profile,
        }
    ]
    mapping = [
        (GUEST / n, "/usr/local/lib/searx-preview/" + n)
        for n in ("runner.py", "bootstrap.sh")
    ]
    mapping += [
        (p, "/etc/systemd/system/" + p.name)
        for p in sorted((GUEST / "units").iterdir())
    ]
    mapping += [(GUEST / "valkey.conf", "/etc/searx-preview/valkey.conf")]
    files += [
        {"path": dest, "permissions": "0644", "content": src.read_text()}
        for src, dest in mapping
    ]
    data = {
        "disable_root": True,
        "ssh_pwauth": False,
        "users": ["default"],
        "write_files": files,
        "runcmd": [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "searx-preview-bootstrap.service"],
            ["systemctl", "start", "--no-block", "searx-preview-bootstrap.service"],
        ],
    }
    return "#cloud-config\n" + yaml.dump(data, Dumper=Dumper, sort_keys=False)


if __name__ == "__main__":
    (GUEST.parent / "app/user-data.yaml").write_text(render())
