"""Storage tab helpers: filesystems with their inode use, and the biggest folders. No Qt here."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import dashboard as d
from .cron import shell_path

FS_SCRIPT = r"""
echo @@df; df -PTk 2>/dev/null
echo @@inodes; df -Pi 2>/dev/null
"""

# not real disks: hidden unless asked for
VIRTUAL = {"tmpfs", "devtmpfs", "squashfs", "proc", "sysfs", "cgroup", "cgroup2", "overlay", "efivarfs",
           "debugfs", "securityfs", "pstore", "autofs", "mqueue", "hugetlbfs", "fusectl", "configfs", "tracefs",
           "ramfs", "binfmt_misc", "devpts", "nsfs", "bpf", "selinuxfs"}


@dataclass
class Filesystem:
    mount: str
    fstype: str
    size_kb: int
    used_kb: int
    avail_kb: int
    percent: float
    inode_percent: float | None = None

    @property
    def virtual(self) -> bool:
        return self.fstype in VIRTUAL


def parse_filesystems(text: str) -> list[Filesystem]:
    s = d.split_sections(text)
    inodes: dict[str, float] = {}
    for line in s.get("inodes", "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and p[4].endswith("%") and p[4][:-1].isdigit():
            inodes[" ".join(p[5:])] = float(p[4][:-1])
    out = []
    for disk in d.parse_df(s.get("df", "")):
        out.append(Filesystem(disk.mount, disk.fstype, disk.size_kb, disk.used_kb, disk.avail_kb, disk.percent,
                              inodes.get(disk.mount)))
    return sorted(out, key=lambda f: f.mount)


def du_command(path: str, limit: int = 25) -> str:
    """The biggest entries directly under `path`, on that filesystem only (-x), biggest first."""
    return f"du -xk -d 1 -- {shell_path(path)} 2>/dev/null | sort -rn | head -n {int(limit) + 1}"


def parse_du(text: str, path: str) -> tuple[int, list[tuple[int, str]]]:
    """(total KB of `path`, [(KB, folder) ...] for what is inside it, biggest first)."""
    rows = []
    for line in text.splitlines():
        m = re.match(r"^(\d+)\s+(.+)$", line.strip("\n"))
        if m:
            rows.append((int(m.group(1)), m.group(2)))
    norm = path.rstrip("/") or "/"
    total = next((kb for kb, p in rows if (p.rstrip("/") or "/") == norm), 0)
    inside = [(kb, p) for kb, p in rows if (p.rstrip("/") or "/") != norm]
    if not total and rows:
        total = max(kb for kb, _ in rows)
    return total, inside


def parent(path: str) -> str:
    p = path.rstrip("/")
    return p.rsplit("/", 1)[0] or "/"

