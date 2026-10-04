"""Docker / Podman tab helpers: list containers with their CPU and memory, and build the
commands to start, stop, restart, remove them or read their logs. No Qt here."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

READ_SCRIPT = r"""
for e in docker podman; do
  if command -v $e >/dev/null 2>&1; then
    echo @@engine; echo $e
    echo @@ps; $e ps -a --format '{{json .}}' 2>&1
    echo @@stats; $e stats --no-stream --format '{{json .}}' 2>&1
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

    @property
    def running(self) -> bool:
        return self.state in ("running", "restarting", "paused")


@dataclass
class Containers:
    engine: str = "none"
    items: list[Container] | None = None
    needs_access: bool = False
    error: str = ""

    def __post_init__(self):
        if self.items is None:
            self.items = []


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
            st.get("CPUPerc", ""), (st.get("MemUsage", "") or "").split(" / ")[0]))
    rank = {"running": 0, "restarting": 1, "paused": 2}
    result.items.sort(key=lambda c: (rank.get(c.state, 3), c.name))
    return result


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
