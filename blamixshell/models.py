"""Data model + persistence (on top of the encrypted vault)."""
from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .vault import PasswordChangedElsewhere, Vault, VaultError  # noqa: F401 (re-exported)

BACKUPS_KEEP = 20                 # encrypted copies kept in <data>/backups
BACKUP_EVERY = 24 * 3600          # at most one automatic backup a day

COLORS = ["", "#ff6b6b", "#ffa94d", "#ffd43b", "#69db7c", "#38d9a9",
          "#4dabf7", "#748ffc", "#b197fc", "#f783ac"]


def _id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Tunnel:
    """A port forward that runs while the server is connected.

    L  local:   listen here  (listen_host:listen_port) -> dest_host:dest_port as seen by the server
    R  remote:  listen on the server (listen_host:listen_port) -> dest_host:dest_port as seen from here
    D  dynamic: SOCKS4/5 proxy here (listen_host:listen_port), destinations chosen by the app using it
    """
    kind: str = "L"
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    dest_host: str = "localhost"
    dest_port: int = 0
    enabled: bool = True
    name: str = ""

    def describe(self) -> str:
        lp = f"{self.listen_host}:{self.listen_port}"
        if self.kind == "D":
            return f"SOCKS {lp}"
        arrow = f"{self.dest_host}:{self.dest_port}"
        return f"{'Local' if self.kind == 'L' else 'Remote'} {lp} → {arrow}"

    def problem(self) -> str:
        """'' if the tunnel is usable, else a short reason."""
        if self.kind not in ("L", "R", "D"):
            return "unknown type"
        if not (0 <= int(self.listen_port) <= 65535) or (self.kind != "R" and not self.listen_port):
            return "needs a listen port"
        if self.kind != "D" and (not self.dest_host or not (0 < int(self.dest_port) <= 65535)):
            return "needs a destination host and port"
        return ""

    @classmethod
    def parse(cls, kind: str, spec: str) -> "Tunnel":
        """OpenSSH syntax: L/R '[bind:]port:host:hostport', D '[bind:]port'.
        Also accepts the 'LocalForward 8080 host:80' form from ssh_config."""
        kind = kind.upper().lstrip("-")[:1]
        spec = spec.strip().replace(" ", ":")
        if spec.startswith("["):   # [::1]:port… - keep IPv6 literals whole
            host_end = spec.index("]")
            parts = [spec[1:host_end]] + spec[host_end + 2:].split(":")
        else:
            parts = spec.split(":")
        default_bind = "localhost" if kind == "R" else "127.0.0.1"
        try:
            if kind == "D":
                bind, port = (parts[0], parts[1]) if len(parts) == 2 else (default_bind, parts[0])
                return cls("D", bind or default_bind, int(port), "", 0)
            if len(parts) == 4:
                bind, port, host, hport = parts
            elif len(parts) == 3:
                bind, (port, host, hport) = default_bind, parts
            else:
                raise ValueError
            if bind in ("*", ""):
                bind = "0.0.0.0" if kind == "L" else ""
            return cls(kind, bind, int(port), host, int(hport))
        except (ValueError, IndexError):
            raise ValueError(f"Can't read -{kind} {spec!r}; expected "
                             + ("[bind:]port" if kind == "D" else "[bind:]port:host:hostport")) from None

    @classmethod
    def from_dict(cls, d: dict) -> "Tunnel":
        known = {f.name for f in fields(cls)}
        t = cls(**{k: v for k, v in d.items() if k in known})
        t.listen_port = int(t.listen_port or 0)
        t.dest_port = int(t.dest_port or 0)
        return t


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
    agent_forward: bool = False     # ssh -A: let this server use the local SSH agent
    keepalive: int = 30
    startup_cmd: str = ""
    tunnels: list[Tunnel] = field(default_factory=list)
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
        s.tunnels = [t if isinstance(t, Tunnel) else Tunnel.from_dict(t) for t in (s.tunnels or [])]
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

    def __init__(self, vault: Vault, data: dict, backup_dir: Path | None = None):
        self.vault = vault
        self.servers: dict[str, Server] = {}
        self.snippets: list[Snippet] = []
        self.groups: set[str] = set()   # explicit (possibly empty) groups
        self.group_colors: dict[str, str] = {}
        self.notify = lambda message: None   # the UI shows merge/sync messages through this
        if backup_dir is None:
            from .paths import data_dir
            backup_dir = data_dir() / "backups"
        self.backup_dir = Path(backup_dir)
        self._load(data)
        self._base = self.to_dict()         # what's on disk, for merging concurrent edits

    def _load(self, data: dict) -> None:
        self.servers, self.snippets, self.groups = {}, [], set()
        self.group_colors = {k: v for k, v in (data.get("group_colors") or {}).items() if v}
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
            "group_colors": dict(sorted(self.group_colors.items())),
        }

    # ---- saving, sync and backups ---------------------------------------------
    def save(self) -> None:
        """Write the vault. If another computer changed the file since we read it
        (vault in OneDrive / Syncthing / on a USB stick), merge both versions first
        instead of overwriting theirs."""
        data = self.to_dict()
        if self.vault.changed_on_disk():
            try:
                theirs = self.vault.read_disk()
            except PasswordChangedElsewhere:
                raise                        # the UI asks to unlock again; don't overwrite
            except VaultError:
                theirs = None                # half-synced/damaged file: keep a copy, then write ours
                self.backup_now("unreadable")
            if theirs is not None:
                self.backup_now("before-merge")
                data, conflicts = merge_vault_data(self._base, data, theirs)
                self._load(data)
                self.notify("Merged changes made on another computer"
                            + (f" ({conflicts} edited on both: kept this computer's version)" if conflicts else "")
                            + ". A backup of the other version was kept.")
        self._daily_backup()
        self.vault.save(data)
        self._base = data

    def reload_if_changed(self) -> bool:
        """Pick up changes another computer made to a shared vault file.
        Returns True if the data changed. Raises PasswordChangedElsewhere."""
        if not self.vault.changed_on_disk():
            return False
        try:
            theirs = self.vault.read_disk()
        except PasswordChangedElsewhere:
            raise
        except VaultError:
            return False                     # still syncing: try again later
        merged, _ = merge_vault_data(self._base, self.to_dict(), theirs)
        self._load(merged)
        self._base = theirs
        if merged != theirs:                 # we had changes they didn't: write the merge
            self.save()
        return True

    def backups(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(self.backup_dir.glob("vault-*.sdv"), reverse=True)

    def backup_now(self, reason: str = "manual") -> Path | None:
        """Copy the encrypted vault file into the backups folder (same master password)."""
        if not self.vault.path.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backup_dir / f"vault-{stamp}-{reason}.sdv"
        n = 1
        while dest.exists():
            n += 1
            dest = self.backup_dir / f"vault-{stamp}-{reason}-{n}.sdv"
        shutil.copy2(self.vault.path, dest)
        for old in self.backups()[BACKUPS_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest

    def _daily_backup(self) -> None:
        newest = self.backups()[:1]
        if newest and time.time() - newest[0].stat().st_mtime < BACKUP_EVERY:
            return
        self.backup_now("daily")

    def change_password(self, new_password: str) -> None:
        self.backup_now("before-password-change")
        self.vault.change_password(new_password, self.to_dict())
        self._base = self.to_dict()

    def restore_backup(self, path: Path, password: str) -> None:
        """Replace everything with the contents of a backup (the current vault is
        backed up first, so a restore can itself be undone)."""
        _v, data = Vault.open(Path(path), password)
        self.backup_now("before-restore")
        self._load(data)
        self.vault.save(self.to_dict())
        self._base = self.to_dict()

    def move_vault(self, target: Path) -> None:
        """Write the vault to a new file (e.g. inside a OneDrive / Syncthing folder) and
        use that from now on. The old file is left in place."""
        target = Path(target)
        self.vault.path = target
        self.vault.save(self.to_dict())
        self._base = self.to_dict()

    def import_vault(self, path: Path, password: str) -> tuple[int, int]:
        """Add servers, snippets and groups from another vault file (never deletes or
        overwrites anything here). Returns (servers added, snippets added)."""
        _v, data = Vault.open(Path(path), password)
        self.backup_now("before-import")
        servers = [Server.from_dict(d) for d in data.get("servers", [])]
        known = {(s.host.lower(), s.port, s.username) for s in self.servers.values()}
        added = 0
        for s in servers:
            if s.id in self.servers or (s.host.lower(), s.port, s.username) in known:
                continue
            self.servers[s.id] = s
            known.add((s.host.lower(), s.port, s.username))
            if s.group:
                self.groups.add(s.group)
            added += 1
        have = {(sn.name, sn.command) for sn in self.snippets} | {sn.id for sn in self.snippets}
        snips = 0
        for d in data.get("snippets", []):
            sn = Snippet(**{k: v for k, v in d.items() if k in ("name", "command", "id")})
            if sn.id in have or (sn.name, sn.command) in have:
                continue
            self.snippets.append(sn)
            snips += 1
        self.groups |= set(data.get("groups", []))
        for g, col in (data.get("group_colors") or {}).items():
            self.group_colors.setdefault(g, col)
        self.save()
        return added, snips

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

    # ---- colors (a server's own color, else the nearest group color) -----------
    def group_color(self, path: str) -> str:
        parts = path.split("/") if path else []
        while parts:
            col = self.group_colors.get("/".join(parts))
            if col:
                return col
            parts.pop()
        return ""

    def color_for(self, server: Server) -> str:
        return server.color or self.group_color(server.group)

    def set_group_color(self, path: str, color: str) -> None:
        if color:
            self.group_colors[path] = color
        else:
            self.group_colors.pop(path, None)
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
        self.group_colors = {((new + g[len(old):]) if (g == old or g.startswith(old + "/")) else g): c
                             for g, c in self.group_colors.items()}
        self.save()

    def delete_group(self, group: str) -> None:
        """Remove a group; its servers move to the parent group."""
        parent = group.rsplit("/", 1)[0] if "/" in group else ""
        for s in self.servers.values():
            if s.group == group or s.group.startswith(group + "/"):
                s.group = parent
        self.groups = {g for g in self.groups if not (g == group or g.startswith(group + "/"))}
        self.group_colors = {g: c for g, c in self.group_colors.items()
                             if not (g == group or g.startswith(group + "/"))}
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


def merge_vault_data(base: dict, ours: dict, theirs: dict) -> tuple[dict, int]:
    """Three-way merge of two versions of the vault contents that both started from
    `base`. Per server / snippet (by id): a change on one side wins over no change;
    if both sides changed the same item, ours wins, and a deletion never beats an
    edit (nothing is lost). Returns (merged, number of both-sides conflicts)."""
    import json

    def key(d):
        return json.dumps(d, sort_keys=True) if d is not None else None

    out = dict(ours)
    conflicts = 0
    for kind in ("servers", "snippets"):
        b = {d["id"]: d for d in base.get(kind, []) if "id" in d}
        o = {d["id"]: d for d in ours.get(kind, []) if "id" in d}
        t = {d["id"]: d for d in theirs.get(kind, []) if "id" in d}
        ids = list(o) + [i for i in t if i not in o] + [i for i in b if i not in o and i not in t]
        result = []
        for i in ids:
            bo, oo, to = b.get(i), o.get(i), t.get(i)
            if key(oo) == key(to):
                r = oo
            elif key(oo) == key(bo):
                r = to
            elif key(to) == key(bo):
                r = oo
            else:
                conflicts += 1
                r = oo if oo is not None else to
            if r is not None:
                result.append(r)
        out[kind] = result
    bg, og, tg = set(base.get("groups", [])), set(ours.get("groups", [])), set(theirs.get("groups", []))
    out["groups"] = sorted((og & tg) | (og - bg) | (tg - bg))
    bc, oc, tc = (x.get("group_colors") or {} for x in (base, ours, theirs))
    colors = {}
    for g in set(oc) | set(tc):
        o_, t_, b_ = oc.get(g), tc.get(g), bc.get(g)
        v = t_ if o_ == b_ else o_
        if v:
            colors[g] = v
    out["group_colors"] = dict(sorted(colors.items()))
    return out, conflicts


def open_or_create(path: Path, password: str, create: bool) -> Store:
    if create:
        v = Vault.create(path, password)
        return Store(v, {})
    v, data = Vault.open(path, password)
    return Store(v, data)
