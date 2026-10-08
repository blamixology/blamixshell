"""Docker Compose projects on a server, found from the labels compose puts on every container it starts (works with
`docker compose`, the old `docker-compose`, `podman compose` and `podman-compose`), and the commands to manage them.
Every command runs in the project's own folder with its own compose files, like you would by hand. No Qt here."""
from __future__ import annotations

import base64
import re
import shlex
import time
from dataclasses import dataclass, field

from .docker import COMPOSE_TOOLS, Container

L_PROJECT = "com.docker.compose.project"
L_DIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"


@dataclass
class Service:
    name: str
    containers: list[Container] = field(default_factory=list)

    @property
    def state(self) -> str:
        states = [c.state for c in self.containers]
        if states and all(s == "running" for s in states):
            return "running" if len(states) == 1 else f"running ({len(states)})"
        if any(s == "running" for s in states):
            return f"{sum(1 for s in states if s == 'running')} of {len(states)} running"
        return states[0] if len(set(states)) == 1 else "stopped"

    @property
    def image(self) -> str:
        return self.containers[0].image if self.containers else ""


@dataclass
class Project:
    name: str
    folder: str = ""
    files: list[str] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)

    @property
    def containers(self) -> list[Container]:
        return [c for s in self.services for c in s.containers]

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.state == "running")

    @property
    def state(self) -> str:
        n, run = len(self.containers), self.running
        if run == n:
            return "running"
        return "stopped" if run == 0 else f"partly running ({run} of {n})"

    @property
    def manageable(self) -> bool:
        """The folder and files are known (compose v1.24+ and v2 label them), so commands can run."""
        return bool(self.folder and self.files)


def projects(containers: list[Container]) -> list[Project]:
    found: dict[str, Project] = {}
    for c in containers:
        name = c.labels.get(L_PROJECT, "")
        if not name:
            continue
        p = found.setdefault(name, Project(name))
        p.folder = p.folder or c.labels.get(L_DIR, "")
        if not p.files and c.labels.get(L_FILES):
            p.files = [f for f in c.labels[L_FILES].split(",") if f]
        svc = next((s for s in p.services if s.name == c.service), None)
        if svc is None:
            svc = Service(c.service or c.name)
            p.services.append(svc)
        svc.containers.append(c)
    for p in found.values():
        p.services.sort(key=lambda s: s.name)
    return sorted(found.values(), key=lambda p: (p.running == 0, p.name.lower()))


# ---------------------------------------------------------------- commands
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.I)


def _base(tool: str, p: Project, files: list[str] | None = None) -> str:
    if tool not in COMPOSE_TOOLS:
        raise ValueError("No compose command on this server (docker compose, docker-compose or podman-compose).")
    if not p.manageable:
        raise ValueError(f"{p.name}: compose didn't record its folder and files (a very old compose?), so it "
                         "can't be managed from here.")
    if not _NAME.fullmatch(p.name):
        raise ValueError(f"Unusual project name: {p.name}")
    fs = " ".join(f"-f {shlex.quote(f)}" for f in (files or p.files))
    return f"cd {shlex.quote(p.folder)} && {tool} -p {shlex.quote(p.name)} {fs}"


def _sh(script: str) -> str:
    return f"sh -c {shlex.quote(script)}"


def _service(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"Unusual service name: {name}")
    return shlex.quote(name)


ACTIONS = {
    "up": ("up -d", "Start (create what is missing)"),
    "stop": ("stop", "Stop"),
    "start": ("start", "Start the stopped containers"),
    "restart": ("restart", "Restart"),
    "pull": ("pull", "Download newer images"),
    "update": ("pull 2>&1 && {base} up -d", "Update: pull newer images and re-create what changed"),
    "down": ("down", "Remove the containers and networks (volumes are kept)"),
}


def action_command(tool: str, p: Project, action: str, service: str = "") -> str:
    if action not in ACTIONS:
        raise ValueError(action)
    base = _base(tool, p)
    verb = ACTIONS[action][0].replace("{base}", base)
    svc = f" {_service(service)}" if service else ""
    return _sh(f"{base} {verb}{svc} 2>&1")


def recreate_command(tool: str, p: Project, service: str) -> str:
    """Re-create one service's containers (after its image or settings changed)."""
    return _sh(f"{_base(tool, p)} up -d --force-recreate {_service(service)} 2>&1")


def logs_command(tool: str, p: Project, service: str = "", lines: int = 300) -> str:
    svc = f" {_service(service)}" if service else ""
    return _sh(f"{_base(tool, p)} logs --no-color --timestamps --tail {int(lines)}{svc} 2>&1")


def ps_command(tool: str, p: Project) -> str:
    return _sh(f"{_base(tool, p)} ps -a 2>&1")


def config_command(tool: str, p: Project) -> str:
    """The configuration as compose understands it (variables filled in, files merged), or the errors in it."""
    return _sh(f"{_base(tool, p)} config 2>&1")


def read_files_command(p: Project) -> str:
    """The compose files, each after a "@@<path>" line."""
    if not p.files:
        raise ValueError("No compose files known for this project.")
    return _sh("; ".join(f"echo @@{shlex.quote(f)}; cat {shlex.quote(f)} 2>&1" for f in p.files))


def parse_files(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = None
    for line in text.splitlines(keepends=True):
        if line.startswith("@@/"):
            cur = line[2:].rstrip("\n")
            out[cur] = ""
        elif cur is not None:
            out[cur] += line
    return out


def save_file_command(tool: str, p: Project, path: str, text: str, stamp: str = "") -> str:
    """Write a new version of one compose file, but only if compose accepts it (checked with `config -q` on a copy
    next to the original, with the project's other files); the previous version is kept as <file>.bak-<time>."""
    if path not in p.files:
        raise ValueError("That file isn't one of this project's compose files.")
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    tmp = f"{path}.blamixshell-new"
    files = [tmp if f == path else f for f in p.files]
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    q, qt, qb = shlex.quote(path), shlex.quote(tmp), shlex.quote(f"{path}.bak-{stamp}")
    script = (f"echo {b64} | base64 -d > {qt} || exit 1; "
              f"if out=$({_base(tool, p, files)} config -q 2>&1); then "
              f"cp -p {q} {qb} && cat {qt} > {q} && rm -f {qt} && echo \"Saved. The previous version is {path}.bak-{stamp}\"; "
              f"else rm -f {qt}; echo \"$out\" >&2; echo 'Not saved: compose found errors (above).' >&2; exit 1; fi")
    return _sh(script)
