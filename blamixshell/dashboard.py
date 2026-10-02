"""Agentless server dashboard (Qt-free): runs standard commands over an existing SSH
connection and parses their output. Nothing is installed on the server.

Read-only collectors: overview, services, processes, logs, ports, updates, users.
Actions (service start/stop/restart/enable/disable, ending a process) are built as
explicit commands the UI shows before running, using sudo only when needed.
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field

import paramiko

from .ssh_core import exec_command


# ---------------------------------------------------------------- running commands
@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Runner:
    """Runs commands on an open connection (one exec channel each, like `ssh host cmd`)."""

    def __init__(self, client: paramiko.SSHClient):
        self.client = client

    def run(self, command: str, timeout: float = 30, stdin: str | None = None) -> Result:
        # a plain POSIX shell with C locale: predictable, parseable output. The sbin
        # directories are added because non-root logins often lack them (CentOS, Debian),
        # which hides service/chkconfig/ss and friends.
        wrapped = ('LC_ALL=C LANG=C PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin" sh -c '
                   + shlex.quote(command))
        i, o, e = exec_command(self.client, wrapped, timeout=timeout)
        if stdin is not None:
            i.write(stdin)
            i.flush()
        i.channel.shutdown_write()
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return Result(o.channel.recv_exit_status(), out, err)


def split_sections(text: str) -> dict[str, str]:
    """Output of a batched script: '@@name' lines start sections."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in sections.items()}


# ---------------------------------------------------------------- overview
# Which service manager runs this box. /run/systemd/system is systemd's own "booted with
# systemd" test: containers often have systemctl installed without systemd running.
INIT_DETECT = ("if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then echo systemd; "
               "elif command -v rc-service >/dev/null 2>&1 && [ -d /etc/runlevels ]; then echo openrc; "
               "elif ls /etc/init.d/* >/dev/null 2>&1 || [ -d /etc/rc.d/init.d ]; then echo sysv; fi")

OVERVIEW_SCRIPT = r"""
echo @@host; hostname 2>/dev/null
echo @@os; if [ -r /etc/os-release ]; then . /etc/os-release; echo "$PRETTY_NAME"; else uname -sr; fi
echo @@kernel; uname -r
echo @@arch; uname -m
echo @@uptime; cat /proc/uptime 2>/dev/null
echo @@load; cat /proc/loadavg 2>/dev/null
echo @@nproc; nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null
echo @@mem; cat /proc/meminfo 2>/dev/null
echo @@stat1; head -n1 /proc/stat 2>/dev/null
sleep 0.5
echo @@stat2; head -n1 /proc/stat 2>/dev/null
echo @@df; df -PTk -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null || df -Pk 2>/dev/null
echo @@who; who 2>/dev/null | wc -l
echo @@init; INIT_DETECT
echo @@systemd; [ -d /run/systemd/system ] && systemctl is-system-running 2>/dev/null
echo @@failed; [ -d /run/systemd/system ] && systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | awk '{print ($1 ~ /[.]service$/) ? $1 : $2}'
echo @@pid1; cat /proc/1/comm 2>/dev/null
echo @@uid; id -u
echo @@end
""".replace("INIT_DETECT", INIT_DETECT)


@dataclass
class Disk:
    mount: str
    fstype: str
    size_kb: int
    used_kb: int
    avail_kb: int

    @property
    def percent(self) -> float:
        total = self.used_kb + self.avail_kb
        return 100.0 * self.used_kb / total if total else 0.0


@dataclass
class Overview:
    host: str = ""
    os: str = ""
    kernel: str = ""
    arch: str = ""
    uptime_s: float = 0.0
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cpus: int = 0
    cpu_percent: float | None = None
    mem_total_kb: int = 0
    mem_avail_kb: int = 0
    swap_total_kb: int = 0
    swap_free_kb: int = 0
    disks: list[Disk] = field(default_factory=list)
    sessions: int = 0
    systemd: str = ""               # "running", "degraded", … or "" without systemd
    init: str = ""                  # systemd / openrc / sysv / "" (none found)
    pid1: str = ""                  # e.g. "systemd", "init", or "bash" in a container
    failed_units: list[str] = field(default_factory=list)
    root: bool = False

    @property
    def mem_used_kb(self) -> int:
        return max(0, self.mem_total_kb - self.mem_avail_kb)

    @property
    def mem_percent(self) -> float:
        return 100.0 * self.mem_used_kb / self.mem_total_kb if self.mem_total_kb else 0.0

    @property
    def swap_percent(self) -> float:
        used = self.swap_total_kb - self.swap_free_kb
        return 100.0 * used / self.swap_total_kb if self.swap_total_kb else 0.0


