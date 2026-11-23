"""Prepare only the identified new pilot disk, or reuse its labelled filesystem."""

import stat
import subprocess
from pathlib import Path

DEVICE = Path("/dev/disk/by-id/virtio-actual-pilot-data")
DISK_SERIAL = "actual-pilot-data"
LABEL = "actual-pilot"


def filesystem_action(info):
    if (
        info.get("TYPE") == "ext4"
        and info.get("LABEL") == LABEL
        and not info.get("PTTYPE")
    ):
        return "mount"
    if info:
        raise ValueError("unexpected disk signature; refusing to format existing data")
    return "format"


def verify_blank(path, expected_size):
    read = 0
    with Path(path).open("rb", buffering=0) as disk:
        while read < expected_size:
            block = disk.read(min(8 * 1024 * 1024, expected_size - read))
            if not block:
                raise ValueError("disk shorter than expected")
            if any(block):
                raise ValueError("nonzero existing data; refusing to format")
            read += len(block)
        if disk.read(1):
            raise ValueError("disk larger than expected")


def main():
    if not stat.S_ISBLK(DEVICE.stat().st_mode):
        raise ValueError("pilot data path is not a block device")
    serial = subprocess.check_output(
        ["lsblk", "-dn", "-o", "SERIAL", str(DEVICE)], text=True
    ).strip()
    if serial != DISK_SERIAL:
        raise ValueError("unexpected disk serial")
    size = int(
        subprocess.check_output(["blockdev", "--getsize64", str(DEVICE)], text=True)
    )
    # CDI reserves filesystem overhead from the 10 GiB filesystem-mode PVC.
    if not 9 * 1024**3 <= size <= 10 * 1024**3:
        raise ValueError("unexpected pilot data disk size")
    probe = subprocess.run(
        ["blkid", "-p", "-o", "export", str(DEVICE)],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode not in (0, 2):
        raise ValueError("cannot identify filesystem safely")
    info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    # DEVNAME describes the queried device, not a filesystem signature.
    info.pop("DEVNAME", None)
    if filesystem_action(info) == "format":
        verify_blank(DEVICE, size)
        subprocess.run(["mkfs.ext4", "-m", "0", "-L", LABEL, str(DEVICE)], check=True)
    print("pilot data filesystem ready", flush=True)


if __name__ == "__main__":
    main()
