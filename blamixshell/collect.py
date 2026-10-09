"""What the server dashboard shows, as plain tables. No Qt: the terminal dashboard (tui_dash.py) and the
CLI use it, built on the same readers and parsers as the desktop dashboard.

Each loader takes a Context (an open connection, plus the sudo password once it is known) and returns a
Table. A loader raises NeedsSudo when the answer needs root and a password has to be asked for first.
"""
from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from typing import Callable

from . import compose, cron, dashboard as d, docker, firewall as fw, loglines, report, security, storage, timers
from . import system as sysinfo


class NeedsSudo(Exception):
    """Reading or changing this needs root, and sudo asks for a password: ask the user, then retry."""


@dataclass
class Table:
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)     # per row: "" | "ok" | "warn" | "bad" | "dim"
    keys: list = field(default_factory=list)            # per row: what actions need (a unit, a PID, ...)
    note: str = ""
    details: list = field(default_factory=list)         # per row: the longer explanation ("" = just the cells)

    def add(self, row: list, style: str = "", key=None, details: str = "") -> None:
        self.rows.append([str(c) for c in row])
        self.styles.append(style)
        self.keys.append(key)
        self.details.append(details)

    def describe(self, i: int) -> str:
        """Everything known about row `i`: its own explanation, or its cells as "column: value" lines."""
        if 0 <= i < len(self.details) and self.details[i]:
            return self.details[i]
        if not 0 <= i < len(self.rows):
            return ""
        return "\n".join(f"{(c or 'Item')}: {v}" for c, v in zip(self.columns, self.rows[i]))


class Context:
    def __init__(self, runner: d.Runner, username: str = "", root: bool = False, ssh_port: int = 22):
        self.runner = runner
        self.username = username
        self.root = root
        self.ssh_port = ssh_port             # the port this connection uses: its firewall rule is never removed
        self.firewall: fw.Firewall | None = None     # what load_firewall last saw (the actions need it)
        self.docker: docker.Containers | None = None  # what the Docker tabs last saw (engine-wide actions)
        self.sudo_pw: str | None = None
        self.opener: Callable[[bool], tuple] | None = None     # opener(interactive) -> (client, chain): the way back in
        self.chain: list = []                        # jump-host connections to close with the client
        self.retry_delays: tuple = ()                # (tests) seconds between reconnect attempts; () = the usual ones

    def alive(self) -> bool:
        """Is the SSH connection still up? (Unknown counts as up: a failed command will tell.)"""
        client = getattr(self.runner, "client", None)
        get = getattr(client, "get_transport", None)
        if get is None:
            return True
        transport = get()
        return bool(transport and transport.is_active())

    def reconnect(self, interactive: bool = False) -> None:
        """Open the connection again (raises when it can't); interactive may ask for a password in the terminal."""
        if self.opener is None:
            raise RuntimeError("This dashboard has no way to reconnect.")
        client, chain = self.opener(interactive)
        self.close()
        self.runner = d.Runner(client)
        self.chain = list(chain)

    def close(self) -> None:
        client = getattr(self.runner, "client", None)
        for c in [client, *self.chain]:
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.chain = []

    def _need_pw(self) -> bool:
        return not self.root and self.sudo_pw is None and d.needs_password(self.runner, self.root)

    def read(self, script: str, needs_root=None, timeout: float = 30) -> str:
        """Run a read-only script; when `needs_root(output)` says it was refused, run it again as root."""
        out = self.runner.run(script, timeout=timeout).out
        if needs_root and needs_root(out) and not self.root:
            if self._need_pw():
                raise NeedsSudo()
            res = d.run_privileged(self.runner, f"sh -c {shlex.quote(script)}", self.root, self.sudo_pw, timeout)
            if res.ok:
                return res.out
        return out

    def run(self, command: str, allow_plain: bool = False, timeout: float = 60) -> d.Result:
        """Run a changing command as root (sudo when needed); `allow_plain` tries it as yourself first."""
        if allow_plain and not self.root:
            plain = self.runner.run(command, timeout=timeout)
            if plain.ok:
                return plain
        if self._need_pw():
            raise NeedsSudo()
        return d.run_privileged(self.runner, command, self.root, self.sudo_pw, timeout)


def _bar(pct: float, width: int = 20) -> str:
    n = max(0, min(width, round(width * pct / 100)))
    return "█" * n + "·" * (width - n)


def _level(v: float | None, warn: float = 75, bad: float = 90) -> str:
    return "" if v is None else "bad" if v >= bad else "warn" if v >= warn else "ok"