def _cpu_times(line: str) -> tuple[int, int] | None:
    """(idle, total) jiffies from the first /proc/stat line."""
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:] if x.isdigit()]
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)       # idle + iowait
    total = sum(nums[:8])                                      # without guest (already in user)
    return idle, total


def parse_df(text: str) -> list[Disk]:
    disks = []
    lines = text.splitlines()
    if not lines:
        return disks
    has_type = "Type" in lines[0]
    for line in lines[1:]:
        p = line.split()
        if has_type and len(p) >= 7:
            fstype, size, used, avail, mount = p[1], p[2], p[3], p[4], " ".join(p[6:])
        elif not has_type and len(p) >= 6:
            fstype, size, used, avail, mount = "", p[1], p[2], p[3], " ".join(p[5:])
        else:
            continue
        if not (size.isdigit() and used.isdigit() and avail.isdigit()) or int(size) == 0:
            continue
        disks.append(Disk(mount, fstype, int(size), int(used), int(avail)))
    return disks


def parse_overview(text: str) -> Overview:
    s = split_sections(text)
    ov = Overview(host=s.get("host", ""), os=s.get("os", ""), kernel=s.get("kernel", ""), arch=s.get("arch", ""))
    try:
        ov.uptime_s = float(s.get("uptime", "0").split()[0])
    except (ValueError, IndexError):
        pass
    try:
        a, b, c = s.get("load", "").split()[:3]
        ov.load = (float(a), float(b), float(c))
    except ValueError:
        pass
    n = s.get("nproc", "").strip()
    ov.cpus = int(n) if n.isdigit() else 0
    mem = {}
    for line in s.get("mem", "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            mem[m.group(1)] = int(m.group(2))
    ov.mem_total_kb = mem.get("MemTotal", 0)
    ov.mem_avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0))
    ov.swap_total_kb, ov.swap_free_kb = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    t1, t2 = _cpu_times(s.get("stat1", "")), _cpu_times(s.get("stat2", ""))
    if t1 and t2 and t2[1] > t1[1]:
        busy = (t2[1] - t1[1]) - (t2[0] - t1[0])
        ov.cpu_percent = max(0.0, min(100.0, 100.0 * busy / (t2[1] - t1[1])))
    ov.disks = parse_df(s.get("df", ""))
    w = s.get("who", "").strip()
    ov.sessions = int(w) if w.isdigit() else 0
    ov.systemd = s.get("systemd", "").strip().splitlines()[-1] if s.get("systemd", "").strip() else ""
    ov.failed_units = [u for u in s.get("failed", "").split() if u.endswith(".service")]
    ov.init = s.get("init", "").strip()
    ov.pid1 = s.get("pid1", "").strip()
    ov.root = s.get("uid", "").strip() == "0"
    return ov


def overview(runner: Runner) -> Overview:
    return parse_overview(runner.run(OVERVIEW_SCRIPT, timeout=20).out)


# ---------------------------------------------------------------- health strip (status bar)
# Small and cheap: runs every few seconds for the active terminal. CPU % comes from the
# difference to the previous sample, so the script never sleeps.
HEALTH_SCRIPT = r"""
echo @@stat; head -n1 /proc/stat 2>/dev/null
echo @@load; cat /proc/loadavg 2>/dev/null
echo @@nproc; nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null
echo @@mem; grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null
echo @@df; df -Pk / 2>/dev/null
echo @@end
"""


@dataclass
class Health:
    cpu: float | None = None         # % (None on the first sample)
    mem: float | None = None         # % used
    disk: float | None = None        # % used on /
    load: float | None = None        # 1-minute load average
    cpus: int = 0
    sample: tuple[int, int] | None = None   # (idle, total) for the next CPU %
    swap: float | None = None        # % used (None: no swap)
    loads: tuple[float, ...] = ()    # 1 / 5 / 15 minute load
    mem_kb: tuple[int, int] | None = None    # (used, total)
    swap_kb: tuple[int, int] | None = None
    disk_kb: tuple[int, int] | None = None


