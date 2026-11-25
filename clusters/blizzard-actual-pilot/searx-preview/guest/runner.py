"""Read the public configuration disk on each boot; keep identity on this root."""

import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/run/searx-preview-config")
STATE = Path("/var/lib/searx-preview")
IMAGE = re.compile(r"ghcr\.io/searxng/searxng@sha256:[0-9a-f]{64}")


def no_symlinks(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("preview paths must not contain symlinks")


def desired_image(config):
    data = json.loads((config / "app.json").read_text())
    if (
        set(data) != {"image"}
        or not isinstance(data["image"], str)
        or not IMAGE.fullmatch(data["image"])
    ):
        raise ValueError("desired image must be an official SearxNG digest reference")
    return data["image"]


def prepare(config=CONFIG, state=STATE, uid=977, gid=977):
    image = desired_image(config)
    no_symlinks(state)
    identity = state / "secret.env"
    for path in (identity, state / "config", state / "cache"):
        no_symlinks(path)
    if identity.exists():
        info = identity.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size == 0
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
        ):
            raise ValueError("existing preview identity must be a private regular file")
    # These are public files, copied only into this preview's retained state.
    contents = {}
    for name in ("settings.yml", "limiter.toml"):
        source = config / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("configuration disk must contain regular settings files")
        contents[name] = source.read_bytes()
        no_symlinks(state / "config" / name)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not identity.exists():
        descriptor = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write("SEARXNG_SECRET=" + secrets.token_hex(32) + "\n")
    (state / "config").mkdir(mode=0o755, exist_ok=True)
    for name, content in contents.items():
        target = state / "config" / name
        target.write_bytes(content)
        target.chmod(0o644)
    cache = state / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    os.chown(cache, uid, gid)
    return image


def ensure_image(image, podman="/usr/bin/podman"):
    cached = subprocess.run([podman, "image", "exists", image], check=False, timeout=20)
    if cached.returncode == 1:
        # Debian 13's Podman 5.4 supports plain pull; cached starts stay offline.
        subprocess.run([podman, "pull", image], check=True, timeout=300)
    elif cached.returncode != 0:
        cached.check_returncode()


def wait_cache(cli="/usr/bin/valkey-cli", attempts=30, delay=1):
    # SearxNG initializes the limiter once and otherwise starts without it.
    # Type=simple/After alone cannot establish that Valkey is accepting PINGs.
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                [cli, "-h", "127.0.0.1", "-p", "6379", "PING"],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip() == "PONG":
                return
        except subprocess.TimeoutExpired:
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError("preview cache is not ready; refusing to start without limiter")


def run_command(image, state=STATE):
    return [
        "/usr/bin/podman",
        "run",
        "--rm",
        "--replace",
        "--pull=never",
        "--name=searx-preview",
        "--network=host",
        "--user=977:977",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs=/tmp:rw,nodev,nosuid,noexec,size=64m",
        "--env-file=" + str(state / "secret.env"),
        "--env=FORCE_OWNERSHIP=false",
        "--env=GRANIAN_HOST=0.0.0.0",
        "--env=GRANIAN_PORT=8080",
        "--volume=" + str(state / "config") + ":/etc/searxng:ro",
        "--volume=" + str(state / "cache") + ":/var/cache/searxng:rw",
        image,
    ]


def main():
    if sys.argv[1:] == ["prepare"]:
        ensure_image(prepare())
    elif sys.argv[1:] == ["wait-cache"]:
        wait_cache()
    elif sys.argv[1:] == ["run"]:
        command = run_command(desired_image(CONFIG))
        os.execv(command[0], command)
    else:
        raise ValueError("use prepare, wait-cache or run")


if __name__ == "__main__":
    main()
