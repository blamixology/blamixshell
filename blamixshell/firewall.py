"""Firewall tab helpers: read the rules of firewalld / ufw / iptables / nftables and build the
commands to open or close a port. firewalld and ufw can be edited; the rest is read-only.
No Qt here.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

READ_SCRIPT = r"""
if command -v firewall-cmd >/dev/null 2>&1; then
  echo @@manager; echo firewalld
  echo @@state; firewall-cmd --state 2>&1
  echo @@default; firewall-cmd --get-default-zone 2>&1
  echo @@zones
  for z in $(firewall-cmd --get-active-zones 2>/dev/null | grep -v '^[[:space:]]'); do
    echo "## $z"; firewall-cmd --zone="$z" --list-all 2>&1
  done
elif command -v ufw >/dev/null 2>&1; then
  echo @@manager; echo ufw
  echo @@state; ufw status verbose 2>&1
  echo @@rules; ufw status numbered 2>&1
elif command -v iptables >/dev/null 2>&1; then
  echo @@manager; echo iptables
  echo @@rules; iptables -S 2>&1
elif command -v nft >/dev/null 2>&1; then
  echo @@manager; echo nftables
  echo @@rules; nft list ruleset 2>&1
else
  echo @@manager; echo none
fi
"""

EDITABLE = ("firewalld", "ufw")
_NEEDS_ROOT = re.compile(r"you must be root|need to be root|permission denied|must be root|not authorized|"
                         r"authorization failed|are you root", re.I)


@dataclass
class Rule:
    scope: str                # zone (firewalld) / chain (iptables) / "" (ufw)
    kind: str                 # service | port | rich rule | source | forward-port | protocol | rule | policy
    value: str
    detail: str = ""
    number: int = 0           # ufw rule number


@dataclass
class Firewall:
    manager: str = "none"
    state: str = ""           # "running" / "active" / "inactive" / "" (unknown)
    default_zone: str = ""
    rules: list[Rule] = field(default_factory=list)
    raw: str = ""
    needs_root: bool = False

    @property
    def editable(self) -> bool:
        return self.manager in EDITABLE and self.state in ("running", "active")


def needs_root(text: str) -> bool:
    return bool(_NEEDS_ROOT.search(text))


def _parse_firewalld(zones: str) -> list[Rule]:
    rules: list[Rule] = []
    zone, in_rich = "", False
    keys = {"services": "service", "ports": "port", "source-ports": "source port", "protocols": "protocol",
            "forward-ports": "forward-port", "sources": "source", "interfaces": "interface"}
    for line in zones.splitlines():
        if line.startswith("## "):
            zone, in_rich = line[3:].strip(), False
            continue
        if not line.strip():
            continue
        if in_rich and (line.startswith((" ", "\t"))) and "rule " in line:
            rules.append(Rule(zone, "rich rule", line.strip()))
            continue
        m = re.match(r"^\s+([a-z -]+):\s*(.*)$", line)
        if not m:
            continue
        key, rest = m.group(1).strip(), m.group(2).strip()
        in_rich = key == "rich rules"
        kind = keys.get(key)
        if kind and rest:
            if kind in ("forward-port",):
                rules.extend(Rule(zone, kind, v) for v in rest.split("\n") if v.strip())
            else:
                rules.extend(Rule(zone, kind, tok) for tok in rest.split())
    return rules


_UFW = re.compile(r"^\[\s*(\d+)\]\s+(.*?)\s{2,}(ALLOW|DENY|REJECT|LIMIT)(?:\s+(IN|OUT|FWD))?\s+(.*)$")


def _parse_ufw(text: str) -> list[Rule]:
    out = []
    for line in text.splitlines():
        m = _UFW.match(line.strip())
        if m:
            num, to, action, direction, frm = m.groups()
            out.append(Rule("", action.lower(), to.strip(), f"from {frm.strip()}" + (f" ({direction})" if direction else ""),
                            int(num)))
    return out


def _parse_iptables(text: str) -> list[Rule]:
    out = []
    for line in text.splitlines():
        p = line.split(None, 2)
        if len(p) >= 3 and p[0] == "-P":
            out.append(Rule(p[1], "policy", p[2]))
        elif len(p) >= 3 and p[0] == "-A":
            out.append(Rule(p[1], "rule", " ".join(line.split()[2:])))
    return out


def parse(text: str) -> Firewall:
    from .dashboard import split_sections
    s = split_sections(text)
    fw = Firewall(manager=s.get("manager", "none").strip() or "none", raw=text)
    fw.needs_root = needs_root(text) and fw.manager in ("ufw", "iptables", "nftables", "firewalld")
    state = s.get("state", "").strip()
    if fw.manager == "firewalld":
        fw.state = "running" if state.splitlines()[:1] == ["running"] else "inactive" if state else ""
        fw.default_zone = s.get("default", "").strip().splitlines()[0] if s.get("default", "").strip() else ""
        fw.rules = _parse_firewalld(s.get("zones", ""))
    elif fw.manager == "ufw":
        m = re.search(r"Status:\s*(\w+)", state)
        fw.state = m.group(1).lower() if m else ""
        fw.rules = _parse_ufw(s.get("rules", ""))
    elif fw.manager == "iptables":
        fw.state = "active"
        fw.rules = _parse_iptables(s.get("rules", ""))
    elif fw.manager == "nftables":
        fw.state = "active"
        fw.rules = [Rule("ruleset", "rule", l.strip()) for l in s.get("rules", "").splitlines() if l.strip()]
    return fw


# ---------------------------------------------------------------- changing rules
def normalize_port(text: str, manager: str) -> str:
    """"80", "8000-8100" or "8000:8100" -> the form the manager wants; "" when it isn't a port."""
    m = re.fullmatch(r"(\d{1,5})(?:[-:](\d{1,5}))?", text.strip())
    if not m:
        return ""
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not (1 <= a <= 65535 and 1 <= b <= 65535 and a <= b):
        return ""
    if a == b:
        return str(a)
    return f"{a}-{b}" if manager == "firewalld" else f"{a}:{b}"