def parse_health(text: str, prev: tuple[int, int] | None = None) -> Health:
    s = split_sections(text)
    h = Health()
    h.sample = _cpu_times(s.get("stat", ""))
    if prev and h.sample and h.sample[1] > prev[1]:
        busy = (h.sample[1] - prev[1]) - (h.sample[0] - prev[0])
        h.cpu = max(0.0, min(100.0, 100.0 * busy / (h.sample[1] - prev[1])))
    try:
        h.loads = tuple(float(x) for x in s.get("load", "").split()[:3])
        h.load = h.loads[0]
    except (ValueError, IndexError):
        h.loads = ()
    n = s.get("nproc", "").strip()
    h.cpus = int(n) if n.isdigit() else 0
    mem = {m.group(1): int(m.group(2)) for m in re.finditer(r"(\w+):\s+(\d+)", s.get("mem", ""))}
    total = mem.get("MemTotal", 0)
    if total:
        avail = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0))
        h.mem = 100.0 * max(0, total - avail) / total
        h.mem_kb = (max(0, total - avail), total)
    swap_total = mem.get("SwapTotal", 0)
    if swap_total:
        used = max(0, swap_total - mem.get("SwapFree", 0))
        h.swap = 100.0 * used / swap_total
        h.swap_kb = (used, swap_total)
    disks = parse_df(s.get("df", ""))
    if disks:
        h.disk = disks[0].percent
        h.disk_kb = (disks[0].used_kb, disks[0].used_kb + disks[0].avail_kb)
    return h


def health(runner: Runner, prev: tuple[int, int] | None = None) -> Health:
    h = parse_health(runner.run(HEALTH_SCRIPT, timeout=10).out, prev)
    if h.cpu is None and h.sample is not None:
        # first sample for this pane: take a second one shortly after so CPU % shows at once
        time.sleep(0.6)
        h2 = parse_health(runner.run(HEALTH_SCRIPT, timeout=10).out, h.sample)
        if h2.cpu is not None:
            return h2
    return h


# ---------------------------------------------------------------- services
# systemd (any version), OpenRC (Alpine, Gentoo), SysV init scripts (CentOS 6, older
# Debian, containers) and supervisord, which can run next to any of them.
@dataclass
class Service:
    unit: str
    load: str
    active: str                     # active / inactive / failed / unknown
    sub: str                        # running / dead / exited / … (or the tool's own word)
    description: str
    enabled: str = ""               # enabled / disabled / static / masked / …
    init: str = "systemd"           # systemd / openrc / sysv / supervisor

    @property
    def failed(self) -> bool:
        return self.active == "failed"


INIT_NAMES = {"systemd": "systemd", "openrc": "OpenRC", "sysv": "SysV init scripts",
              "supervisor": "supervisord"}

# init scripts that aren't services
_SYSV_SKIP = ("README skeleton functions rc rcS rc.local halt killall reboot single sendsigs umountfs "
              "umountnfs.sh umountroot bootlogd hwclock.sh mountall.sh mountkernfs.sh mountdevsubfs.sh "
              "checkroot.sh checkfs.sh urandom netconsole")

SERVICES_SCRIPT = r"""
INIT=$(INIT_DETECT)
echo @@init; echo "$INIT"
echo @@pid1; cat /proc/1/comm 2>/dev/null
case "$INIT" in
systemd)
  echo @@units; systemctl list-units --type=service --all --no-legend --no-pager 2>&1
  echo @@files; systemctl list-unit-files --type=service --no-legend --no-pager 2>/dev/null ;;
openrc)
  echo @@openrc; rc-status --all 2>&1 ;;
sysv)
  T=""; command -v timeout >/dev/null 2>&1 && T="timeout 5"
  echo @@sysv
  for f in /etc/init.d/* /etc/rc.d/init.d/*; do
    [ -f "$f" ] && [ -x "$f" ] || continue
    n=${f##*/}
    case " SKIP " in *" $n "*) continue ;; esac
    case "$n" in *.dpkg*|*.rpm*|*~) continue ;; esac
    case " $seen " in *" $n "*) continue ;; esac
    seen="$seen $n"
    out=$($T "$f" status 2>&1 </dev/null); rc=$?
    msg=$(printf '%s\n' "$out" | grep -v '^ *$' | head -n1 | tr '|' '/' | cut -c1-120)
    e=disabled
    for r in /etc/rc3.d /etc/rc.d/rc3.d /etc/rc2.d /etc/rc.d/rc2.d /etc/rc5.d /etc/rc.d/rc5.d; do
      ls "$r"/S[0-9][0-9]"$n" >/dev/null 2>&1 && e=enabled
    done
    desc=$(sed -n -e 's/^# *Short-Description: *//p' -e 's/^# *description: *//p' "$f" 2>/dev/null | head -n1 | tr '|' '/')
    echo "$n|$rc|$e|$desc|$msg"
  done ;;
esac
if command -v supervisorctl >/dev/null 2>&1; then echo @@supervisor; supervisorctl status 2>&1; fi
echo @@end
""".replace("INIT_DETECT", INIT_DETECT).replace("SKIP", _SYSV_SKIP)

