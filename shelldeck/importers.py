"""Import servers from PuTTY (Windows registry) and OpenSSH ~/.ssh/config."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote

from .models import Server

PUTTY_KEY = r"Software\SimonTatham\PuTTY\Sessions"


def putty_sessions() -> list[Server]:
    if sys.platform != "win32":
        return []
    import winreg  # noqa: PLC0415 (Windows only)

    out: list[Server] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PUTTY_KEY)
    except OSError:
        return out
    i = 0
    while True:
        try:
            raw_name = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, raw_name)
        except OSError:
            continue

        def val(name, default=None):
            try:
                return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return default

        if (val("Protocol", "ssh") or "ssh") != "ssh":
            continue
        host = (val("HostName", "") or "").strip()
        if not host:
            continue
        user = (val("UserName", "") or "").strip()
        if "@" in host and not user:
            user, host = host.rsplit("@", 1)
        keyfile = (val("PublicKeyFile", "") or "").strip()
        name = unquote(raw_name)
        if name == "Default Settings":
            continue
        out.append(Server(
            name=name, host=host, port=int(val("PortNumber", 22) or 22), username=user,
            auth="key" if keyfile else "password", key_path=keyfile,
            group="Imported/PuTTY", tags=["putty"],
        ))
    return out


def parse_ssh_config(text: str, base_dir: Path | None = None) -> list[Server]:
    """Parse the useful subset of an OpenSSH client config."""
    import paramiko

    cfg = paramiko.SSHConfig.from_text(text)
    aliases = [h for h in cfg.get_hostnames() if h != "*" and not any(c in h for c in "*?!")]
    servers: dict[str, Server] = {}
    jumps: dict[str, str] = {}
    for alias in aliases:
        o = cfg.lookup(alias)
        ident = (o.get("identityfile") or [""])[0]
        if ident and base_dir and ident.startswith("~"):
            ident = str(Path(ident).expanduser())
        s = Server(
            name=alias, host=o.get("hostname", alias), port=int(o.get("port", 22)),
            username=o.get("user", ""), auth="key" if ident else "agent", key_path=ident,
            group="Imported/ssh-config", tags=["ssh-config"],
        )
        servers[alias] = s
        pj = o.get("proxyjump")
        if pj and pj.lower() != "none":
            jumps[alias] = pj.split(",")[-1].strip()
    for alias, jump in jumps.items():
        target = jump.split("@")[-1].split(":")[0]
        if target in servers:
            servers[alias].jump_id = servers[target].id
    return list(servers.values())


def ssh_config_servers() -> list[Server]:
    p = Path.home() / ".ssh" / "config"
    if not p.exists():
        return []
    return parse_ssh_config(p.read_text(encoding="utf-8", errors="replace"), p.parent)