def _chain(*steps: str) -> str:
    return f"sh -c {shlex.quote(' && '.join(steps))}"


def add_port_command(manager: str, port: str, proto: str, zone: str = "") -> str:
    z = f"--zone={shlex.quote(zone)} " if zone else ""
    if manager == "firewalld":
        return _chain(f"firewall-cmd {z}--permanent --add-port={port}/{proto}", "firewall-cmd --reload")
    if manager == "ufw":
        return f"ufw allow {port}/{proto}"
    raise ValueError(manager)


def add_service_command(service: str, zone: str = "") -> str:
    z = f"--zone={shlex.quote(zone)} " if zone else ""
    return _chain(f"firewall-cmd {z}--permanent --add-service={shlex.quote(service)}", "firewall-cmd --reload")


def remove_command(manager: str, rule: Rule) -> str | None:
    """Command removing `rule`, or None when that kind of rule can't be removed from here."""
    if manager == "ufw" and rule.number:
        return f"ufw --force delete {int(rule.number)}"
    if manager != "firewalld":
        return None
    z = f"--zone={shlex.quote(rule.scope)} " if rule.scope else ""
    flag = {"service": "--remove-service", "port": "--remove-port", "source port": "--remove-source-port",
            "protocol": "--remove-protocol", "source": "--remove-source", "rich rule": "--remove-rich-rule",
            "forward-port": "--remove-forward-port"}.get(rule.kind)
    if not flag:
        return None
    return _chain(f"firewall-cmd {z}--permanent {flag}={shlex.quote(rule.value)}", "firewall-cmd --reload")


def reload_command(manager: str) -> str | None:
    return {"firewalld": "firewall-cmd --reload", "ufw": "ufw reload"}.get(manager)


def protects_ssh(rule: Rule, ssh_port: int) -> bool:
    """Would removing this rule probably cut the SSH connection? (never offered from here)"""
    v = rule.value.lower()
    if rule.kind == "service" and v == "ssh":
        return True
    m = re.match(r"(\d+)(?:[-:](\d+))?(?:/\w+)?", v)
    if m and rule.kind in ("port", "allow", "limit"):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        return a <= ssh_port <= b
    return False