_GLYPHS = ("●", "*", "○", "×", "↻")


def parse_systemd_units(units: str, files: str = "") -> list[Service]:
    enabled = {}
    for line in files.splitlines():
        p = line.split()
        if len(p) >= 2:
            enabled[p[0]] = p[1]
    out = []
    for line in units.splitlines():
        p = line.split(None, 4)
        # failed/inactive units get a marker: "●" in UTF-8, "*" in the C locale
        if p and p[0] in _GLYPHS:
            p = line.split(None, 5)[1:]
        if len(p) < 4 or not p[0].endswith(".service"):
            continue
        out.append(Service(p[0], p[1], p[2], p[3], p[4] if len(p) > 4 else "", enabled.get(p[0], "")))
    return out


# LSB "status" exit codes
_LSB = {0: ("active", "running"), 1: ("failed", "dead (pid file left)"), 2: ("failed", "dead (lock file left)"),
        3: ("inactive", "stopped")}


def parse_sysv(text: str) -> list[Service]:
    """Lines of "name|status exit code|enabled|description|first line of `status` output"."""
    out = []
    for line in text.splitlines():
        p = line.split("|", 4)
        if len(p) < 3 or not p[1].strip().lstrip("-").isdigit():
            continue
        name, rc, en = p[0].strip(), int(p[1]), p[2].strip()
        desc = p[3].strip() if len(p) > 3 else ""
        msg = p[4].strip() if len(p) > 4 else ""
        active, sub = _LSB.get(rc, ("unknown", f"status code {rc}"))
        if rc == 0 and not msg:                              # one-shot scripts (procps, …)
            sub = "ok"
        if re.search(r"permission denied|not permitted|must be (run as )?root|needs? (to be )?root|root privileges|are you root|only root", msg, re.I):
            active, sub = "unknown", "status needs root"
        elif rc == 4:
            active, sub = "unknown", "status unknown"
        elif rc == 124:
            active, sub = "unknown", "status timed out"
        elif re.search(r"\busage\b", msg, re.I):            # script has no "status" command
            active, sub = "unknown", "no status command"
        out.append(Service(name, "loaded", active, sub, desc or msg, en, "sysv"))
    return out


_RC_STATE = {"started": ("active", "running"), "stopped": ("inactive", "stopped"),
             "crashed": ("failed", "crashed"), "starting": ("active", "starting"),
             "stopping": ("active", "stopping"), "inactive": ("inactive", "inactive"),
             "scheduled": ("inactive", "scheduled"), "failed": ("failed", "failed")}


def parse_openrc(text: str) -> list[Service]:
    """`rc-status --all`: services grouped by runlevel; "Dynamic Runlevel" ones aren't enabled."""
    found: dict[str, Service] = {}
    level, dynamic = "", False
    for line in text.splitlines():
        if line.startswith(("Runlevel:", "Dynamic Runlevel:")):
            dynamic = line.startswith("Dynamic")
            level = line.split(":", 1)[1].strip()
            continue
        m = re.match(r"\s+(\S+)\s+\[\s*(\w+)", line)
        if not m:
            continue
        name, state = m.groups()
        active, sub = _RC_STATE.get(state.lower(), ("unknown", state))
        svc = found.get(name)
        if svc is None:
            svc = found[name] = Service(name, "loaded", active, sub, "", "", "openrc")
        if not dynamic and level not in ("", "manual"):
            svc.enabled = f"enabled ({level})" if level != "default" else "enabled"
    for svc in found.values():
        svc.enabled = svc.enabled or "disabled"
    return list(found.values())


_SUP_STATE = {"RUNNING": ("active", "running"), "STARTING": ("active", "starting"),
              "STOPPING": ("active", "stopping"), "STOPPED": ("inactive", "stopped"),
              "EXITED": ("inactive", "exited"), "BACKOFF": ("failed", "backoff"),
              "FATAL": ("failed", "fatal"), "UNKNOWN": ("unknown", "unknown")}


def parse_supervisor(text: str) -> list[Service]:
    out = []
    for line in text.splitlines():
        p = line.split(None, 2)
        if len(p) >= 2 and p[1] in _SUP_STATE:
            active, sub = _SUP_STATE[p[1]]
            out.append(Service(p[0], "loaded", active, sub, p[2] if len(p) > 2 else "", "", "supervisor"))
    return out


def parse_services(text: str) -> list[Service]:
    s = split_sections(text)
    return (parse_systemd_units(s.get("units", ""), s.get("files", ""))
            + parse_openrc(s.get("openrc", "")) + parse_sysv(s.get("sysv", ""))
            + parse_supervisor(s.get("supervisor", "")))


