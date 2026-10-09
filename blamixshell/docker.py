"""Docker / Podman tab helpers: list containers with their CPU and memory, and build the
commands to start, stop, restart, remove them or read their logs. No Qt here."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field

READ_SCRIPT = r"""
for e in docker podman; do
  if command -v $e >/dev/null 2>&1; then
    echo @@engine; echo $e
    echo @@ps; $e ps -a --format '{{json .}}' 2>&1
    echo @@stats; $e stats --no-stream --format '{{json .}}' 2>&1
    echo @@images; $e images --format '{{json .}}' 2>&1
    echo @@volumes; $e volume ls --format '{{json .}}' 2>&1
    echo @@networks; $e network ls --format '{{json .}}' 2>&1
    echo @@host; nproc 2>/dev/null; grep -m1 MemTotal /proc/meminfo 2>/dev/null
    echo @@compose
    if $e compose version >/dev/null 2>&1; then echo "$e compose"
    elif command -v $e-compose >/dev/null 2>&1; then echo "$e-compose"
    fi
    exit 0
  fi
done
echo @@engine; echo none
"""

_NEEDS_ACCESS = re.compile(r"permission denied|cannot connect to the docker daemon|connect: no such file|"
                           r"got permission denied|dial unix|is the docker daemon running", re.I)


@dataclass
class Container:
    id: str
    name: str
    image: str
    status: str
    state: str                 # running | exited | paused | restarting | created | dead
    ports: str = ""
    cpu: str = ""
    mem: str = ""
    labels: dict = field(default_factory=dict)

    @property
    def project(self) -> str:
        """The compose project it belongs to ("" when it wasn't started by compose)."""
        return self.labels.get("com.docker.compose.project", "")

    @property
    def service(self) -> str:
        return self.labels.get("com.docker.compose.service", "")

    @property
    def running(self) -> bool:
        return self.state in ("running", "restarting", "paused")


@dataclass
class Image:
    id: str
    repository: str
    tag: str
    size: str = ""
    created: str = ""
    used_by: list[str] = field(default_factory=list)      # names of the containers made from it

    @property
    def ref(self) -> str:
        """What to call it in commands: repo:tag, or the ID for an untagged (dangling) image."""
        if self.repository and self.repository != "<none>" and self.tag and self.tag != "<none>":
            return f"{self.repository}:{self.tag}"
        return self.id

    @property
    def dangling(self) -> bool:
        return self.repository in ("", "<none>")


@dataclass
class Volume:
    name: str
    driver: str = ""
    mountpoint: str = ""


@dataclass
class Network:
    id: str
    name: str
    driver: str = ""
    scope: str = ""

    @property
    def builtin(self) -> bool:
        """The engine's own networks can't (and mustn't) be removed."""
        return self.name in BUILTIN_NETWORKS


BUILTIN_NETWORKS = ("bridge", "host", "none", "podman")


@dataclass
class Containers:
    engine: str = "none"
    items: list[Container] | None = None
    needs_access: bool = False
    error: str = ""
    compose: str = ""                         # the compose command on the server: "docker compose", "docker-compose" …
    cpus: int = 0                             # the server's CPUs and memory (to put the totals in proportion)
    mem_total: int = 0                        # bytes
    images: list[Image] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)

    def __post_init__(self):
        if self.items is None:
            self.items = []

    def totals(self) -> tuple[float, int]:
        """(CPU % summed over the running containers, as docker counts it: 100 % = one CPU; memory in bytes)."""
        cpu = sum(_percent(c.cpu) for c in self.items if c.running)
        mem = sum(size_bytes(c.mem) for c in self.items if c.running)
        return cpu, mem

    def totals_text(self) -> str:
        """"CPU 37 % of 4 CPUs (9 % of the server) · RAM 1.4 GB (18 % of 7.8 GB)", or "" without stats."""
        from .dashboard import human_kb
        if not any(c.cpu or c.mem for c in self.items):
            return ""
        cpu, mem = self.totals()
        parts = [f"CPU {cpu:.1f} %" + (f" of {self.cpus} CPU{'s' if self.cpus != 1 else ''} ({cpu / self.cpus:.1f} % of "
                                       "the server)" if self.cpus else "")]
        parts.append(f"RAM {human_kb(mem / 1024)}" + (f" ({100 * mem / self.mem_total:.0f} % of "
                                                      f"{human_kb(self.mem_total / 1024)})" if self.mem_total else ""))
        return "  ·  ".join(parts)


