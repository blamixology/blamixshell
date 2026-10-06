"""System basics for the dashboards: time and clock, reboot, swap, fstab and the network. Readers, parsers
and the commands that change things (each is meant to be confirmed first). No Qt here."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# ---------------------------------------------------------------- system: time, reboot, swap
SYSTEM_SCRIPT = r"""
echo @@hostname; hostname 2>/dev/null
echo @@os; if [ -r /etc/os-release ]; then . /etc/os-release; echo "$PRETTY_NAME"; else uname -sr; fi
echo @@kernel; uname -r
echo @@uptime; cat /proc/uptime 2>/dev/null
echo @@time; date '+%Y-%m-%d %H:%M:%S|%Z|%z|%s'
echo @@timedatectl; timedatectl 2>/dev/null
echo @@tzfile; readlink /etc/localtime 2>/dev/null; cat /etc/timezone 2>/dev/null
echo @@ntp; systemctl is-active chronyd ntpd systemd-timesyncd 2>/dev/null
echo @@reboot
if [ -f /var/run/reboot-required ]; then echo yes
elif command -v needs-restarting >/dev/null 2>&1; then
  needs-restarting -r >/dev/null 2>&1; rc=$?
  case $rc in 0) echo no ;; 1) echo yes ;; *) echo unknown ;; esac
else
  echo unknown
fi
echo @@swaps; cat /proc/swaps 2>/dev/null
echo @@meminfo; grep -E '^(MemTotal|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null
echo @@scheduled; cat /run/systemd/shutdown/scheduled 2>/dev/null
"""


@dataclass
class Swap:
    name: str
    kind: str                 # file | partition
    size_kb: int
    used_kb: int
    priority: str = ""


@dataclass
class SystemInfo:
    hostname: str = ""
    os: str = ""
    kernel: str = ""
    uptime_s: float = 0.0
    local_time: str = ""      # the server's wall clock, e.g. "2026-10-06 10:00:01"
    tz_abbr: str = ""         # EEST
    tz_offset: str = ""       # +0300
    tz_name: str = ""         # Europe/Bucharest
    epoch: int = 0
    ntp_synced: bool | None = None
    ntp_active: bool | None = None
    reboot_required: bool | None = None
    swaps: list[Swap] = field(default_factory=list)
    mem_total_kb: int = 0
    swap_total_kb: int = 0
    scheduled: str = ""       # a shutdown / reboot already scheduled ("" = none)

    def drift_s(self, client_now: float) -> float | None:
        """Seconds the server's clock is ahead of `client_now` (negative: behind)."""
        return None if not self.epoch else self.epoch - client_now


def _yes(v: str) -> bool | None:
    v = v.strip().lower()
    return True if v in ("yes", "true", "active", "1") else False if v in ("no", "false", "inactive", "0") else None