# ---------------------------------------------------------------- the tabs
def load_overview(ctx: Context) -> Table:
    ov = d.overview(ctx.runner)
    ctx.root = ctx.root or ov.root
    t = Table(["", "Value", "Detail"])
    t.note = " · ".join(x for x in (ov.host, ov.os, f"kernel {ov.kernel}" if ov.kernel else "",
                                    f"up {d.human_uptime(ov.uptime_s)}" if ov.uptime_s else "") if x)
    if ov.cpu_percent is not None:
        t.add(["CPU", f"{ov.cpu_percent:.0f}%", f"{_bar(ov.cpu_percent)}  {ov.cpus} CPUs"], _level(ov.cpu_percent))
    t.add(["Load", f"{ov.load[0]:.2f}", f"5 min {ov.load[1]:.2f} · 15 min {ov.load[2]:.2f}"],
          _level(ov.load[0] / ov.cpus * 100 if ov.cpus else None, 100, 150))
    if ov.mem_total_kb:
        t.add(["Memory", f"{ov.mem_percent:.0f}%",
               f"{_bar(ov.mem_percent)}  {d.human_kb(ov.mem_used_kb)} of {d.human_kb(ov.mem_total_kb)}"],
              _level(ov.mem_percent))
    if ov.swap_total_kb:
        t.add(["Swap", f"{ov.swap_percent:.0f}%", f"{_bar(ov.swap_percent)}  of {d.human_kb(ov.swap_total_kb)}"],
              _level(ov.swap_percent, 25, 60))
    for dk in ov.disks:
        t.add([dk.mount, f"{dk.percent:.0f}%",
               f"{_bar(dk.percent)}  {d.human_kb(dk.used_kb)} of {d.human_kb(dk.size_kb)}"], _level(dk.percent, 80, 90))
    if ov.failed_units:
        t.add(["Failed services", str(len(ov.failed_units)), ", ".join(ov.failed_units[:6])], "bad")
    elif ov.init == "systemd" and ov.systemd:
        t.add(["Services", "OK", f"systemd {ov.systemd}"], "ok")
    t.add(["Sessions", str(ov.sessions), "logged in now"], "dim")
    return t


def load_services(ctx: Context) -> Table:
    svcs, problem = d.services(ctx.runner)
    t = Table(["Service", "State", "Startup", "Description"], note=problem)
    for s in sorted(svcs, key=lambda x: (not x.failed, x.unit)):
        t.add([s.unit, f"{s.active} ({s.sub})", s.enabled or "–", s.description],
              "bad" if s.failed else "" if s.active == "active" else "dim", key=(s.init, s.unit))
    return t


def load_processes(ctx: Context) -> Table:
    t = Table(["PID", "User", "CPU %", "Mem %", "Memory", "Running for", "Command"])
    for p in d.processes(ctx.runner, "cpu", limit=500):
        t.add([p.pid, p.user, f"{p.cpu:.1f}", f"{p.mem:.1f}", d.human_kb(p.rss_kb), p.elapsed, p.command],
              "warn" if p.cpu >= 50 else "", key=(p.pid, p.command))
    return t


def load_logs(ctx: Context) -> Table:
    text = d.logs(ctx.runner, "", "", 300, "")
    t = Table(["Log (last 300 lines)"])
    for line in text.splitlines():
        t.add([line], {"error": "bad", "warn": "warn"}.get(loglines.severity(line), ""))
    return t


def load_ports(ctx: Context) -> Table:
    t = Table(["Protocol", "Address", "Port", "Process"])
    for p in d.ports(ctx.runner):
        public = p.address in ("*", "0.0.0.0", "::")
        t.add([p.proto.upper(), "all interfaces" if public else p.address, p.port, p.process or "–"],
              "warn" if public else "")
    return t


def load_updates(ctx: Context) -> Table:
    mgr, ups = d.updates(ctx.runner)
    t = Table(["Package", "New version"],
              note=("No supported package manager found." if not mgr else
                    f"Up to date ({mgr}), from the server's cached package lists." if not ups else
                    f"{len(ups)} update{'s' if len(ups) != 1 else ''} available ({mgr})"))
    for u in ups:
        t.add([u.package, u.version or "–"])
    return t


def load_users(ctx: Context) -> Table:
    accounts, sessions, _groups = d.users(ctx.runner)
    t = Table(["Account", "UID", "Groups", "Locked", "Logged in", "Shell", "Home"])
    for a in accounts:
        if a.system:
            continue
        t.add([a.name, a.uid, ", ".join(a.groups) or "–", "locked" if a.locked else ("no" if a.locked is False else "?"),
               a.logged_in or "–", a.shell, a.home], "warn" if a.uid == 0 else "", key=a.name)
    return t