_CONTAINER_PID1 = ("bash", "sh", "dash", "ash", "tini", "dumb-init", "docker-init", "catatonit", "sleep",
                   "s6-svscan", "runsvdir", "java", "node", "python", "python3")


def services(runner: Runner) -> tuple[list[Service], str]:
    """(services, problem) - problem explains an empty list (or a partial one)."""
    r = runner.run(SERVICES_SCRIPT, timeout=60)
    return services_from_output(r.out + ("\n" + r.err if r.err.strip() and "@@" not in r.out else ""))


def services_from_output(out: str) -> tuple[list[Service], str]:
    s = split_sections(out)
    init, pid1 = s.get("init", "").strip(), s.get("pid1", "").strip()
    svcs = parse_services(out)
    sup = s.get("supervisor", "").strip()
    sup_problem = ""
    if sup and not any(x.init == "supervisor" for x in svcs):
        sup_problem = "supervisorctl: " + sup.splitlines()[-1][:200]
    if svcs:
        return svcs, sup_problem
    if init == "systemd":
        return [], ("systemctl didn't return any services: "
                    + (s.get("units", "").strip()[:300] or "no output"))
    if pid1 in _CONTAINER_PID1 or (pid1 and init == "" and pid1 not in ("init", "systemd")):
        why = (f"This looks like a container: its first process is “{pid1}”, not a service manager, "
               "so there are no services to manage here (manage them on the host).")
    elif init:
        why = f"{INIT_NAMES.get(init, init)} found, but no services were listed."
    else:
        why = ("No service manager found (no systemd, OpenRC, SysV init scripts or supervisord)"
               + (f"; the first process is “{pid1}”." if pid1 else "."))
    return [], why + (f"\n{sup_problem}" if sup_problem else "")


# ---------------------------------------------------------------- processes
@dataclass
class Process:
    pid: int
    user: str
    cpu: float
    mem: float
    rss_kb: int
    elapsed: str
    command: str


def parse_ps(text: str) -> list[Process]:
    out = []
    for line in text.splitlines()[1:]:
        p = line.split(None, 6)
        if len(p) < 7 or not p[0].isdigit():
            continue
        try:
            out.append(Process(int(p[0]), p[1], float(p[2]), float(p[3]), int(p[4]), p[5], p[6]))
        except ValueError:
            continue
    return out


def processes(runner: Runner, sort: str = "cpu", limit: int = 60) -> list[Process]:
    key = "-pcpu" if sort == "cpu" else "-rss"
    r = runner.run(f"ps -eo pid,user,pcpu,pmem,rss,etime,args --sort={key} 2>/dev/null | head -n {limit + 1} "
                   f"|| ps -eo pid,user,pcpu,pmem,rss,etime,args | head -n {limit + 1}")
    return parse_ps(r.out)


# ---------------------------------------------------------------- logs
PRIORITIES = {"all": "", "warnings and worse": "warning", "errors and worse": "err"}


def logs_command(unit: str = "", priority: str = "", lines: int = 200, init: str = "") -> str:
    n = int(lines)
    if init == "supervisor" and unit:
        return f"supervisorctl tail -{n * 200} {shlex.quote(unit)} 2>&1 | tail -n {n}"
    cmd = f"journalctl --no-pager -o short-iso -n {n}"
    if unit:
        cmd += " -u " + shlex.quote(unit)
    if priority:
        cmd += " -p " + shlex.quote(priority)
    # without journald (CentOS 6, containers, OpenRC): the classic syslog files
    files = "/var/log/syslog /var/log/messages"
    if unit:
        name = shlex.quote(unit[:-8] if unit.endswith(".service") else unit)
        fallback = f"grep -h -i -F -- {name} {files} 2>/dev/null | tail -n {n}"
    else:
        fallback = f"cat {files} 2>/dev/null | tail -n {n}"
    hint = ("echo '(No readable system log: journald is not running, and /var/log/messages or "
            "/var/log/syslog is missing or readable by root only.)'")
    return (f"if [ -d /run/systemd/system ] && command -v journalctl >/dev/null 2>&1; then {cmd} 2>&1; "
            f"else out=$({fallback}); if [ -n \"$out\" ]; then printf '%s\\n' \"$out\"; "
            f"elif [ -r /var/log/messages ] || [ -r /var/log/syslog ]; then echo '(No matching log lines.)'; "
            f"else {hint}; fi; fi")


