"""Crontab helpers for the dashboard's Cron tab: parse, build, describe, preview. No Qt here.

A crontab is kept as its raw lines, so comments, MAILTO= / PATH= lines and anything we
don't understand survive an edit untouched. Jobs can be switched off without deleting them:
the line gets the OFF prefix (a plain comment to cron).
"""
from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta

OFF = "#off# "
WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SPECIALS = {"@reboot": "At server startup", "@hourly": "Every hour", "@daily": "Every day at 00:00",
            "@midnight": "Every day at 00:00", "@weekly": "Every Sunday at 00:00",
            "@monthly": "On the 1st of every month at 00:00", "@yearly": "Every January 1st at 00:00",
            "@annually": "Every January 1st at 00:00"}
_SPECIAL_EXPR = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
                 "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_RANGES = [(0, 59, None), (0, 23, None), (1, 31, None), (1, 12, _MONTHS), (0, 7, _DOW)]
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


@dataclass
class Entry:
    kind: str                 # job | env | comment | blank
    raw: str
    schedule: str = ""
    command: str = ""
    enabled: bool = True
    user: str = ""            # system crontabs: the user column
    source: str = ""          # system crontabs: the file it came from


# ---------------------------------------------------------------- schedule fields
def _num(tok: str, lo: int, hi: int, names) -> int:
    t = tok.lower()
    if names and t in names:
        return names.index(t) + (1 if names is _MONTHS else 0)
    if not t.isdigit():
        raise ValueError(f"“{tok}” isn't a number")
    n = int(t)
    if not lo <= n <= hi:
        raise ValueError(f"{n} is outside {lo}-{hi}")
    return n


def expand_field(field: str, lo: int, hi: int, names=None) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        if not part:
            raise ValueError("empty list item")
        rng, _, step_s = part.partition("/")
        step = 1
        if _:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError("the step after / must be a positive number")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, y = rng.split("-", 1)
            a, b = _num(x, lo, hi, names), _num(y, lo, hi, names)
            if a > b:
                raise ValueError(f"{rng} runs backwards")
        else:
            a = _num(rng, lo, hi, names)
            b = hi if step_s else a
        out.update(range(a, b + 1, step))
    return out


def validate_schedule(expr: str) -> str:
    """"" when `expr` is a valid 5-field or @special schedule, else what is wrong."""
    expr = expr.strip()
    if expr in SPECIALS:
        return ""
    if expr.startswith("@"):
        return f"Unknown shortcut {expr.split()[0]}"
    fields = expr.split()
    if len(fields) != 5:
        return "A schedule has 5 fields: minute hour day-of-month month day-of-week"
    labels = ["minute", "hour", "day of month", "month", "day of week"]
    for f, label, (lo, hi, names) in zip(fields, labels, _RANGES):
        try:
            expand_field(f, lo, hi, names)
        except ValueError as e:
            return f"{label}: {e}"
    return ""


# ---------------------------------------------------------------- parse / build
def _job_from(line: str, system: bool) -> Entry | None:
    s = line.strip()
    if s.startswith("@"):
        parts = s.split(None, 2 if system else 1)
        if parts[0] not in SPECIALS or len(parts) < (3 if system else 2):
            return None
        return Entry("job", line, parts[0], parts[-1], user=parts[1] if system else "")
    parts = s.split(None, 6 if system else 5)
    need = 7 if system else 6
    if len(parts) < need:
        return None
    sched = " ".join(parts[:5])
    if validate_schedule(sched):
        return None
    return Entry("job", line, sched, parts[-1], user=parts[5] if system else "")


def parse_line(line: str, system: bool = False) -> Entry:
    s = line.strip()
    if not s:
        return Entry("blank", line)
    if s.startswith(OFF.strip()):
        job = _job_from(s[len(OFF.strip()):].lstrip(), system)
        if job:
            job.raw, job.enabled = line, False
            return job
    if s.startswith("#"):
        return Entry("comment", line)
    if _ENV.match(s):
        return Entry("env", line)
    return _job_from(line, system) or Entry("comment", line)