def load_cron(ctx: Context) -> Table:
    body, now, tz = cron.parse_read(ctx.runner.run(cron.read_script("")).out)
    t = Table(["On", "When", "Schedule", "Command"],
              note=(f"Server time {now:%Y-%m-%d %H:%M} {tz}" if now else ""))
    for e in cron.parse_crontab(body):
        if e.kind == "job":
            t.add(["yes" if e.enabled else "off", cron.describe(e.schedule), e.schedule, e.command],
                  "" if e.enabled else "dim")
    if not t.rows:
        t.note = ("No jobs in the crontab. " + t.note).strip()
    return t


def load_firewall(ctx: Context) -> Table:
    out = ctx.read(fw.READ_SCRIPT, needs_root=fw.needs_root, timeout=40)
    f = fw.parse(out)
    ctx.firewall = f
    t = Table(["Where", "Type", "Rule", "Detail"])
    state = {"running": "running", "active": "active", "inactive": "not running"}.get(f.state, f.state or "?")
    t.note = ("No firewall tool found (firewalld, ufw, iptables, nftables)." if f.manager == "none" else
              f"{f.manager}: {state}" + (f", default zone {f.default_zone}" if f.default_zone else "")
              + (" · needs root to read the rules (press S for sudo)" if f.needs_root else "")
              + (f" · {f.why_not_editable}" if f.why_not_editable and not f.needs_root else ""))
    for r in f.rules:
        protected = fw.protects_ssh(r, ctx.ssh_port)
        t.add([r.scope or "–", r.kind, r.value, r.detail or "–"],
              "ok" if r.kind in ("allow", "service", "port") else "bad" if r.kind in ("deny", "reject") else "",
              key=("fw", f.manager, r.kind, r.value, r.scope, r.number, protected),
              details=f"{f.manager}: {r.kind}\n{r.scope + ': ' if r.scope else ''}{r.value}"
                      + (f"\n{r.detail}" if r.detail else "")
                      + ("\n\nThis rule keeps your SSH connection open, so it can't be removed from here." if protected else ""))
    return t


def _docker(ctx: Context) -> docker.Containers:
    out = ctx.read(docker.READ_SCRIPT, needs_root=lambda o: docker.parse(o).needs_access, timeout=60)
    c = docker.parse(out)
    ctx.docker = c
    return c


def load_docker(ctx: Context) -> Table:
    c = _docker(ctx)
    t = Table(["State", "Name", "Image", "Status", "Ports", "CPU", "Memory"])
    if c.engine == "none":
        t.note = "No Docker or Podman found."
    elif c.needs_access:
        t.note = "This account can't talk to the daemon: use root, the docker group, or sudo (press S)."
    else:
        run = sum(1 for x in c.items if x.state == "running")
        totals = c.totals_text()
        t.note = f"{c.engine}: {run} running, {len(c.items) - run} not running" + (f"  ·  {totals}" if totals else "")
    for x in c.items:
        t.add([x.state, x.name, x.image, x.status, x.ports or "–", x.cpu or "–", x.mem or "–"],
              "ok" if x.state == "running" else "warn" if x.state in ("paused", "restarting") else "dim",
              key=(c.engine, x.id, x.name, x.state))
    return t


def load_compose(ctx: Context) -> Table:
    """One row per project, then one per service (indented)."""
    c = _docker(ctx)
    t = Table(["Project / service", "State", "Image", "Folder"])
    found = compose.projects(c.items)
    if c.engine == "none":
        t.note = "No Docker or Podman found."
    elif c.needs_access:
        t.note = "This account can't talk to the daemon: use root, the docker group, or sudo (press S)."
    elif not found:
        t.note = "No compose projects: none of the containers was started by docker compose / docker-compose."
    elif not c.compose:
        t.note = "No compose command on the server (docker compose, docker-compose, podman-compose): view only."
    else:
        t.note = f"Using “{c.compose}”, in each project's own folder. a: actions (up, update, down, edit files …), l: log"
    for p in found:
        key = ("compose", c.compose, p.name, p.folder, tuple(p.files), "")
        t.add([p.name, p.state, "", p.folder or "?"],
              "ok" if p.state == "running" else "warn" if p.running else "dim", key=key,
              details=f"Project {p.name}\nFolder: {p.folder or '?'}\nFiles: {', '.join(p.files) or '?'}\n"
                      f"Services: {', '.join(s.name for s in p.services)}")
        for s in p.services:
            st = s.state
            t.add([f"  {s.name}", st, s.image, ", ".join(x.name for x in s.containers)],
                  "ok" if st.startswith("running") else "warn" if "running" in st else "dim", key=key[:5] + (s.name,))
    return t