def logs(runner: Runner, unit: str = "", priority: str = "", lines: int = 200, init: str = "") -> str:
    return runner.run(logs_command(unit, priority, lines, init), timeout=30).out


# ---------------------------------------------------------------- ports
@dataclass
class Port:
    proto: str
    address: str
    port: str
    process: str = ""


def parse_ss(text: str) -> list[Port]:
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) < 5 or p[0] not in ("tcp", "udp"):
            continue
        local = p[4]
        addr, _, port = local.rpartition(":")
        proc = ""
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            proc = f"{m.group(1)} ({m.group(2)})"
        out.append(Port(p[0], addr.strip("[]") or "*", port, proc))
    out.sort(key=lambda x: (x.proto, int(x.port) if x.port.isdigit() else 0))
    return out


def parse_netstat(text: str) -> list[Port]:
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].startswith(("tcp", "udp")) and (len(p) < 6 or p[5] == "LISTEN" or p[0].startswith("udp")):
            addr, _, port = p[3].rpartition(":")
            out.append(Port(p[0][:3], addr or "*", port, p[6] if len(p) > 6 else ""))
    return out


def _hex_addr(h: str) -> str:
    """/proc/net address (little-endian hex) -> text."""
    import ipaddress
    import struct
    raw = bytes.fromhex(h)
    if len(raw) == 4:
        return str(ipaddress.IPv4Address(struct.pack("<I", struct.unpack(">I", raw)[0])))
    words = struct.unpack(">4I", raw)
    return str(ipaddress.IPv6Address(b"".join(struct.pack("<I", w) for w in words)))


def parse_proc_net(text: str) -> list[Port]:
    """Fallback for minimal systems without ss/netstat: /proc/net/{tcp,tcp6,udp,udp6}.
    TCP state 0A = LISTEN, UDP state 07 = unconnected (a listening socket)."""
    out, seen = [], set()
    for section, body in split_sections(text).items():
        proto = section[:3]
        for line in body.splitlines()[1:]:
            p = line.split()
            if len(p) < 4 or ":" not in p[1]:
                continue
            if (proto == "tcp" and p[3] != "0A") or (proto == "udp" and p[3] != "07"):
                continue
            addr_hex, port_hex = p[1].split(":")
            try:
                addr = _hex_addr(addr_hex)
            except (ValueError, Exception):
                continue
            port = str(int(port_hex, 16))
            addr = {"0.0.0.0": "*", "::": "*"}.get(addr, addr)
            key = (proto, addr, port)
            if key not in seen:
                seen.add(key)
                out.append(Port(proto, addr, port))
    out.sort(key=lambda x: (x.proto, int(x.port)))
    return out


def ports(runner: Runner) -> list[Port]:
    r = runner.run("ss -tulnpH 2>/dev/null || ss -tulnp 2>/dev/null | tail -n +2")
    if r.out.strip():
        return parse_ss(r.out)
    r = runner.run("netstat -tuln 2>/dev/null")
    if r.out.strip():
        return parse_netstat(r.out)
    return parse_proc_net(runner.run("for f in tcp tcp6 udp udp6; do echo @@$f; cat /proc/net/$f 2>/dev/null; done").out)


# ---------------------------------------------------------------- updates
UPDATES_SCRIPT = r"""
if command -v apt >/dev/null 2>&1; then echo @@apt; apt list --upgradable 2>/dev/null
elif command -v dnf >/dev/null 2>&1; then echo @@dnf; dnf -q check-update 2>/dev/null
elif command -v yum >/dev/null 2>&1; then echo @@yum; yum -q check-update 2>/dev/null
elif command -v zypper >/dev/null 2>&1; then echo @@zypper; zypper -q list-updates 2>/dev/null
elif command -v checkupdates >/dev/null 2>&1; then echo @@pacman; checkupdates 2>/dev/null
elif command -v apk >/dev/null 2>&1; then echo @@apk; apk list -u 2>/dev/null
else echo @@none
fi
"""

UPGRADE_COMMANDS = {
    "apt": "sudo apt update && sudo apt upgrade",
    "dnf": "sudo dnf upgrade",
    "yum": "sudo yum update",
    "zypper": "sudo zypper update",
    "pacman": "sudo pacman -Syu",
    "apk": "sudo apk upgrade",
}

# Non-interactive versions, run as root through `sh -c` (so sudo covers every step). Config
# files the admin edited are kept (apt); prompts never block (the channel has no terminal).
_INSTALL_SCRIPTS = {
    "apt": "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
           "apt-get -y -o Dpkg::Options::=--force-confold upgrade",
    "dnf": "dnf -y upgrade",
    "yum": "yum -y update",
    "zypper": "zypper -n update",
    "pacman": "pacman -Syu --noconfirm",
    "apk": "apk update && apk upgrade",
}