def parse_crontab(text: str, system: bool = False) -> list[Entry]:
    out, source = [], ""
    for line in text.splitlines():
        if system and line.startswith("# ---- "):
            source = line[7:].strip()
            continue
        e = parse_line(line, system)
        e.source = source
        out.append(e)
    return out


def format_job(schedule: str, command: str, enabled: bool = True) -> str:
    return ("" if enabled else OFF) + f"{schedule.strip()} {command.strip()}"


def build_crontab(entries: list[Entry]) -> str:
    text = "\n".join(e.raw for e in entries)
    return text + "\n" if text else ""


# ---------------------------------------------------------------- friendly form <-> expression
KINDS = ("minutes", "hourly", "daily", "weekly", "monthly", "reboot", "custom")


def build_schedule(kind: str, minute: int = 0, hour: int = 0, weekday: int = 1, day: int = 1,
                   every: int = 5) -> str:
    if kind == "minutes":
        return "* * * * *" if every <= 1 else f"*/{every} * * * *"
    if kind == "hourly":
        return f"{minute} * * * *"
    if kind == "daily":
        return f"{minute} {hour} * * *"
    if kind == "weekly":
        return f"{minute} {hour} * * {weekday}"
    if kind == "monthly":
        return f"{minute} {hour} {day} * *"
    if kind == "reboot":
        return "@reboot"
    raise ValueError(kind)


def classify(expr: str) -> tuple[str, dict]:
    """The form that would produce `expr`: (kind, parameters), or ("custom", {})."""
    expr = expr.strip()
    if expr == "@reboot":
        return "reboot", {}
    expr = _SPECIAL_EXPR.get(expr, expr)
    f = expr.split()
    if len(f) != 5 or validate_schedule(expr):
        return "custom", {}
    m, h, dom, mon, dow = f
    plain = lambda x: x.isdigit()          # noqa: E731
    if f == ["*"] * 5:
        return "minutes", {"every": 1}
    mm = re.fullmatch(r"\*/(\d+)", m)
    if mm and f[1:] == ["*"] * 4:
        return "minutes", {"every": int(mm.group(1))}
    if plain(m) and f[1:] == ["*"] * 4:
        return "hourly", {"minute": int(m)}
    if plain(m) and plain(h) and mon == "*":
        base = {"minute": int(m), "hour": int(h)}
        if dom == "*" and dow == "*":
            return "daily", base
        if dom == "*" and plain(dow) and int(dow) <= 7:
            return "weekly", {**base, "weekday": int(dow) % 7}
        if plain(dom) and dow == "*":
            return "monthly", {**base, "day": int(dom)}
    return "custom", {}


def _days(dow: str) -> str:
    days = sorted({d % 7 for d in expand_field(dow, 0, 7, _DOW)})
    if days == [1, 2, 3, 4, 5]:
        return "weekday"
    if days == [0, 6]:
        return "weekend day"
    return ", ".join(WEEKDAYS[d] for d in days)


def describe(expr: str) -> str:
    expr = expr.strip()
    if expr in SPECIALS:
        return SPECIALS[expr]
    kind, p = classify(expr)
    t = f"{p.get('hour', 0):02d}:{p.get('minute', 0):02d}"
    if kind == "minutes":
        return "Every minute" if p["every"] <= 1 else f"Every {p['every']} minutes"
    if kind == "hourly":
        return f"Every hour at :{p['minute']:02d}"
    if kind == "daily":
        return f"Every day at {t}"
    if kind == "weekly":
        return f"Every {WEEKDAYS[p['weekday']]} at {t}"
    if kind == "monthly":
        return f"On day {p['day']} of every month at {t}"
    f = expr.split()
    if len(f) == 5 and not validate_schedule(expr):
        m, h, dom, mon, dow = f
        if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
            return f"Every {_days(dow)} at {int(h):02d}:{int(m):02d}"
        if m.isdigit() and re.fullmatch(r"\*/\d+", h) and f[2:] == ["*"] * 3:
            return f"Every {h[2:]} hours at :{int(m):02d}"
        return "Custom schedule"
    return "Invalid schedule"