def load_images(ctx: Context) -> Table:
    """Images, volumes and networks in one list (the Kind column says which)."""
    c = _docker(ctx)
    t = Table(["Kind", "Name", "Size / driver", "Created / scope", "Used by"])
    if c.engine == "none":
        t.note = "No Docker or Podman found."
        return t
    if c.needs_access:
        t.note = "This account can't talk to the daemon: use root, the docker group, or sudo (press S)."
        return t
    dangling = sum(1 for i in c.images if i.dangling)
    t.note = (f"{len(c.images)} images" + (f" ({dangling} dangling)" if dangling else "")
              + f", {len(c.volumes)} volume{'s' if len(c.volumes) != 1 else ''}, {len(c.networks)} network"
              + f"{'s' if len(c.networks) != 1 else ''}. a: details, layers, pull, remove, clean up")
    for i in c.images:
        t.add(["image", f"{i.repository}:{i.tag}" if not i.dangling else f"<none> {i.id}", i.size, i.created,
               ", ".join(i.used_by) or "–"], "dim" if i.dangling else "ok" if i.used_by else "",
              key=("image", c.engine, i.ref, tuple(i.used_by), i.dangling))
    for v in c.volumes:
        t.add(["volume", v.name, v.driver, "", "–"], "", key=("volume", c.engine, v.name))
    for n in c.networks:
        t.add(["network", n.name, n.driver, n.scope, ""], "dim" if n.builtin else "", key=("network", c.engine, n.name))
    return t


def load_timers(ctx: Context) -> Table:
    t = Table(["On", "Timer", "Runs", "Schedule", "Next", "Last"])
    for x in timers.parse(ctx.runner.run(timers.READ_SCRIPT, timeout=30).out):
        on = x.active == "active"
        t.add(["yes" if on else "no", x.name, x.unit or "–", x.schedule or "–", x.next, x.last],
              "" if on else "dim", key=(x.name, x.unit, on))
    if not t.rows:
        t.note = "No systemd timers found (or this server doesn't run systemd)."
    return t


def load_storage(ctx: Context) -> Table:
    t = Table(["Mounted on", "Type", "Size", "Used", "Free", "Use", "Inodes"])
    for f in storage.parse_filesystems(ctx.runner.run(storage.FS_SCRIPT, timeout=20).out):
        if f.virtual:
            continue
        t.add([f.mount, f.fstype, d.human_kb(f.size_kb), d.human_kb(f.used_kb), d.human_kb(f.avail_kb),
               f"{f.percent:.0f}%", "–" if f.inode_percent is None else f"{f.inode_percent:.0f}%"],
              _level(max(f.percent, f.inode_percent or 0), 80, 90))
    return t


def load_security(ctx: Context) -> Table:
    out = ctx.read(security.READ_SCRIPT, needs_root=lambda o: security.needs_root(security.parse(o)), timeout=60)
    found = security.parse(out)
    t = Table(["", "Check", "Result", "Advice"])
    style = {security.OK: "ok", security.WARN: "warn", security.BAD: "bad", security.UNKNOWN: "dim"}
    sign = {security.OK: "ok", security.WARN: "warn", security.BAD: "BAD", security.UNKNOWN: "?"}
    for x in found:
        t.add([sign[x.level], x.title, x.result, x.advice or "–"], style[x.level], details=x.text())
    if security.needs_root(found) and not ctx.root:
        t.note = "Some checks need root (press S to give the sudo password and run them again)."
    return t