def state_of(status: str, state: str = "") -> str:
    s = (state or "").lower()
    if s in ("running", "exited", "paused", "restarting", "created", "dead"):
        return s
    t = status.lower()
    for key, name in (("up", "running"), ("exited", "exited"), ("created", "created"), ("restarting", "restarting"),
                      ("dead", "dead")):
        if t.startswith(key):
            return "paused" if "(paused)" in t else name
    return s or "unknown"


def _json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def parse(text: str) -> Containers:
    from .dashboard import split_sections
    s = split_sections(text)
    engine = s.get("engine", "none").strip() or "none"
    result = Containers(engine=engine)
    if engine == "none":
        return result
    ps = s.get("ps", "")
    if _NEEDS_ACCESS.search(ps) and not _json_lines(ps):
        result.needs_access = True
        result.error = ps.strip().splitlines()[0][:200] if ps.strip() else ""
        return result
    stats: dict[str, dict] = {}
    for row in _json_lines(s.get("stats", "")):
        for key in (row.get("ID") or row.get("Container") or "", row.get("Name") or ""):
            if key:
                stats[key] = row
    for row in _json_lines(ps):
        cid = (row.get("ID") or row.get("Id") or "")[:12]
        names = row.get("Names") or row.get("Name") or ""
        name = (names[0] if isinstance(names, list) else str(names)).lstrip("/")
        st = stats.get(cid) or stats.get(name) or {}
        status = row.get("Status", "")
        result.items.append(Container(
            cid, name, row.get("Image", ""), status, state_of(status, row.get("State", "")),
            row.get("Ports", "") if isinstance(row.get("Ports", ""), str) else "",
            st.get("CPUPerc", ""), (st.get("MemUsage", "") or "").split(" / ")[0], parse_labels(row.get("Labels"))))
    rank = {"running": 0, "restarting": 1, "paused": 2}
    result.items.sort(key=lambda c: (rank.get(c.state, 3), c.name))
    result.images = _parse_images(s.get("images", ""), result.items)
    host = s.get("host", "").split()
    if host and host[0].isdigit():
        result.cpus = int(host[0])
    if "MemTotal:" in host:
        i = host.index("MemTotal:")
        if i + 1 < len(host) and host[i + 1].isdigit():
            result.mem_total = int(host[i + 1]) * 1024
    tool = s.get("compose", "").strip()
    result.compose = tool if tool in COMPOSE_TOOLS else ""
    result.volumes = [Volume(r.get("Name", ""), r.get("Driver", ""), r.get("Mountpoint", ""))
                      for r in _json_lines(s.get("volumes", "")) if r.get("Name")]
    result.networks = [Network((r.get("ID") or r.get("Id") or "")[:12], r.get("Name", ""), r.get("Driver", ""),
                               r.get("Scope", "")) for r in _json_lines(s.get("networks", "")) if r.get("Name")]
    return result


_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3, "gib": 1024 ** 3,
          "tb": 1000 ** 4, "tib": 1024 ** 4}