def parse_system(text: str) -> SystemInfo:
    from .dashboard import split_sections
    s = split_sections(text)
    i = SystemInfo(hostname=s.get("hostname", "").strip(), os=s.get("os", "").strip(), kernel=s.get("kernel", "").strip())
    try:
        i.uptime_s = float(s.get("uptime", "").split()[0])
    except (ValueError, IndexError):
        pass
    parts = s.get("time", "").strip().split("|")
    if len(parts) == 4:
        i.local_time, i.tz_abbr, i.tz_offset = parts[0], parts[1], parts[2]
        i.epoch = int(parts[3]) if parts[3].isdigit() else 0
    tdc = s.get("timedatectl", "")
    m = re.search(r"Time ?zone:\s*(\S+)", tdc)
    if m:
        i.tz_name = m.group(1)
    for pat, attr in ((r"(?:System clock|NTP) synchronized:\s*(\w+)", "ntp_synced"), (r"NTP (?:service|enabled):\s*(\w+)", "ntp_active")):
        m = re.search(pat, tdc)
        if m:
            setattr(i, attr, _yes(m.group(1)) if attr == "ntp_synced" else _yes(m.group(1).replace("active", "yes")))
    if not i.tz_name:
        for line in s.get("tzfile", "").splitlines():
            line = line.strip()
            if "zoneinfo/" in line:
                i.tz_name = line.split("zoneinfo/", 1)[1]
            elif re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+|UTC", line):
                i.tz_name = line
    if i.ntp_active is None and s.get("ntp", "").strip():
        i.ntp_active = "active" in s["ntp"].split()
    i.reboot_required = {"yes": True, "no": False}.get(s.get("reboot", "").strip())
    for line in s.get("swaps", "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[2].isdigit():
            i.swaps.append(Swap(p[0], p[1], int(p[2]), int(p[3]) if p[3].isdigit() else 0, p[4] if len(p) > 4 else ""))
    mem = {m.group(1): int(m.group(2)) for m in re.finditer(r"(\w+):\s+(\d+)", s.get("meminfo", ""))}
    i.mem_total_kb, i.swap_total_kb = mem.get("MemTotal", 0), mem.get("SwapTotal", 0)
    i.scheduled = s.get("scheduled", "").strip()
    return i


# ---------------------------------------------------------------- commands that change the system
_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*")


def valid_zone(zone: str) -> bool:
    return bool(_ZONE.fullmatch(zone)) and ".." not in zone and len(zone) < 64


def timezone_command(zone: str) -> str:
    zone = zone.strip()
    if not valid_zone(zone):
        raise ValueError("A time zone looks like Europe/Bucharest or UTC.")
    q = shlex.quote(zone)
    z = shlex.quote(f"/usr/share/zoneinfo/{zone}")
    return "sh -c " + shlex.quote(f"[ -e {z} ] || {{ echo 'Unknown time zone' >&2; exit 1; }}; "
                                  f"timedatectl set-timezone {q} 2>/dev/null || ln -sf {z} /etc/localtime")


def ntp_command(on: bool) -> str:
    return f"timedatectl set-ntp {'true' if on else 'false'}"


def reboot_command(minutes: int = 0) -> str:
    if minutes <= 0:
        return "sh -c " + shlex.quote("systemctl reboot || shutdown -r now || reboot")
    return f"shutdown -r +{int(minutes)} 'Reboot scheduled from BlamixShell'"


def shutdown_command(minutes: int = 0) -> str:
    if minutes <= 0:
        return "sh -c " + shlex.quote("systemctl poweroff || shutdown -h now || poweroff")
    return f"shutdown -h +{int(minutes)} 'Shutdown scheduled from BlamixShell'"


def cancel_shutdown_command() -> str:
    return "shutdown -c"


def minutes_from(text: str) -> int:
    """"5" or "5 min" -> 5 (1 to 10080 minutes); ValueError otherwise."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*(?:min\w*)?\s*", text)
    if not m or not 1 <= int(m.group(1)) <= 10080:
        raise ValueError("Give a number of minutes from 1 to 10080.")
    return int(m.group(1))


def swap_size_mb(text: str) -> int:
    """"2G", "512M" or "2048" (MB) -> megabytes (64 MB to 128 GB); ValueError otherwise."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([MmGg]?)[Bb]?\s*", text)
    if not m:
        raise ValueError("Give a size like 2G or 512M.")
    mb = float(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    if not 64 <= mb <= 131072:
        raise ValueError("The swap file can be 64 MB to 128 GB.")
    return int(mb)


_SWAP_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,100}")


def _swap_path(path: str) -> str:
    if not _SWAP_PATH.fullmatch(path) or ".." in path or path.endswith("/"):
        raise ValueError("Use a plain path like /swapfile.")
    return path


def swap_create_command(size_mb: int, path: str = "/swapfile") -> str:
    """A swap file that is also in /etc/fstab. Refuses an existing file and checks the free space first."""
    path = _swap_path(path)
    folder = path.rsplit("/", 1)[0] or "/"
    steps = [f"[ ! -e {path} ] || {{ echo '{path} already exists' >&2; exit 1; }}",
             f"avail=$(df -Pk {folder} | awk 'NR==2{{print $4}}')",
             f"[ \"$avail\" -gt {size_mb * 1024 + 524288} ] || {{ echo 'Not enough free disk space' >&2; exit 1; }}",
             f"{{ fallocate -l {size_mb}M {path} || dd if=/dev/zero of={path} bs=1M count={size_mb}; }}",
             f"chmod 600 {path}", f"mkswap {path}", f"swapon {path}",
             f"{{ grep -q '^{path} ' /etc/fstab || echo '{path} none swap sw 0 0' >> /etc/fstab; }}"]
    return "sh -c " + shlex.quote(" && ".join(steps))


def swap_remove_command(path: str) -> str:
    """Turn a swap *file* off, drop it from /etc/fstab and delete it."""
    path = _swap_path(path)
    steps = [f"swapoff {path}", f"sed -i '\#^{path}[[:space:]]#d' /etc/fstab", f"rm -f {path}"]
    return "sh -c " + shlex.quote(" && ".join(steps))


# ---------------------------------------------------------------- fstab / mounts
MOUNTS_SCRIPT = r"""
echo @@fstab; cat /etc/fstab 2>/dev/null
echo @@mounts; cat /proc/mounts 2>/dev/null
"""
PROTECTED_MOUNTS = ("/", "/boot", "/boot/efi", "/usr", "/var", "/etc", "/proc", "/sys", "/dev")


@dataclass
class FstabEntry:
    device: str
    mount: str
    fstype: str
    options: str
    dump: str = "0"
    passno: str = "0"
    mounted: bool | None = None

    @property
    def is_swap(self) -> bool:
        return self.fstype == "swap"


def _unescape(v: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), v)