def load_system(ctx: Context) -> Table:
    t0 = time.time()
    out = ctx.runner.run(sysinfo.SYSTEM_SCRIPT, timeout=20).out
    t1 = time.time()
    i = sysinfo.parse_system(out)
    t = Table(["", "Value", "Detail"])
    t.add(["Host", i.hostname or "–", i.os or ""], "")
    t.add(["Kernel", i.kernel or "–", f"up {d.human_uptime(i.uptime_s)}" if i.uptime_s else ""], "")
    tz = " ".join(x for x in (i.tz_name, f"({i.tz_abbr} {i.tz_offset})" if i.tz_abbr else "") if x)
    t.add(["Time zone", tz or "–", "double-click to change"], "", key=("tz", i.tz_name))
    drift = i.drift_s((t0 + t1) / 2)
    t.add(["Server time", i.local_time or "–",
           ("" if drift is None else f"{'ahead of' if drift >= 0 else 'behind'} this computer by {abs(drift):.0f} s")],
          "" if drift is None or abs(drift) <= 5 else "warn" if abs(drift) <= 60 else "bad")
    ntp = ("synchronized" if i.ntp_synced else "not synchronized" if i.ntp_synced is False else "unknown")
    svc = ("service running" if i.ntp_active else "service off" if i.ntp_active is False else "")
    t.add(["Clock sync (NTP)", ntp, svc], "ok" if i.ntp_synced else "warn" if i.ntp_synced is False else "dim")
    t.add(["Reboot required", "yes" if i.reboot_required else "no" if i.reboot_required is False else "unknown", ""],
          "warn" if i.reboot_required else "dim" if i.reboot_required is None else "")
    if i.scheduled:
        t.add(["Scheduled", "shutdown or reboot", i.scheduled.replace("\n", " ")[:80]], "warn")
    if i.mem_total_kb:
        t.add(["Memory", d.human_kb(i.mem_total_kb), ""], "")
    if i.swaps:
        for s in i.swaps:
            pct = 100.0 * s.used_kb / s.size_kb if s.size_kb else 0
            t.add(["Swap", f"{d.human_kb(s.size_kb)} ({s.kind})", f"{s.name}  ·  {pct:.0f}% used"],
                  "warn" if pct >= 60 else "", key=("swap", s.name, s.kind))
    else:
        t.add(["Swap", "none", "add a swap file from the actions"], "warn" if i.mem_total_kb and i.mem_total_kb < 2_000_000 else "dim")
    return t


def load_mounts(ctx: Context) -> Table:
    out = ctx.runner.run(sysinfo.MOUNTS_SCRIPT, timeout=20).out
    t = Table(["Device", "Mounted on", "Type", "Options", "Mounted"], note="From /etc/fstab, compared with what is mounted now.")
    for e in sysinfo.parse_mounts(out):
        state = "–" if e.mounted is None else "yes" if e.mounted else "NO"
        style = "dim" if e.mounted is None else "" if e.mounted else ("dim" if "noauto" in e.options else "warn")
        t.add([e.device, e.mount, e.fstype, e.options, state], style,
              key=("mount", e.mount, e.mounted, e.fstype))
    if not t.rows:
        t.note = "No entries in /etc/fstab (or it can't be read)."
    return t


def load_network(ctx: Context) -> Table:
    n = sysinfo.parse_network(ctx.runner.run(sysinfo.NETWORK_SCRIPT, timeout=20).out)
    t = Table(["Type", "Item", "Value", "Detail"])
    if not n.has_ip_tool:
        t.note = "The `ip` command isn't installed on this server: only the name servers are shown."
    t.add(["Hostname", n.hostname or "–", "", ""])
    for i in n.interfaces:
        up = i.state == "UP"
        detail = "  ·  ".join(x for x in (", ".join(i.addresses), i.mac, f"MTU {i.mtu}" if i.mtu else "") if x)
        t.add(["Interface", i.name, i.state or "–", detail or "–"], "ok" if up else "dim", key=("net", i.name))
    if n.gateway:
        t.add(["Gateway", "default route", n.gateway, ""], "")
    for r in n.routes:
        t.add(["Route", r.split()[0], r, ""], "dim")
    for ns in n.dns:
        t.add(["DNS", "name server", ns, ""], "")
    if n.search:
        t.add(["DNS", "search domains", " ".join(n.search), ""], "dim")
    return t


TABS: list[tuple[str, str, object]] = [
    ("overview", "Overview", load_overview), ("services", "Services", load_services),
    ("processes", "Processes", load_processes), ("logs", "Logs", load_logs), ("ports", "Ports", load_ports),
    ("updates", "Updates", load_updates), ("users", "Users", load_users), ("cron", "Cron", load_cron),
    ("firewall", "Firewall", load_firewall), ("docker", "Docker", load_docker), ("compose", "Compose", load_compose),
    ("images", "Images", load_images), ("timers", "Timers", load_timers),
    ("storage", "Storage", load_storage), ("mounts", "Mounts", load_mounts), ("system", "System", load_system),
    ("network", "Network", load_network), ("security", "Security", load_security),
]
LOADERS = {key: fn for key, _title, fn in TABS}