def size_bytes(text: str) -> int:
    """"12.3MiB", "1.2GB", "512kB", "0B" (docker stats) -> bytes; 0 when it can't be read."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]*)\s*", text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 0))
    except ValueError:
        return 0


def _percent(text: str) -> float:
    try:
        return float((text or "").strip().rstrip("%") or 0)
    except ValueError:
        return 0.0


COMPOSE_TOOLS = ("docker compose", "docker-compose", "podman compose", "podman-compose")


def parse_labels(value) -> dict:
    """docker gives "a=1,b=2" (and a value may itself hold commas: compose's list of files), podman a dict."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    out: dict[str, str] = {}
    last = None
    for part in str(value or "").split(","):
        key, sep, val = part.partition("=")
        if sep and key and " " not in key:
            out[key] = val
            last = key
        elif last is not None:                    # "/srv/app/a.yml,/srv/app/b.yml": the comma was in the value
            out[last] += "," + part
    return out


def _parse_images(text: str, containers: list[Container]) -> list[Image]:
    out = []
    for r in _json_lines(text):
        iid = (r.get("ID") or r.get("Id") or "").replace("sha256:", "")[:12]
        repo = r.get("Repository", "")
        tag = r.get("Tag", "")
        if not repo and isinstance(r.get("Names"), list) and r["Names"]:          # podman's other shape
            repo, _, tag = r["Names"][0].rpartition(":")
        size = r.get("Size", "")
        if isinstance(size, (int, float)):                                        # podman: bytes
            from .dashboard import human_kb
            size = human_kb(size / 1024)
        img = Image(iid, repo or "<none>", tag or "<none>", str(size), r.get("CreatedSince") or r.get("CreatedAt", ""))
        names = {img.ref, iid, f"{repo}:{tag}"} | ({repo} if tag == "latest" else set())
        img.used_by = [c.name for c in containers if c.image in names or (iid and c.image.startswith(iid))]
        out.append(img)
    out.sort(key=lambda i: (i.dangling, i.repository.lower(), i.tag))
    return out


# ---------------------------------------------------------------- commands
ACTIONS = {"start": "start", "stop": "stop", "restart": "restart", "pause": "pause", "unpause": "unpause",
           "remove": "rm"}


def action_command(engine: str, action: str, target: str, force: bool = False) -> str:
    if engine not in ("docker", "podman") or action not in ACTIONS:
        raise ValueError((engine, action))
    verb = ACTIONS[action] + (" -f" if action == "remove" and force else "")
    return f"{engine} {verb} {shlex.quote(target)}"


def logs_command(engine: str, target: str, lines: int = 300) -> str:
    return f"{engine} logs --tail {int(lines)} {shlex.quote(target)} 2>&1"


# ---------------------------------------------------------------- troubleshooting (all read-only)
KINDS = ("container", "image", "volume", "network")


def _check(engine: str) -> None:
    if engine not in ("docker", "podman"):
        raise ValueError(engine)


def inspect_command(engine: str, kind: str, target: str) -> str:
    _check(engine)
    if kind not in KINDS:
        raise ValueError(kind)
    return f"{engine} {kind} inspect {shlex.quote(target)} 2>&1"


def top_command(engine: str, target: str) -> str:
    """The processes running inside a container."""
    _check(engine)
    return f"{engine} top {shlex.quote(target)} 2>&1"


def history_command(engine: str, image: str) -> str:
    """How an image was built, layer by layer (with each layer's size)."""
    _check(engine)
    return f"{engine} history {shlex.quote(image)} 2>&1"


def info_command(engine: str) -> str:
    _check(engine)
    return f"{engine} info 2>&1"


def disk_usage_command(engine: str) -> str:
    """What images, containers, volumes and the build cache take, and how much of it could be freed."""
    _check(engine)
    return f"{engine} system df -v 2>&1"


def events_command(engine: str, minutes: int = 60) -> str:
    """What happened lately: containers that died, were killed (OOM), restarted, images pulled ..."""
    _check(engine)
    m = max(1, int(minutes))
    if engine == "podman":
        return f"podman events --since {m}m --stream=false 2>&1 | tail -n 500"
    return f"docker events --since {m}m --until \"$(date +%s)\" 2>&1 | tail -n 500"


def shell_command(engine: str, target: str, sudo: bool = False) -> str:
    """Typed into the terminal (not run from the dashboard): an interactive shell inside the container, bash when
    it has one."""
    _check(engine)
    inner = shlex.quote("command -v bash >/dev/null 2>&1 && exec bash || exec sh")
    return f"{'sudo ' if sudo else ''}{engine} exec -it {shlex.quote(target)} sh -c {inner}"


# ---------------------------------------------------------------- cleaning up (each one asks first)
PRUNE = {
    "containers": ("container prune -f", "Delete all stopped containers"),
    "images": ("image prune -f", "Delete dangling images (untagged, used by no container)"),
    "unused-images": ("image prune -a -f", "Delete every image no container uses (they are downloaded again "
                                            "when needed)"),
    "volumes": ("volume prune -f", "Delete volumes no container uses (their data is lost)"),
    "networks": ("network prune -f", "Delete networks no container uses"),
    "build-cache": ("builder prune -f", "Delete the build cache"),
}


def prune_command(engine: str, what: str) -> str:
    _check(engine)
    if what not in PRUNE or (what == "build-cache" and engine != "docker"):
        raise ValueError(what)
    return f"{engine} {PRUNE[what][0]}"


def remove_image_command(engine: str, image: str, force: bool = False) -> str:
    _check(engine)
    return f"{engine} rmi {'-f ' if force else ''}{shlex.quote(image)}"


def pull_command(engine: str, image: str) -> str:
    """Download the image again (a newer version of the same tag, if there is one)."""
    _check(engine)
    if image.startswith("<none>") or not re.fullmatch(r"[\w.\-/:@]+", image):
        raise ValueError("Only a named image (repository:tag) can be pulled.")
    return f"{engine} pull {shlex.quote(image)} 2>&1"


def remove_volume_command(engine: str, name: str) -> str:
    _check(engine)
    return f"{engine} volume rm {shlex.quote(name)}"


def remove_network_command(engine: str, name: str) -> str:
    _check(engine)
    if name in BUILTIN_NETWORKS:
        raise ValueError(f"{name} is the engine's own network and can't be removed.")
    return f"{engine} network rm {shlex.quote(name)}"


# ---------------------------------------------------------------- `inspect`, made readable
_SECRET = re.compile(r"pass|secret|token|key|pwd|auth|credential", re.I)

EXIT_CODES = {0: "finished normally", 1: "the program reported an error", 125: "docker couldn't run it",
              126: "the command can't be executed", 127: "the command wasn't found",
              137: "killed (SIGKILL: out of memory, or docker kill)", 139: "crashed (segmentation fault)",
              143: "stopped (SIGTERM)"}


def _mask_env(item: str) -> str:
    name, sep, value = item.partition("=")
    return f"{name}=•••• (hidden)" if sep and value and _SECRET.search(name) else item


def summarize_inspect(text: str) -> str:
    """The things you look for when a container misbehaves (why it stopped, OOM, restarts, health, limits, ports,
    networks, mounts), as plain text, followed by the full JSON. Environment values whose names look like secrets
    are hidden in both."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):                # nothing found: docker's own message says why
        return text
    if "State" not in data:                       # an image, volume or network: the JSON, indented
        return json.dumps(data, indent=2, ensure_ascii=False)
    st = data.get("State") or {}
    cfg = data.get("Config") or {}
    host = data.get("HostConfig") or {}
    out: list[str] = []

    def row(label, value):
        if value not in (None, "", [], {}):
            out.append(f"{label:<18}{value}")
    row("Name", (data.get("Name") or "").lstrip("/"))
    row("Image", cfg.get("Image"))
    status = st.get("Status", "")
    row("State", status + ("  (OOM-killed: it ran out of memory)" if st.get("OOMKilled") else ""))
    if status in ("exited", "dead"):
        code = st.get("ExitCode")
        meaning = EXIT_CODES.get(code, "")
        row("Exit code", f"{code}" + (f"  ({meaning})" if meaning else ""))
    row("Error", st.get("Error"))
    row("Started", st.get("StartedAt"))
    if status != "running":
        row("Finished", st.get("FinishedAt"))
    row("Restarts", data.get("RestartCount"))
    row("Restart policy", (host.get("RestartPolicy") or {}).get("Name") or "no")
    health = st.get("Health") or {}
    if health:
        row("Health", f"{health.get('Status')} ({health.get('FailingStreak', 0)} failing in a row)")
        logs = health.get("Log") or []
        if logs:
            last = logs[-1]
            row("Last health check", f"exit {last.get('ExitCode')}: {(last.get('Output') or '').strip()[:200]}")
    cmd = (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])
    row("Command", " ".join(shlex.quote(x) for x in cmd))
    row("Working folder", cfg.get("WorkingDir"))
    row("User", cfg.get("User"))
    mem = host.get("Memory") or 0
    row("Memory limit", f"{mem // (1024 * 1024)} MB" if mem else "none")
    cpus = (host.get("NanoCpus") or 0) / 1e9
    row("CPU limit", f"{cpus:g} CPUs" if cpus else "none")
    net = data.get("NetworkSettings") or {}
    published = [f"{(b or {}).get('HostIp') or '0.0.0.0'}:{(b or {}).get('HostPort')} → {p}"
                 for p, binds in (net.get("Ports") or {}).items() for b in (binds or [])]
    row("Published ports", ", ".join(published) or "none")
    row("Networks", ", ".join(f"{n} ({(v or {}).get('IPAddress') or 'no IP'})"
                              for n, v in (net.get("Networks") or {}).items()))
    mounts = data.get("Mounts") or []
    if mounts:
        out.append("Mounts")
        for m in mounts:
            src = m.get("Name") or m.get("Source")
            out.append(f"  {m.get('Type', '')} {src} → {m.get('Destination')}"
                       + ("" if m.get("RW", True) else " (read-only)"))
    env = cfg.get("Env") or []
    if env:
        out.append("Environment")
        out += [f"  {_mask_env(e)}" for e in env]
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    for e in env:                                 # the JSON below must not show what the summary hid
        masked = _mask_env(e)
        if masked != e:
            raw = raw.replace(json.dumps(e, ensure_ascii=False), json.dumps(masked, ensure_ascii=False))
    return "\n".join(out) + "\n\n──── full inspect output ────\n" + raw
