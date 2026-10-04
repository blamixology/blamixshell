"""systemd timers for the Timers tab: read them, and make a new one (a .service + a .timer) from
the same simple schedule form the Cron tab uses. No Qt here."""
from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass

from . import cron

READ_SCRIPT = r"""
echo @@list
for t in $(systemctl list-units --type=timer --all --no-legend --plain 2>/dev/null | awk '{print $1}'); do
  echo "## $t"
  systemctl show "$t" -p Description -p ActiveState -p UnitFileState -p Unit -p NextElapseUSecRealtime \
    -p LastTriggerUSec -p TimersCalendar -p TimersMonotonic 2>/dev/null
done
"""


@dataclass
class Timer:
    name: str                 # foo.timer
    unit: str = ""            # the service it starts
    description: str = ""
    active: str = ""
    enabled: str = ""
    next: str = ""
    last: str = ""
    schedule: str = ""


def _schedule(show: dict) -> str:
    bits = []
    for key in ("TimersCalendar", "TimersMonotonic"):
        for m in re.finditer(r"On\w+=([^;}]+?)\s*(?:;|\})", show.get(key, "")):
            bits.append(m.group(0).rstrip(";} ").strip())
    return ", ".join(bits)


def parse(text: str) -> list[Timer]:
    out: list[Timer] = []
    cur: Timer | None = None
    show: dict[str, str] = {}

    def finish():
        if cur is not None:
            cur.schedule = _schedule(show)
            out.append(cur)
    for line in text.splitlines():
        if line.startswith("## "):
            finish()
            cur, show = Timer(name=line[3:].strip()), {}
        elif cur is not None and "=" in line:
            k, _, v = line.partition("=")
            show[k] = v.strip()
            cur.description = show.get("Description", "")
            cur.active = show.get("ActiveState", "")
            cur.enabled = show.get("UnitFileState", "")
            cur.unit = show.get("Unit", "")
            cur.next = _nice(show.get("NextElapseUSecRealtime", ""))
            cur.last = _nice(show.get("LastTriggerUSec", ""))
    finish()
    return sorted(out, key=lambda t: t.name)


def _nice(v: str) -> str:
    v = v.strip()
    return "–" if v in ("", "0", "n/a") else v


# ---------------------------------------------------------------- cron expression -> OnCalendar
_DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _dow(field: str) -> str:
    if field == "*":
        return ""
    parts = []
    for p in field.split(","):
        if "-" in p:
            a, b = p.split("-", 1)
            parts.append(f"{_dname(a)}..{_dname(b)}")
        else:
            parts.append(_dname(p))
    return ",".join(parts) + " "


def _dname(x: str) -> str:
    return _DOW_NAMES[int(x) % 7] if x.isdigit() else x.capitalize()


def calendar_from_cron(expr: str) -> str | None:
    """OnCalendar= value for a cron schedule, or None when it can't be written the same way
    (@reboot, or both day-of-month and day-of-week given: cron ORs them, systemd ANDs them)."""
    expr = expr.strip()
    if cron.validate_schedule(expr) or expr == "@reboot":
        return None
    expr = cron._SPECIAL_EXPR.get(expr, expr)
    m, h, dom, mon, dow = expr.split()
    if dom != "*" and dow != "*":
        return None
    hour = h if h != "*" else "*"
    return f"{_dow(dow)}*-{mon}-{dom} {hour}:{m}:00"


# ---------------------------------------------------------------- creating one
def exec_shell(command: str) -> str:
    """ExecStart= that runs `command` through sh (so redirects and && work), quoted for systemd."""
    s = command.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'/bin/sh -c "{s}"'


def build_units(description: str, command: str, user: str = "", calendar: str = "", boot: bool = False,
                persistent: bool = True) -> tuple[str, str]:
    """(service text, timer text). `calendar` = OnCalendar value; `boot` = run 1 minute after startup."""
    desc = description.strip() or "Scheduled job created with BlamixShell"
    service = ["[Unit]", f"Description={desc}", "", "[Service]", "Type=oneshot", f"ExecStart={exec_shell(command)}"]
    if user.strip():
        service.append(f"User={user.strip()}")
    timer = ["[Unit]", f"Description={desc} (timer)", "", "[Timer]"]
    timer.append("OnBootSec=1min" if boot else f"OnCalendar={calendar}")
    if persistent and not boot:
        timer.append("Persistent=true")
    timer += ["", "[Install]", "WantedBy=timers.target", ""]
    return "\n".join(service) + "\n", "\n".join(timer)


def create_command(name: str, service_text: str, timer_text: str, enable: bool = True) -> str:
    d = "/etc/systemd/system"
    q = shlex.quote
    b = lambda t: base64.b64encode(t.encode("utf-8")).decode("ascii")        # noqa: E731
    steps = [f"[ ! -e {q(f'{d}/{name}.timer')} ] && [ ! -e {q(f'{d}/{name}.service')} ] || "
             f"{{ echo {q(name + ' already exists')} >&2; exit 1; }}",
             f"echo {b(service_text)} | base64 -d > {q(f'{d}/{name}.service')}",
             f"echo {b(timer_text)} | base64 -d > {q(f'{d}/{name}.timer')}",
             "systemctl daemon-reload"]
    if enable:
        steps.append(f"systemctl enable --now {q(name + '.timer')}")
    return f"sh -c {q(' && '.join(steps))}"


def toggle_command(timer: str, enable: bool) -> str:
    return f"systemctl {'enable --now' if enable else 'disable --now'} {shlex.quote(timer)}"


def run_now_command(service_unit: str) -> str:
    return f"systemctl start {shlex.quote(service_unit)}"
