"""Data model + persistence (on top of the encrypted vault)."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .vault import Vault

COLORS = ["", "#ff6b6b", "#ffa94d", "#ffd43b", "#69db7c", "#38d9a9",
          "#4dabf7", "#748ffc", "#b197fc", "#f783ac"]


def _id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Server:
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = "password"          # password | key | agent
    password: str = ""
    key_path: str = ""
    key_data: str = ""              # pasted private key (stored encrypted in the vault)
    passphrase: str = ""
    group: str = ""                 # "Prod/EU" style path, "" = root
    tags: list[str] = field(default_factory=list)
    color: str = ""
    favorite: bool = False
    jump_id: str = ""               # id of another server used as a bastion
    keepalive: int = 30
    startup_cmd: str = ""
    notes: str = ""
    last_connected: float = 0.0
    connect_count: int = 0
    id: str = field(default_factory=_id)

    @property
    def label(self) -> str:
        return self.name or self.address

    @property
    def address(self) -> str:
        user = f"{self.username}@" if self.username else ""
        port = f":{self.port}" if self.port and self.port != 22 else ""
        return f"{user}{self.host}{port}"

    def ssh_command(self) -> str:
        parts = ["ssh"]
        if self.port and self.port != 22:
            parts += ["-p", str(self.port)]
        if self.auth == "key" and self.key_path:
            parts += ["-i", f'"{self.key_path}"']
        target = f"{self.username}@{self.host}" if self.username else self.host
        parts.append(target)
        return " ".join(parts)

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        for term in q.split():
            if term.startswith("tag:"):
                if not any(t.lower().startswith(term[4:]) for t in self.tags):
                    return False
                continue
            hay = " ".join([self.name, self.host, self.username, self.group,
                            " ".join(self.tags), self.notes]).lower()
            if term not in hay:
                return False
        return True

    @classmethod
    def from_dict(cls, d: dict) -> "Server":
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in d.items() if k in known})
        s.port = int(s.port or 22)
        s.tags = [t for t in (s.tags or []) if t]
        return s

    def copy(self) -> "Server":
        return Server.from_dict(asdict(self))


@dataclass
class Snippet:
    name: str = ""
    command: str = ""
    id: str = field(default_factory=_id)


class Store:
    """Holds all user data in memory and writes it to the vault on every change."""

    def __init__(self, vault: Vault, data: dict):
        self.vault = vault
        self.servers: dict[str, Server] = {}
        self.snippets: list[Snippet] = []
        self.groups: set[str] = set()   # explicit (possibly empty) groups
        self._load(data)

    def _load(self, data: dict) -> None:
        for d in data.get("servers", []):
            s = Server.from_dict(d)
            self.servers[s.id] = s
        self.snippets = [Snippet(**d) for d in data.get("snippets", [])]
        self.groups = set(data.get("groups", []))
        if "snippets" not in data:
            self.snippets = [
                Snippet("Disk usage", "df -h"),
                Snippet("Top memory processes", "ps aux --sort=-%mem | head -15"),
                Snippet("Listening ports", "sudo ss -tulpn"),
                Snippet("Follow syslog", "sudo journalctl -f"),
                Snippet("Docker containers", "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"),
            ]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "servers": [asdict(s) for s in self.servers.values()],
            "snippets": [asdict(s) for s in self.snippets],
            "groups": sorted(self.groups),
        }

    def save(self) -> None:
        self.vault.save(self.to_dict())

    # ---- servers ------------------------------------------------------
    def upsert(self, server: Server) -> None:
        self.servers[server.id] = server
        if server.group:
            self.groups.add(server.group)
        self.save()

    def delete(self, server_id: str) -> None:
        self.servers.pop(server_id, None)
        for s in self.servers.values():
            if s.jump_id == server_id:
                s.jump_id = ""
        self.save()

    def touch(self, server_id: str) -> None:
        s = self.servers.get(server_id)
        if s:
            s.last_connected = time.time()
            s.connect_count += 1
            self.save()

    def all_groups(self) -> list[str]:
        out = set(self.groups)
        for s in self.servers.values():
            if s.group:
                out.add(s.group)
        # include parents of nested groups
        for g in list(out):
            parts = g.split("/")
            for i in range(1, len(parts)):
                out.add("/".join(parts[:i]))
        return sorted(out, key=str.lower)

    def rename_group(self, old: str, new: str) -> None:
        new = new.strip("/ ")
        for s in self.servers.values():
            if s.group == old or s.group.startswith(old + "/"):
                s.group = new + s.group[len(old):]
        self.groups = {(new + g[len(old):]) if (g == old or g.startswith(old + "/")) else g
                       for g in self.groups}
        self.save()

    def delete_group(self, group: str) -> None:
        """Remove a group; its servers move to the parent group."""
        parent = group.rsplit("/", 1)[0] if "/" in group else ""
        for s in self.servers.values():
            if s.group == group or s.group.startswith(group + "/"):
                s.group = parent
        self.groups = {g for g in self.groups if not (g == group or g.startswith(group + "/"))}
        self.save()

    def recent(self, n: int = 8) -> list[Server]:
        items = [s for s in self.servers.values() if s.last_connected]
        items.sort(key=lambda s: s.last_connected, reverse=True)
        return items[:n]

    def import_servers(self, servers: list[Server]) -> int:
        existing = {(s.host.lower(), s.port, s.username) for s in self.servers.values()}
        added = 0
        for s in servers:
            key = (s.host.lower(), s.port, s.username)
            if key in existing or not s.host:
                continue
            self.servers[s.id] = s
            existing.add(key)
            if s.group:
                self.groups.add(s.group)
            added += 1
        if added:
            self.save()
        return added


def open_or_create(path: Path, password: str, create: bool) -> Store:
    if create:
        v = Vault.create(path, password)
        return Store(v, {})
    v, data = Vault.open(path, password)
    return Store(v, data)