# ---------------------------------------------------------------- what can be done to a row
@dataclass
class Action:
    label: str
    command: str = ""
    allow_plain: bool = False        # try it as yourself first (your own processes need no sudo)
    danger: bool = False
    drops: str = ""                  # "reboot" / "shutdown": the SSH connection is expected to go away
    prompt: str = ""                 # ask for a value first (shown as the question) ...
    placeholder: str = ""
    make: Callable[[str], str] | None = None      # ... and build the command from it (ValueError: wrong value)
    readonly: bool = False           # only looks: run it as yourself and show the output, no confirmation
    picker: str = ""                 # a chooser instead of the prompt ("timezone"; "compose-edit": edit the files)
    timeout: float = 60              # how long it may take (pulling images takes a while)
    root_if_refused: bool = False    # readonly: run it again with sudo when the daemon refuses this account
    shape: Callable[[str], str] | None = None     # readonly: turns the output into something readable
    target: object = None            # what the chooser works on (picker)

    def command_for(self, value: str = "") -> str:
        """The command to run (a ValueError says what is wrong with `value`)."""
        return self.make(value) if self.make else self.command


def _open_port_command(f: fw.Firewall, text: str) -> str:
    port, proto = fw.parse_port_proto(text)
    return fw.add_port_command(f.manager, fw.normalize_port(port, f.manager), proto, f.default_zone,
                               fw.nft_input_chain(f.rules))


def actions_for(tab: str, key, ctx: Context | None = None) -> list[Action]:
    """The things the selected row can be given (each is confirmed before it runs). `ctx` is needed for
    the firewall tab, whose actions depend on which firewall the server runs."""
    out: list[Action] = []
    if tab == "firewall":
        f = getattr(ctx, "firewall", None)
        if f is None:
            return out
        if f.can_start:
            out.append(Action("Start the firewall (allows SSH first)", fw.start_command(f.manager, ctx.ssh_port)))
        if f.editable:
            out.append(Action("Open a port…", prompt="Port and protocol (like 8080/tcp or 8000-8100/udp)",
                              placeholder="8080/tcp", make=lambda v, f=f: _open_port_command(f, v)))
            if key and key[0] == "fw" and not key[6]:
                _k, manager, kind, value, scope, number, _p = key
                cmd = fw.remove_command(manager, fw.Rule(scope, kind, value, "", number))
                if cmd:
                    out.append(Action(f"Remove the rule {value}", cmd, danger=True))
            cmd = fw.reload_command(f.manager) or fw.save_command(f.manager)
            if cmd:
                out.append(Action(fw.action_label(f.manager), cmd))
        return out
    if tab == "services" and key:
        init, unit = key
        for a in d.supported_actions(init):
            out.append(Action(f"{a.capitalize()} {unit}", d.service_action_command(a, unit, init),
                              danger=a in ("stop", "disable")))
    elif tab == "processes" and key:
        pid, command = key
        out.append(Action(f"End process {pid} ({command[:40]})", d.kill_command(pid, False), allow_plain=True))
        out.append(Action(f"Force-kill process {pid}", d.kill_command(pid, True), allow_plain=True, danger=True))
    elif tab == "docker" and key:
        engine, cid, name, state = key
        run = state in ("running", "paused", "restarting")
        if state == "paused":
            out.append(Action(f"Resume {name}", docker.action_command(engine, "unpause", cid), allow_plain=True))
        if not run:
            out.append(Action(f"Start {name}", docker.action_command(engine, "start", cid), allow_plain=True))
        if run:
            out.append(Action(f"Stop {name}", docker.action_command(engine, "stop", cid), allow_plain=True,
                              danger=True))
            out.append(Action(f"Restart {name}", docker.action_command(engine, "restart", cid), allow_plain=True))
        out.append(Action(f"Remove {name}" + (" (running: forced)" if run else ""),
                          docker.action_command(engine, "remove", cid, force=run), allow_plain=True, danger=True))
        out.append(Action(f"Details of {name} (why it stopped, OOM, health, limits, mounts …)",
                          docker.inspect_command(engine, "container", cid), readonly=True, root_if_refused=True,
                          shape=docker.summarize_inspect))
        if state == "running":
            out.append(Action(f"Processes in {name}", docker.top_command(engine, cid), readonly=True,
                              root_if_refused=True))
    if tab in ("docker", "images"):
        seen = getattr(ctx, "docker", None)            # what the tab last loaded (also when no row is selected)
        engine = seen.engine if seen is not None else (key[1] if tab == "images" else key[0]) if key else ""
        out += _engine_actions(engine)
    if tab == "images" and key:
        out = _image_actions(key) + out
    if tab == "compose" and key:
        out += _compose_actions(key)
    elif tab == "system":
        out += [Action("Reboot now", sysinfo.reboot_command(), danger=True, drops="reboot"),
                Action("Reboot in a while…", prompt="Reboot in how many minutes?", placeholder="5",
                       make=lambda v: sysinfo.reboot_command(sysinfo.minutes_from(v)), danger=True),
                Action("Shut down now", sysinfo.shutdown_command(), danger=True, drops="shutdown"),
                Action("Shut down in a while…", prompt="Shut down in how many minutes?", placeholder="5",
                       make=lambda v: sysinfo.shutdown_command(sysinfo.minutes_from(v)), danger=True),
                Action("Cancel a scheduled reboot or shutdown", sysinfo.cancel_shutdown_command()),
                Action("Set the time zone…", prompt="Time zone (like Europe/Bucharest or UTC)", placeholder="Europe/Bucharest",
                       make=sysinfo.timezone_command, picker="timezone"),
                Action("Turn clock sync (NTP) on", sysinfo.ntp_command(True)),
                Action("Turn clock sync (NTP) off", sysinfo.ntp_command(False)),
                Action("Add a swap file…", prompt="Size of the swap file (like 2G or 512M)", placeholder="2G",
                       make=lambda v: sysinfo.swap_create_command(sysinfo.swap_size_mb(v)))]
        if key and key[0] == "swap" and key[2] == "file":
            out.append(Action(f"Remove the swap file {key[1]}", sysinfo.swap_remove_command(key[1]), danger=True))
    elif tab == "mounts":
        if key and key[0] == "mount" and key[1] not in ("none", "swap"):
            _kind, mp, mounted, _fs = key
            if mounted is False:
                out.append(Action(f"Mount {mp}", sysinfo.mount_command(mp)))
            elif mounted:
                try:
                    out.append(Action(f"Unmount {mp}", sysinfo.umount_command(mp), danger=True))
                except ValueError:
                    pass                     # the system needs it: not offered
        out.append(Action("Check /etc/fstab (what mount -a would do)", sysinfo.verify_fstab_command(), readonly=True))
    elif tab == "network":
        out += [Action("Check a host…", prompt="Host or host:port to test from the server", placeholder="example.com:443",
                       make=lambda v: sysinfo.check_command(*sysinfo.parse_target(v)), readonly=True),
                Action("Check the internet (1.1.1.1:443)", sysinfo.check_command("1.1.1.1", 443), readonly=True),
                Action("Check name lookup (example.com)", sysinfo.check_command("example.com"), readonly=True)]
    elif tab == "timers" and key:
        name, unit, on = key
        out.append(Action(f"{'Disable' if on else 'Enable'} {name}", timers.toggle_command(name, not on)))
        if unit:
            out.append(Action(f"Run {unit} now", timers.run_now_command(unit)))
    return out