def install_command(manager: str) -> str | None:
    script = _INSTALL_SCRIPTS.get(manager)
    return f"sh -c {shlex.quote(script)}" if script else None


@dataclass
class Update:
    package: str
    version: str = ""


def parse_updates(text: str) -> tuple[str, list[Update]]:
    """(package manager, pending updates). The package lists are the server's cached
    ones: nothing is refreshed or installed (read-only)."""
    s = split_sections(text)
    if not s:
        return "", []
    manager = next(iter(s))
    body = s[manager]
    ups = []
    if manager == "apt":
        for line in body.splitlines():
            m = re.match(r"([^/\s]+)/\S+\s+(\S+)", line)
            if m:
                ups.append(Update(m.group(1), m.group(2)))
    elif manager in ("dnf", "yum"):
        for line in body.splitlines():
            p = line.split()
            if len(p) >= 3 and "." in p[0] and not line.startswith(("Obsoleting", "Last metadata", " ")):
                ups.append(Update(p[0], p[1]))
    elif manager == "zypper":
        for line in body.splitlines():
            p = [x.strip() for x in line.split("|")]
            if len(p) >= 5 and p[0] == "v":
                ups.append(Update(p[2], p[4]))
    elif manager == "pacman":
        for line in body.splitlines():
            p = line.split()
            if len(p) >= 4:
                ups.append(Update(p[0], p[3]))
    elif manager == "apk":
        for line in body.splitlines():
            if line.strip():
                ups.append(Update(line.split()[0]))
    return ("" if manager == "none" else manager), ups


def updates(runner: Runner) -> tuple[str, list[Update]]:
    return parse_updates(runner.run(UPDATES_SCRIPT, timeout=60).out)


# ---------------------------------------------------------------- users
@dataclass
class Account:
    name: str
    uid: int
    home: str
    shell: str
    logged_in: int = 0
    groups: list[str] = field(default_factory=list)     # primary group first
    locked: bool | None = None                           # None: unknown (needs root to read shadow)


USERS_SCRIPT = r"""
echo @@passwd; getent passwd 2>/dev/null || cat /etc/passwd
echo @@group; getent group 2>/dev/null || cat /etc/group
echo @@shadow; getent shadow 2>/dev/null
echo @@who; who 2>/dev/null
"""

# groups worth offering even when their gid is below 1000
COMMON_GROUPS = ("sudo", "wheel", "admin", "docker", "adm", "www-data", "ssh", "systemd-journal", "users")


def parse_groups(text: str) -> list[str]:
    """Group names a person would pick from: regular groups (gid >= 1000) and the usual
    administrative ones (sudo, wheel, docker, ...)."""
    names = []
    for line in text.splitlines():
        p = line.split(":")
        if len(p) >= 3 and p[2].isdigit() and (int(p[2]) >= 1000 and int(p[2]) < 60000 or p[0] in COMMON_GROUPS):
            names.append(p[0])
    return sorted(names)


def parse_users(text: str) -> tuple[list[Account], list[str]]:
    """(login accounts: root + uid >= 1000 with a real shell, current sessions)."""
    s = split_sections(text)
    by_gid: dict[int, str] = {}
    members: dict[str, list[str]] = {}
    for line in s.get("group", "").splitlines():
        p = line.split(":")
        if len(p) < 4 or not p[2].isdigit():
            continue
        by_gid[int(p[2])] = p[0]
        for m in p[3].split(","):
            if m:
                members.setdefault(m, []).append(p[0])
    shadow = {}
    for line in s.get("shadow", "").splitlines():
        p = line.split(":")
        if len(p) >= 2:
            shadow[p[0]] = p[1].startswith("!")
    sessions = [l for l in s.get("who", "").splitlines() if l.strip()]
    counts: dict[str, int] = {}
    for l in sessions:
        counts[l.split()[0]] = counts.get(l.split()[0], 0) + 1
    accounts = []
    for line in s.get("passwd", "").splitlines():
        p = line.split(":")
        if len(p) < 7 or not p[2].isdigit():
            continue
        uid, shell = int(p[2]), p[6]
        if (uid == 0 or 1000 <= uid < 60000) and not shell.endswith(("nologin", "false")):
            primary = by_gid.get(int(p[3])) if p[3].isdigit() else None
            groups = ([primary] if primary else []) + [g for g in members.get(p[0], []) if g != primary]
            accounts.append(Account(p[0], uid, p[5], shell, counts.get(p[0], 0), groups, shadow.get(p[0])))
    return accounts, sessions


