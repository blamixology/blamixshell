"""Find, install and remove packages (apt, dnf, yum, zypper, pacman, apk). Searching is read-only and uses the
server's cached package lists; installing and removing are run as root, non-interactively, after a confirmation.
Packages the server can't work (or be reached) without are never removed from here. No Qt here."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .dashboard import split_sections

MANAGERS = ("apt", "dnf", "yum", "zypper", "pacman", "apk")
MAX_RESULTS = 200

_QUERY = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_:@-]{0,127}")

# removing one of these breaks the system, the package manager or the way in
PROTECTED = {
    "openssh-server", "openssh", "ssh", "sshd", "dropbear", "sudo", "bash", "dash", "sh", "busybox", "coreutils",
    "util-linux", "login", "passwd", "shadow", "shadow-utils", "pam", "libpam0g", "libpam-modules", "systemd",
    "systemd-sysv", "init", "openrc", "sysvinit", "glibc", "libc6", "libc-bin", "musl", "base-files", "base-passwd",
    "filesystem", "setup", "apt", "dpkg", "rpm", "dnf", "yum", "zypper", "pacman", "apk-tools", "alpine-base",
    "python3", "python", "platform-python", "openssl", "libssl3", "libssl1.1", "ca-certificates", "grub2", "grub-pc",
    "grub-efi-amd64", "grub2-pc", "dracut", "initramfs-tools", "kmod", "udev", "procps", "procps-ng", "iproute2",
    "iproute", "net-tools", "network-manager", "NetworkManager", "netplan.io", "ifupdown", "dhclient",
    "isc-dhcp-client", "linux-image-generic", "linux-generic", "kernel", "kernel-core", "linux", "linux-lts",
}
_PROTECTED_PREFIXES = ("linux-image-", "kernel-", "systemd-", "libc6", "glibc-", "openssh-")


def valid_query(text: str) -> bool:
    return bool(_QUERY.fullmatch(text.strip()))


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name))


def base_name(name: str) -> str:
    """"openssl.x86_64" (dnf/yum) or "curl:amd64" (apt) -> the package name."""
    n = name.split(":", 1)[0]
    for arch in (".x86_64", ".aarch64", ".i686", ".noarch", ".armv7hl", ".ppc64le", ".s390x"):
        if n.endswith(arch):
            return n[: -len(arch)]
    return n


def protected(name: str) -> bool:
    n = base_name(name)
    return n in PROTECTED or n.startswith(_PROTECTED_PREFIXES)


def why_protected(name: str) -> str:
    return (f"{base_name(name)} is needed by the system, its package manager or your SSH access, so it can't be "
            "removed from here. Use the terminal if you really mean it.")


@dataclass
class Package:
    name: str
    summary: str = ""
    version: str = ""          # the installed version, or the available one
    installed: bool = False


# ---------------------------------------------------------------- search
def search_script(manager: str, query: str) -> str:
    """Prints "@@results" (the manager's own search output) and "@@installed" (which of the found ones are
    installed, and their versions)."""
    if manager not in MANAGERS or not valid_query(query):
        raise ValueError("Search for a package name or a word (letters, digits, . + _ -).")
    q = shlex.quote(query.strip())
    n = MAX_RESULTS
    if manager == "apt":
        return (f"echo @@results; apt-cache search -- {q} 2>/dev/null | head -n {n}\n"
                f"echo @@installed; names=$(apt-cache search -- {q} 2>/dev/null | head -n {n} | cut -d' ' -f1); "
                "[ -n \"$names\" ] && dpkg-query -W -f='${Status}|${Package}|${Version}\\n' $names 2>/dev/null "
                "| grep '^install ok installed' | cut -d'|' -f2,3\n")
    if manager in ("dnf", "yum"):
        # the cached metadata first (fast, no network); without a cache, the normal search fetches it
        return (f"echo @@results; out=$({manager} -q -C search -- {q} 2>/dev/null); "
                f"[ -n \"$out\" ] || out=$({manager} -q search -- {q} 2>/dev/null); "
                f"printf '%s\\n' \"$out\" | head -n {n * 2}\n"
                "echo @@installed; rpm -qa --qf '%{NAME}|%{VERSION}-%{RELEASE}\\n' 2>/dev/null\n")
    if manager == "zypper":
        return f"echo @@results; zypper -q --no-refresh search -s -- {q} 2>/dev/null | head -n {n + 5}\n"
    if manager == "pacman":
        return f"echo @@results; pacman -Ss -- {q} 2>/dev/null | head -n {n * 2}\n"
    return (f"echo @@results; apk search -v -d -- {q} 2>/dev/null | head -n {n}\n"
            "echo @@installed; apk info -v 2>/dev/null\n")


def parse_search(manager: str, text: str, query: str = "") -> list[Package]:
    s = split_sections(text)
    body = s.get("results", "")
    installed: dict[str, str] = {}
    for line in s.get("installed", "").splitlines():
        if "|" in line:
            name, _, ver = line.partition("|")
            installed[name.strip()] = ver.strip()
    out: dict[str, Package] = {}
    if manager == "apt":
        for line in body.splitlines():
            name, sep, summary = line.partition(" - ")
            if sep and name.strip():
                n = name.strip()
                out[n] = Package(n, summary.strip(), installed.get(n, ""), n in installed)
    elif manager in ("dnf", "yum"):
        for line in body.splitlines():
            if line.startswith(("=", " ", "Last metadata", "Loaded plugins")) or " : " not in line:
                continue
            name, _, summary = line.partition(" : ")
            n = base_name(name.strip())
            if n and n not in out:
                out[n] = Package(n, summary.strip(), installed.get(n, ""), n in installed)
    elif manager == "zypper":
        for line in body.splitlines():
            p = [x.strip() for x in line.split("|")]
            # S | Name | Type | Version | Arch | Repository
            if len(p) >= 4 and p[1] and p[1] != "Name" and not set(p[1]) <= set("-+"):
                if len(p) >= 6 and p[2] not in ("package", ""):
                    continue
                n = p[1]
                prev = out.get(n)
                if prev is None or (p[0].startswith("i") and not prev.installed):
                    out[n] = Package(n, "", p[3] if len(p) >= 6 else "", p[0].startswith("i"))
    elif manager == "pacman":
        lines = body.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"(\S+)/(\S+)\s+(\S+)(.*)$", line)
            if not m:
                continue
            summary = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].startswith(" ") else ""
            out[m.group(2)] = Package(m.group(2), summary, m.group(3), "[installed" in m.group(4))
    elif manager == "apk":
        have = set()
        for line in s.get("installed", "").splitlines():
            have.add(line.strip())
        for line in body.splitlines():
            full, _, summary = line.partition(" - ")
            m = re.match(r"(.+)-(\d[^-]*-r\d+)$", full.strip())
            if m:
                out[m.group(1)] = Package(m.group(1), summary.strip(), m.group(2), full.strip() in have)
    q = query.strip().lower()
    # exact name first, then names that start with it, then the rest (the managers sort alphabetically)
    return sorted(out.values(), key=lambda p: (p.name.lower() != q, not p.name.lower().startswith(q), p.name.lower()))


# ---------------------------------------------------------------- install / remove
_INSTALL = {
    "apt": "export DEBIAN_FRONTEND=noninteractive; apt-get install -y -o Dpkg::Options::=--force-confold -- {n}",
    "dnf": "dnf -y install {n}",
    "yum": "yum -y install {n}",
    "zypper": "zypper -n install {n}",
    "pacman": "pacman -S --noconfirm --needed {n}",
    "apk": "apk add {n}",
}
_REMOVE = {
    "apt": "export DEBIAN_FRONTEND=noninteractive; apt-get remove -y -- {n}",
    "dnf": "dnf -y remove {n}",
    "yum": "yum -y remove {n}",
    "zypper": "zypper -n remove {n}",
    "pacman": "pacman -R --noconfirm {n}",
    "apk": "apk del {n}",
}
# what a removal would take with it, without changing anything (no root needed for apt and pacman)
_PREVIEW = {
    "apt": "apt-get -s remove -- {n} 2>&1 | sed -n 's/^Remv \\([^ ]*\\).*/\\1/p'",
    "pacman": "pacman -Rp --print-format '%n' {n} 2>&1",
    "apk": "apk del -s {n} 2>&1 | sed -n 's/.*Purging \\([^ ]*\\).*/\\1/p'",
}


def install_command(manager: str, name: str) -> tuple[str, str]:
    """(the command to run as root, what the confirmation shows)."""
    return _command(_INSTALL, manager, name)


def remove_command(manager: str, name: str) -> tuple[str, str]:
    if protected(name):
        raise ValueError(why_protected(name))
    return _command(_REMOVE, manager, name)


def _command(table: dict, manager: str, name: str) -> tuple[str, str]:
    if manager not in table:
        raise ValueError(f"Not supported for {manager or 'this package manager'}.")
    if not valid_name(name):
        raise ValueError(f"“{name}” doesn't look like a package name.")
    script = table[manager].format(n=shlex.quote(name))
    return f"sh -c {shlex.quote(script)}", script.split("; ")[-1]


def removal_preview_command(manager: str, name: str) -> str | None:
    if manager not in _PREVIEW or not valid_name(name):
        return None
    return _PREVIEW[manager].format(n=shlex.quote(name))


def parse_preview(text: str, name: str) -> list[str]:
    """The other packages a removal would take with it (besides `name`)."""
    names = [x.strip() for x in text.splitlines() if x.strip() and valid_name(x.strip())]
    return [x for x in dict.fromkeys(names) if base_name(x) != base_name(name)]