def _engine_actions(engine: str) -> list[Action]:
    """Disk use, events, engine info and the clean-ups: they don't depend on the selected row."""
    if engine not in ("docker", "podman"):
        return []
    out = [Action("Disk use (images, containers, volumes, build cache)", docker.disk_usage_command(engine),
                  readonly=True, root_if_refused=True, timeout=120),
           Action("Events of the last hour (died, OOM, restarts, pulls)", docker.events_command(engine),
                  readonly=True, root_if_refused=True),
           Action("Engine info", docker.info_command(engine), readonly=True, root_if_refused=True)]
    for what, (_cmd, label) in docker.PRUNE.items():
        if what != "build-cache" or engine == "docker":
            out.append(Action(f"Clean up: {label[0].lower() + label[1:]}", docker.prune_command(engine, what),
                              allow_plain=True, danger=True, timeout=600))
    return out


def _image_actions(key) -> list[Action]:
    kind, engine = key[0], key[1]
    if kind == "image":
        _k, _e, ref, used_by, dangling = key
        out = [Action(f"Details of {ref}", docker.inspect_command(engine, "image", ref), readonly=True,
                      root_if_refused=True, shape=docker.summarize_inspect),
               Action(f"Layers of {ref}", docker.history_command(engine, ref), readonly=True, root_if_refused=True)]
        if not dangling:
            out.append(Action(f"Pull {ref} again (newer version of the tag)", docker.pull_command(engine, ref),
                              allow_plain=True, timeout=900))
        if not used_by:
            out.append(Action(f"Remove the image {ref}", docker.remove_image_command(engine, ref), allow_plain=True,
                              danger=True))
        return out
    name = key[2]
    out = [Action(f"Details of the {kind} {name}", docker.inspect_command(engine, kind, name), readonly=True,
                  root_if_refused=True, shape=docker.summarize_inspect)]
    if kind == "volume":
        out.append(Action(f"Remove the volume {name} (its data is deleted)", docker.remove_volume_command(engine, name),
                          allow_plain=True, danger=True))
    elif name not in docker.BUILTIN_NETWORKS:
        out.append(Action(f"Remove the network {name}", docker.remove_network_command(engine, name),
                          allow_plain=True, danger=True))
    return out