# ---------------------------------------------------------------- next runs
def next_runs(expr: str, after: datetime, count: int = 3) -> list[datetime]:
    """The next `count` times `expr` fires after `after` (in whatever clock `after` is in)."""
    expr = _SPECIAL_EXPR.get(expr.strip(), expr.strip())
    if expr == "@reboot" or validate_schedule(expr):
        return []
    m, h, dom, mon, dow = expr.split()
    mins, hours = expand_field(m, 0, 59), expand_field(h, 0, 23)
    months = expand_field(mon, 1, 12, _MONTHS)
    doms = expand_field(dom, 1, 31)
    dows = {d % 7 for d in expand_field(dow, 0, 7, _DOW)}
    dom_any, dow_any = dom.startswith("*"), dow.startswith("*")
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = after + timedelta(days=366 * 5)
    out: list[datetime] = []
    while t < end and len(out) < count:
        wd = (t.weekday() + 1) % 7
        day_ok = ((t.day in doms) and (wd in dows)) if (dom_any or dow_any) else ((t.day in doms) or (wd in dows))
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
        elif not day_ok:
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
        elif t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
        elif t.minute not in mins:
            t += timedelta(minutes=1)
        else:
            out.append(t)
            t += timedelta(minutes=1)
    return out


# ---------------------------------------------------------------- talking to the server
def read_script(user: str = "") -> str:
    """Shell script printing a crontab and the server's clock. user "" = the connected
    user, "@system" = /etc/crontab and /etc/cron.d (read-only), else that user's crontab."""
    if user == "@system":
        body = ('for f in /etc/crontab /etc/cron.d/*; do [ -f "$f" ] && { echo "# ---- $f"; cat "$f"; }; done '
                '2>/dev/null')
    else:
        who = f"-u {shlex.quote(user)} " if user else ""
        body = f"crontab {who}-l 2>&1"
    return f"echo @@cron; {body}\necho @@date; date '+%Y-%m-%d %H:%M'\necho @@tz; date +%Z\n"


