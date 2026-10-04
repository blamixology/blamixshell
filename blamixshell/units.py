"""Create a systemd service from a simple form: build the unit file and the command that installs it.
No Qt here."""
from __future__ import annotations

import base64
import re
import shlex

UNIT_DIR = "/etc/systemd/system"
RESTART = ("on-failure", "always", "no")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,60}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and not name.endswith((".service", ".timer", ".socket"))


def exec_error(exec_start: str) -> str:
    """"" when `exec_start` is acceptable: systemd wants the program as an absolute path."""
    s = exec_start.strip().lstrip("-@+!:")
    if not s:
        return "Enter the command to run."
    if not s.startswith("/"):
        return "Start with the full path of the program, e.g. /usr/bin/node /srv/app/server.js"
    if "\n" in exec_start:
        return "One line only."
    return ""


def _spec(s: str) -> str:
    """% starts a specifier in unit files; write a literal % as %%."""
    return s.replace("%%", "%").replace("%", "%%")


def build_unit(description: str, exec_start: str, user: str = "", workdir: str = "", restart: str = "on-failure",
               env: list[str] | None = None, after_network: bool = True) -> str:
    lines = ["[Unit]", f"Description={description.strip() or 'Service created with BlamixShell'}"]
    if after_network:
        lines.append("After=network.target")
    lines += ["", "[Service]", "Type=simple", f"ExecStart={_spec(exec_start.strip())}"]
    if user.strip():
        lines.append(f"User={user.strip()}")
    if workdir.strip():
        lines.append(f"WorkingDirectory={workdir.strip()}")
    if restart in ("on-failure", "always"):
        lines += [f"Restart={restart}", "RestartSec=5"]
    for e in env or []:
        e = e.strip()
        if _ENV.fullmatch(e):
            k, v = e.split("=", 1)
            lines.append('Environment="%s=%s"' % (k, _spec(v).replace("\\", "\\\\").replace('"', '\\"')))
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def create_command(name: str, unit_text: str, enable: bool = True, start: bool = True) -> str:
    """One `sh -c` that writes the unit (refusing to overwrite one), reloads systemd, and
    optionally enables and starts it, stopping at the first step that fails. Run as root."""
    path = f"{UNIT_DIR}/{name}.service"
    b64 = base64.b64encode(unit_text.encode("utf-8")).decode("ascii")
    steps = [f"[ ! -e {shlex.quote(path)} ] || {{ echo {shlex.quote(name + '.service already exists')} >&2; exit 1; }}",
             f"echo {b64} | base64 -d > {shlex.quote(path)}", f"chmod 644 {shlex.quote(path)}",
             "systemctl daemon-reload"]
    if enable:
        steps.append(f"systemctl enable {shlex.quote(name)}")
    if start:
        steps.append(f"systemctl start {shlex.quote(name)}")
    return f"sh -c {shlex.quote(' && '.join(steps))}"
