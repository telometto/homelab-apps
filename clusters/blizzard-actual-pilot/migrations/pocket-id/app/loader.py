"""Read the public configuration ISO afresh at every boot; fail closed."""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


def _directory(path):
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("parent traversal is not a configuration directory")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.absolute().parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return directory
    except OSError as error:
        os.close(directory)
        raise ValueError(
            "configuration directory must exist without symlinks"
        ) from error


def _read(directory, name):
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError(f"{name} must be a small regular file")
            content = source.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError(f"{name} exceeds the public configuration limit")
            return content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot safely read {name}") from error


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def execute(config_dir, service):
    if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", service):
        raise ValueError("invalid expected service identity")
    directory = _directory(config_dir)
    try:
        plan = json.loads(_read(directory, "plan.json"), object_pairs_hook=_unique_keys)
        if (
            not isinstance(plan, dict)
            or type(plan.get("schemaVersion", 1)) is not int
            or plan.get("schemaVersion", 1) != 1
        ):
            raise ValueError("unsupported public plan format")
        if plan.get("serviceId") != service:
            raise ValueError("configuration belongs to a different service")
        ready = plan.get("runtimeReady")
        if type(ready) is not bool:
            raise ValueError("runtimeReady must be a JSON boolean")
        if not ready:
            raise RuntimeError(
                f"{service}: runtimeReady is false; runtime is unimplemented"
            )
        script = _read(directory, "runtime.sh")
    finally:
        os.close(directory)
    # Execute the bytes already validated/read from the ISO, never reopen its
    # path. The ISO stays mounted noexec; only this explicit gate invokes sh.
    subprocess.run(["/bin/sh", "-eu"], input=script, text=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("/run/kubevirt-groundwork-config")
    )
    args = parser.parse_args()
    try:
        execute(args.config, args.service)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Groundwork runtime refused: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main())