def compose_project(key) -> compose.Project:
    _k, _tool, name, folder, files, _service = key
    return compose.Project(name, folder, list(files))


_COMPOSE_LABELS = {"up": "Up {n}: start it, creating what is missing", "stop": "Stop {n}",
                   "start": "Start {n} (its stopped containers)", "restart": "Restart {n}",
                   "pull": "Pull newer images for {n}", "update": "Update {n}: pull newer images, re-create what changed",
                   "down": "Down {n}: remove its containers and networks (volumes are kept)"}


def _compose_actions(key) -> list[Action]:
    tool, service = key[1], key[5]
    p = compose_project(key)
    if not tool or not p.manageable:
        return []
    try:
        if service:
            return [Action(f"Restart {p.name} / {service}", compose.action_command(tool, p, "restart", service),
                           allow_plain=True, timeout=300),
                    Action(f"Re-create {p.name} / {service}", compose.recreate_command(tool, p, service),
                           allow_plain=True, timeout=900),
                    Action(f"Log of {p.name} / {service}", compose.logs_command(tool, p, service), readonly=True,
                           root_if_refused=True)]
        out = [Action(label.format(n=p.name), compose.action_command(tool, p, a), allow_plain=True,
                      danger=a == "down", timeout=1800 if a in ("up", "update", "pull") else 300)
               for a, label in _COMPOSE_LABELS.items()]
        out += [Action(f"Status of {p.name} (compose ps)", compose.ps_command(tool, p), readonly=True,
                       root_if_refused=True),
                Action(f"Check the configuration of {p.name}", compose.config_command(tool, p), readonly=True,
                       root_if_refused=True),
                Action(f"Edit the compose files of {p.name}…", picker="compose-edit", target=key)]
        return out
    except ValueError:
        return []


def log_command(tab: str, key) -> str | None:
    """A command showing the log of the selected row (a service's journal, a container's output)."""
    if tab == "services" and key:
        init, unit = key
        return d.logs_command(unit, "", 300, init)
    if tab == "docker" and key:
        return docker.logs_command(key[0], key[1], 300)
    if tab == "compose" and key and key[1]:
        try:
            return compose.logs_command(key[1], compose_project(key), key[5])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- the whole report
def collect_report(ctx: Context) -> tuple[dict, dict]:
    """(data, errors) for report.build(): every part is tried on its own, so one failure doesn't empty it."""
    data: dict = {}
    errs: dict = {}

    def grab(key, fn):
        try:
            data[key] = fn()
        except NeedsSudo:
            errs[key] = "needs root"
        except Exception as e:
            errs[key] = str(e) or e.__class__.__name__

    def jobs():
        body, _now, _tz = cron.parse_read(ctx.runner.run(cron.read_script("")).out)
        return [e for e in cron.parse_crontab(body) if e.kind == "job"]
    grab("overview", lambda: d.overview(ctx.runner))
    grab("services", lambda: d.services(ctx.runner))
    grab("updates", lambda: d.updates(ctx.runner))
    grab("ports", lambda: d.ports(ctx.runner))
    grab("users", lambda: d.users(ctx.runner))
    grab("cron", jobs)
    grab("firewall", lambda: fw.parse(ctx.runner.run(fw.READ_SCRIPT, timeout=20).out))
    grab("system", lambda: sysinfo.parse_system(ctx.runner.run(sysinfo.SYSTEM_SCRIPT, timeout=20).out))
    return data, errs


def build_report(label: str, address: str, ctx: Context, as_json: bool = False):
    """The Markdown report, or with as_json the same parts as a dict (report.as_data)."""
    from datetime import datetime
    data, errs = collect_report(ctx)
    if as_json:
        return report.as_data(label, address, datetime.now(), data, errs)
    return report.build(label, address, datetime.now(), data, errs)
