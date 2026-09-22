"""Filesystem helpers: mounts, filesystem types, free space, safe browsing, hashing."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Optional

from . import config

PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore", "bpf",
    "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs",
    "autofs", "overlay", "squashfs", "nsfs", "ramfs", "efivarfs", "binfmt_misc", "rpc_pipefs",
    "shm", "selinuxfs",
}
# Filesystems that cannot store POSIX owners/modes per file (or have coarse mtimes).
NO_PERM_FS = {"ntfs", "ntfs3", "fuseblk", "vfat", "msdos", "exfat", "fat", "cifs", "smb3"}
COARSE_TIME_FS = {"vfat", "msdos", "exfat", "fat"}


def _unescape(s: str) -> str:
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def read_mounts() -> list[dict]:
    out = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                out.append({"device": _unescape(parts[0]), "mountpoint": _unescape(parts[1]), "fstype": parts[2]})
    except OSError:
        pass
    return out


def mount_for(path: str) -> Optional[dict]:
    """Longest-prefix mount entry containing path."""
    real = os.path.realpath(path)
    best = None
    for m in read_mounts():
        mp = m["mountpoint"]
        if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best["mountpoint"]):
                best = m
    return best


def fstype(path: str) -> str:
    m = mount_for(path)
    return m["fstype"] if m else "unknown"


def is_mountpoint(path: str) -> bool:
    """True if path itself is a mount root (bind mount into a container counts)."""
    try:
        if os.path.ismount(path):
            return True
    except OSError:
        return False
    real = os.path.realpath(path)
    return any(m["mountpoint"] == real for m in read_mounts())


def disk_usage(path: str) -> Optional[dict]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "free": free, "used": total - st.f_bfree * st.f_frsize}


def same_device(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def volumes() -> list[dict]:
    seen = set()
    vols = []
    for m in read_mounts():
        if m["fstype"] in PSEUDO_FS or m["mountpoint"] in seen:
            continue
        if not path_allowed(m["mountpoint"]):
            continue
        seen.add(m["mountpoint"])
        usage = disk_usage(m["mountpoint"])
        if not usage or usage["total"] == 0:
            continue
        vols.append({**m, **usage})
    vols.sort(key=lambda v: v["mountpoint"])
    return vols


def path_allowed(path: str) -> bool:
    real = os.path.realpath(path)
    for root in config.ALLOWED_ROOTS:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def browse(path: str) -> dict:
    if not path:
        return {"path": "", "parent": None, "dirs": [
            {"name": r, "path": r} for r in config.ALLOWED_ROOTS if os.path.isdir(r)
        ]}
    real = os.path.realpath(path)
    if not path_allowed(real):
        raise PermissionError(f"{path} is outside the allowed roots ({', '.join(config.ALLOWED_ROOTS)})")
    dirs = []
    with os.scandir(real) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=True) and not e.name.startswith(config.INTERNAL_PREFIX):
                    dirs.append({"name": e.name, "path": os.path.join(real, e.name)})
            except OSError:
                continue
    dirs.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(real)
    usage = disk_usage(real)
    return {
        "path": real,
        "parent": parent if path_allowed(parent) and parent != real else "",
        "dirs": dirs,
        "fstype": fstype(real),
        "usage": usage,
    }


# Size display units. "si": 1 kB = 1000 B (FreeFileSync, drive labels, most
# file managers). "iec": 1 KiB = 1024 B. Set from the saved setting at startup.
SIZE_UNITS = "si"


def set_size_units(units: str) -> None:
    global SIZE_UNITS
    SIZE_UNITS = "iec" if units == "iec" else "si"


def human(n: float) -> str:
    n = float(n or 0)
    if SIZE_UNITS == "iec":
        base, units = 1024.0, ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    else:
        base, units = 1000.0, ("B", "kB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(n) < base or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= base
    return f"{n:.2f} {units[-1]}"


def sample_hash(path: str, chunk: int = 1 << 20) -> Optional[str]:
    """Cheap fingerprint: size + first/middle/last MiB. Used for move detection."""
    try:
        size = os.path.getsize(path)
        h = hashlib.blake2b(digest_size=16)
        h.update(str(size).encode())
        with open(path, "rb") as fh:
            for off in sorted({0, max(0, size // 2 - chunk // 2), max(0, size - chunk)}):
                fh.seek(off)
                h.update(fh.read(chunk))
        return h.hexdigest()
    except OSError:
        return None


def files_identical(a: str, b: str, cancel=None, chunk: int = 4 << 20) -> bool:
    """Byte-for-byte comparison with early exit."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                if cancel is not None and cancel.is_set():
                    return True
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def is_link(path: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False