def parse_mounts(text: str) -> list[FstabEntry]:
    from .dashboard import split_sections
    s = split_sections(text)
    live = set()
    for line in s.get("mounts", "").splitlines():
        p = line.split()
        if len(p) >= 2:
            live.add(_unescape(p[1]))
    out = []
    for line in s.get("fstab", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 4:
            continue
        e = FstabEntry(_unescape(p[0]), _unescape(p[1]), p[2], p[3], p[4] if len(p) > 4 else "0",
                       p[5] if len(p) > 5 else "0")
        e.mounted = None if e.is_swap or e.mount in ("none", "swap") else e.mount in live
        out.append(e)
    return out


def mount_command(mountpoint: str) -> str:
    return f"mount {shlex.quote(mountpoint)}"


def umount_command(mountpoint: str) -> str:
    if mountpoint.rstrip("/") in PROTECTED_MOUNTS or mountpoint == "/":
        raise ValueError("That mount is needed by the running system.")
    return f"umount {shlex.quote(mountpoint)}"


def verify_fstab_command() -> str:
    """What mount -a would do, without doing it (and the stricter findmnt --verify where it exists)."""
    return "sh -c " + shlex.quote("findmnt --verify 2>&1 || mount -fav 2>&1")


# ---------------------------------------------------------------- network
NETWORK_SCRIPT = r"""
echo @@hostname; hostname -f 2>/dev/null || hostname
echo @@link; ip -o link show 2>/dev/null
echo @@addr; ip -o addr show 2>/dev/null
echo @@route; ip route show 2>/dev/null
echo @@dns; grep -E '^(nameserver|search)' /etc/resolv.conf 2>/dev/null
echo @@noip; command -v ip >/dev/null 2>&1 && echo no || echo yes
"""


@dataclass
class Interface:
    name: str
    state: str = ""
    mtu: str = ""
    mac: str = ""
    addresses: list[str] = field(default_factory=list)
    flags: str = ""


@dataclass
class Network:
    hostname: str = ""
    interfaces: list[Interface] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)
    search: list[str] = field(default_factory=list)
    gateway: str = ""
    has_ip_tool: bool = True


_LINK = re.compile(r"^\d+:\s+([^:@\s]+)(?:@\S+)?:\s+<([^>]*)>\s+mtu\s+(\d+).*?state\s+(\S+)")
_MAC = re.compile(r"link/(?:ether|\S+)\s+([0-9a-f:]{17})")
_ADDR = re.compile(r"^\d+:\s+(\S+?)(?::)?\s+(inet6?)\s+(\S+)")


def parse_network(text: str) -> Network:
    from .dashboard import split_sections
    s = split_sections(text)
    n = Network(hostname=s.get("hostname", "").strip(), has_ip_tool=s.get("noip", "").strip() != "yes")
    by: dict[str, Interface] = {}
    for line in s.get("link", "").splitlines():
        m = _LINK.match(line.strip())
        if m:
            i = Interface(m.group(1), m.group(4), m.group(3), flags=m.group(2))
            mac = _MAC.search(line)
            if mac and mac.group(1) != "00:00:00:00:00:00":
                i.mac = mac.group(1)
            by[i.name] = i
            n.interfaces.append(i)
    for line in s.get("addr", "").splitlines():
        m = _ADDR.match(line.strip())
        if m:
            i = by.get(m.group(1))
            if i is None:
                i = by[m.group(1)] = Interface(m.group(1))
                n.interfaces.append(i)
            i.addresses.append(m.group(3))
    n.routes = [l.strip() for l in s.get("route", "").splitlines() if l.strip()]
    for r in n.routes:
        if r.startswith("default") and not n.gateway:
            mv = re.search(r"via\s+(\S+)", r)
            n.gateway = mv.group(1) if mv else ""
    for line in s.get("dns", "").splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] == "nameserver":
            n.dns.append(p[1])
        elif p and p[0] == "search":
            n.search += p[1:]
    return n


_HOST = re.compile(r"[A-Za-z0-9._:\[\]-]{1,253}")


def check_command(host: str, port: int | None = None) -> str:
    """Name lookup, ping and (with a port) a TCP connection, from the server."""
    host = host.strip()
    if not _HOST.fullmatch(host):
        raise ValueError("Give a host name or IP address.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The port is 1 to 65535.")
    h = shlex.quote(host)
    parts = [f"echo '== name lookup'; getent hosts {h} | head -3 || echo 'failed'",
             f"echo; echo '== ping'; ping -c 3 -W 2 {h} 2>&1 | tail -4"]
    if port:
        parts.append(f"echo; echo '== tcp {port}'; (timeout 5 bash -c 'exec 3<>/dev/tcp/{host}/{port}' && echo open) "
                     "2>&1 || echo 'closed, filtered or unreachable'")
    return "sh -c " + shlex.quote("; ".join(parts))


def parse_target(text: str) -> tuple[str, int | None]:
    """"example.com" or "example.com:443" -> (host, port or None)."""
    t = text.strip()
    if t.count(":") == 1 and t.rsplit(":", 1)[1].isdigit():
        host, port = t.rsplit(":", 1)
        return host, int(port)
    return t, None