def users(runner: Runner) -> tuple[list[Account], list[str], list[str]]:
    out = runner.run(USERS_SCRIPT).out
    accounts, sessions = parse_users(out)
    return accounts, sessions, parse_groups(split_sections(out).get("group", ""))


# account changes (each is run as root through run_privileged)
_USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def valid_username(name: str) -> bool:
    return bool(_USERNAME.fullmatch(name))


def admin_group(groups: list[str]) -> str:
    return next((g for g in ("sudo", "wheel", "admin") if g in groups), "")


def useradd_command(name: str, shell: str = "/bin/bash", groups: list[str] | None = None) -> str:
    extra = f" -G {shlex.quote(','.join(groups))}" if groups else ""
    return f"useradd -m -s {shlex.quote(shell)}{extra} {shlex.quote(name)}"


def userdel_command(name: str, remove_home: bool = False) -> str:
    return f"userdel{' -r' if remove_home else ''} {shlex.quote(name)}"


def lock_command(name: str, lock: bool) -> str:
    return f"usermod {'-L' if lock else '-U'} {shlex.quote(name)}"


def groups_command(name: str, add: list[str], remove: list[str]) -> str | None:
    """Add `name` to some groups and remove it from others, in one go."""
    steps = []
    if add:
        steps.append(f"usermod -aG {shlex.quote(','.join(add))} {shlex.quote(name)}")
    steps += [f"gpasswd -d {shlex.quote(name)} {shlex.quote(g)}" for g in remove]
    return f"sh -c {shlex.quote(' && '.join(steps))}" if steps else None


# ---------------------------------------------------------------- actions
SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable", "disable")


def supported_actions(init: str) -> tuple[str, ...]:
    if init == "supervisor":
        return ("start", "stop", "restart")              # supervisord's autostart lives in its config
    return SERVICE_ACTIONS


def _check_unit(unit: str) -> str:
    if not re.fullmatch(r"[\w@.:\-\\]+", unit):
        raise ValueError(f"unexpected unit name: {unit!r}")
    return shlex.quote(unit)


def service_action_command(action: str, unit: str, init: str = "systemd") -> str:
    """The command for a service action, for whichever service manager runs the server.
    Always a single command, so run_privileged can put sudo in front of it."""
    if action not in supported_actions(init):
        raise ValueError(action)
    u = _check_unit(unit)
    if init == "systemd":
        return f"systemctl {action} {u}"
    if init == "openrc":
        if action in ("enable", "disable"):
            return f"rc-update {'add' if action == 'enable' else 'del'} {u} default"
        return f"rc-service {u} {action}"
    if init == "supervisor":
        return f"supervisorctl {action} {u}"
    if init == "sysv":
        if action in ("enable", "disable"):
            # chkconfig on RHEL/CentOS/SUSE, update-rc.d on Debian/Ubuntu
            on = "on" if action == "enable" else "off"
            script = (f"if command -v chkconfig >/dev/null 2>&1; then chkconfig {u} {on}; "
                      f"else update-rc.d {u} {action}; fi")
            return "sh -c " + shlex.quote(script)
        return f"service {u} {action}"
    raise ValueError(f"unknown service manager: {init!r}")


def status_command(unit: str, init: str = "systemd") -> str:
    u = _check_unit(unit)
    return {"systemd": f"systemctl status --no-pager -l {u}",
            "openrc": f"rc-service {u} status",
            "supervisor": f"supervisorctl status {u}",
            "sysv": f"service {u} status"}.get(init, f"service {u} status") + " 2>&1"


def kill_command(pid: int, force: bool = False) -> str:
    return f"kill {'-KILL' if force else '-TERM'} {int(pid)}"


def needs_password(runner: Runner, root: bool) -> bool:
    """Does sudo need a password here? (never for root, never with NOPASSWD)."""
    if root:
        return False
    return not runner.run("sudo -n true 2>/dev/null").ok


def run_privileged(runner: Runner, command: str, root: bool, password: str | None = None,
                   timeout: float = 30) -> Result:
    """Run `command` as root: directly when logged in as root, else through sudo.
    The password (if any) goes over the SSH channel's stdin; it's never stored."""
    if root:
        return runner.run(command, timeout=timeout)
    if password is None:
        return runner.run(f"sudo -n {command}", timeout=timeout)
    return runner.run(f"sudo -S -p '' {command}", stdin=password + "\n", timeout=timeout)


def human_kb(kb: float) -> str:
    size = float(kb) * 1024
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_uptime(seconds: float) -> str:
    s = int(seconds)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