def parse_read(text: str) -> tuple[str, datetime | None, str]:
    """(crontab text, the server's current time, its time zone name)."""
    from .dashboard import split_sections
    s = split_sections(text)
    body = s.get("cron", "")
    if body.lstrip().lower().startswith("no crontab"):
        body = ""
    now = None
    try:
        now = datetime.strptime(s.get("date", "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        pass
    return body, now, s.get("tz", "").strip()


def save_command(user: str, text: str) -> str:
    """Command that replaces the crontab with `text` (sent base64 so quoting can't break it)."""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    who = f"-u {shlex.quote(user)} " if user else ""
    return f"sh -c {shlex.quote(f'echo {b64} | base64 -d | crontab {who}-')}"


# ---------------------------------------------------------------- the command part of a job
# The form builds the command line from: the command, an optional folder to run in, an
# optional login shell (so PATH and the environment match a real login) and where the
# output goes. decompose() reads a line back into those parts when it matches exactly.
OUT_DEFAULT, OUT_DISCARD, OUT_LOG = "default", "discard", "log"
_INTERPRETERS = {"bash", "sh", "dash", "zsh", "python", "python3", "node", "php", "perl", "ruby", "java"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_PATH = re.compile(r"[\w./$~{}+@:-]+")


def shell_path(path: str) -> str:
    """`path` quoted for sh, but with a leading ~ left to expand (it doesn't inside quotes)."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(path)


def _unshell_path(tok: str) -> str:
    if tok.startswith("~/"):
        rest = tok[2:]
        return "~/" + (shlex.split(rest)[0] if rest else "")
    return shlex.split(tok)[0]


def escape_percent(s: str) -> str:
    """cron turns an unescaped % into a newline: write \\% instead."""
    return re.sub(r"(?<!\\)%", r"\\%", s)


def unescape_percent(s: str) -> str:
    return s.replace("\\%", "%")


def split_output(cmd: str) -> tuple[str, str, str]:
    """(command, output mode, log path) for a command ending in a redirect we generate."""
    m = re.search(r"\s*>>\s*(\S+)\s+2>&1\s*$", cmd)
    if m:
        return cmd[:m.start()], OUT_LOG, m.group(1)
    m = re.search(r"\s*>\s*/dev/null\s+2>&1\s*$", cmd)
    if m:
        return cmd[:m.start()], OUT_DISCARD, ""
    return cmd, OUT_DEFAULT, ""


def join_output(cmd: str, mode: str, log: str = "") -> str:
    if mode == OUT_DISCARD:
        return f"{cmd} >/dev/null 2>&1"
    if mode == OUT_LOG and log.strip():
        path = log.strip()
        return f"{cmd} >> {path if _SAFE_PATH.fullmatch(path) else shlex.quote(path)} 2>&1"
    return cmd


@dataclass
class Parts:
    command: str = ""
    folder: str = ""
    login: bool = False
    out: str = OUT_DEFAULT
    log: str = ""


def compose(p: Parts) -> str:
    cmd = p.command.strip()
    if p.folder.strip():
        cmd = f"cd {shell_path(p.folder.strip())} && {cmd}"
    if p.login:
        cmd = f"bash -lc {shlex.quote(cmd)}"
    return escape_percent(join_output(cmd, p.out, p.log))


def decompose(line: str) -> Parts:
    """Split a crontab command back into form fields; anything unusual stays in `command`."""
    line = line.strip()
    plain = Parts(command=unescape_percent(line))
    try:
        s, out, log = split_output(unescape_percent(line))
        parts = Parts(out=out, log=log)
        tok = shlex.split(s)
        if len(tok) == 3 and tok[0] == "bash" and tok[1] == "-lc":
            s, parts.login = tok[2], True
        m = re.match(r"cd\s+(~/'[^']*'|~/\S*|'[^']*'|\"[^\"]*\"|[^\s&]+)\s+&&\s+(.*)$", s, re.S)
        if m:
            parts.folder, s = _unshell_path(m.group(1)), m.group(2)
        parts.command = s.strip()
    except ValueError:
        return plain
    return parts if compose(parts) == line else plain


def script_path(command: str) -> tuple[str, bool] | None:
    """The script a command starts (path, needs the executable bit), when it has a full path.
    `bash /x/y.sh` and `python3 /x/y.py` name the script without needing +x."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    i = 0
    while i < len(tokens) and (_ASSIGN.match(tokens[i]) or tokens[i] in ("sudo", "nohup", "nice", "env", "time")):
        i += 1
    if i >= len(tokens):
        return None
    first = tokens[i]
    if first.rsplit("/", 1)[-1] in _INTERPRETERS:
        for a in tokens[i + 1:]:
            if not a.startswith("-"):
                return (a, False) if a.startswith(("/", "~")) else None
        return None
    return (first, True) if first.startswith(("/", "~")) else None


def check_script(path: str, need_exec: bool) -> str:
    """Shell script printing ok | missing | dir | noexec for `path`."""
    return (f'p={shell_path(path)}; if [ ! -e "$p" ]; then echo missing; elif [ -d "$p" ]; then echo dir; '
            f'elif [ -f "$p" ] && [ ! -x "$p" ] && {"true" if need_exec else "false"}; then echo noexec; '
            f'else echo ok; fi')


def list_dir_command(path: str = "") -> str:
    """Command printing the folder's full path, then its entries (folders end in /)."""
    return f"cd {shell_path(path)} 2>&1 && pwd && ls -1Ap 2>&1" if path else "cd && pwd && ls -1Ap 2>&1"


def parse_listing(out: str) -> tuple[str, list[str], list[str]]:
    """(folder, sub-folders, files) from list_dir_command's output; ValueError with the reason."""
    lines = out.splitlines()
    if not lines or not lines[0].startswith("/"):
        raise ValueError((lines[0] if lines else "Could not open that folder.").split(": ", 1)[-1])
    dirs = sorted(l[:-1] for l in lines[1:] if l.endswith("/"))
    files = sorted(l for l in lines[1:] if l and not l.endswith("/"))
    return lines[0], dirs, files
