"""Firewall tab helpers: read the rules of firewalld / ufw / iptables / nftables and build the
commands to open or close a port, start the firewall and save the rules. No Qt here.
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
  echo @@rules; nft -a list ruleset 2>&1
else
  echo @@manager; echo none
fi
"""

EDITABLE = ("firewalld", "ufw", "iptables", "nftables")
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
        """Can rules be added and removed right now?"""
        return self.manager in EDITABLE and self.state in ("running", "active") and not self.needs_root

    @property
    def can_start(self) -> bool:
        return self.manager in ("firewalld", "ufw") and self.state not in ("running", "active") and not self.needs_root

    @property
    def why_not_editable(self) -> str:
        """In words, why the buttons are off ("" when they are on)."""
        if self.manager == "none":
            return "No firewall tool found (firewalld, ufw, iptables, nftables)."
        if self.needs_root:
            return "Reading the rules needs root: connect as root, or use an account with sudo (it asks for the password)."
        if self.can_start:
            return f"{self.manager} isn't running: use “Start firewall” (it allows SSH first)."
        return ""


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


def _parse_nft(text: str) -> list[Rule]:
    """Rules of `nft -a list ruleset`: the scope is "family table chain", the handle is what deletes a rule."""
    rules: list[Rule] = []
    family = table = chain = ""
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"table\s+(\S+)\s+(\S+)\s*\{", line)
        if m:
            family, table, chain = m.group(1), m.group(2), ""
            continue
        m = re.match(r"chain\s+(\S+)\s*\{", line)
        if m:
            chain = m.group(1)
            continue
        if line == "}":
            if chain:
                chain = ""
            else:
                family = table = ""
            continue
        if not chain:
            continue
        scope = f"{family} {table} {chain}"
        h = re.search(r"#\s*handle\s+(\d+)\s*$", line)
        if h:
            rules.append(Rule(scope, "rule", line[:h.start()].strip(), f"handle {h.group(1)}", int(h.group(1))))
        elif line.startswith("type ") and "hook" in line:
            rules.append(Rule(scope, "policy", re.sub(r"\s+", " ", line.rstrip(";"))))
    return rules


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
        fw.rules = _parse_nft(s.get("rules", ""))
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


def parse_port_proto(text: str) -> tuple[str, str]:
    """"8080/tcp", "8080" or "8000-8100/udp" -> ("8080", "tcp"); ValueError when it isn't one."""
    port, _, proto = text.strip().partition("/")
    proto = (proto or "tcp").lower()
    if proto not in ("tcp", "udp") or not normalize_port(port, "ufw"):
        raise ValueError("Give a port like 8080, 8080/tcp or 8000-8100/udp (1 to 65535).")
    return port.strip(), proto


_NFT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def nft_input_chain(rules: list[Rule]) -> str:
    """"family table chain" of the chain that filters incoming traffic ("" when there is none)."""
    for r in rules:
        if r.kind == "policy" and " hook input" in " " + r.value:
            return r.scope
    return ""


def add_port_command(manager: str, port: str, proto: str, zone: str = "", target: str = "") -> str:
    """Open a port. `zone` is for firewalld; `target` ("family table chain") is for nftables."""
    z = f"--zone={shlex.quote(zone)} " if zone else ""
    if manager == "firewalld":
        return _chain(f"firewall-cmd {z}--permanent --add-port={port}/{proto}", "firewall-cmd --reload")
    if manager == "ufw":
        return f"ufw allow {port}/{proto}"
    if manager == "iptables":
        return f"iptables -I INPUT -p {proto} --dport {port.replace('-', ':')} -j ACCEPT"
    if manager == "nftables":
        parts = target.split()
        if len(parts) != 3 or not all(_NFT_NAME.fullmatch(p) for p in parts):
            raise ValueError("No chain for incoming traffic was found in the nftables rules.")
        return f"nft insert rule {parts[0]} {parts[1]} {parts[2]} {proto} dport {port.replace(':', '-')} accept"
    raise ValueError(manager)


def add_service_command(service: str, zone: str = "") -> str:
    z = f"--zone={shlex.quote(zone)} " if zone else ""
    return _chain(f"firewall-cmd {z}--permanent --add-service={shlex.quote(service)}", "firewall-cmd --reload")


def remove_command(manager: str, rule: Rule) -> str | None:
    """Command removing `rule`, or None when that kind of rule can't be removed from here."""
    if manager == "ufw" and rule.number:
        return f"ufw --force delete {int(rule.number)}"
    if manager == "iptables":
        if rule.kind != "rule" or not rule.scope:
            return None                              # the default policy of a chain isn't a rule
        try:
            args = shlex.split(rule.value)
        except ValueError:
            return None
        return "iptables -D " + shlex.quote(rule.scope) + " " + " ".join(shlex.quote(a) for a in args)
    if manager == "nftables":
        parts = rule.scope.split()
        if rule.kind != "rule" or not rule.number or len(parts) != 3 or not all(_NFT_NAME.fullmatch(p) for p in parts):
            return None
        return f"nft delete rule {parts[0]} {parts[1]} {parts[2]} handle {int(rule.number)}"
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


def save_command(manager: str) -> str | None:
    """Make the running iptables / nftables rules survive a reboot (firewalld and ufw already keep theirs)."""
    if manager == "iptables":
        return "sh -c " + shlex.quote(
            "if [ -f /etc/sysconfig/iptables ]; then iptables-save > /etc/sysconfig/iptables; "
            "elif [ -f /etc/iptables/rules.v4 ]; then iptables-save > /etc/iptables/rules.v4; "
            "else echo 'No saved-rules file found: install iptables-services (RHEL) or iptables-persistent "
            "(Debian) first' >&2; exit 1; fi")
    if manager == "nftables":
        return "sh -c " + shlex.quote(
            "cp -a /etc/nftables.conf /etc/nftables.conf.bak-$(date +%Y%m%d-%H%M%S) 2>/dev/null; "
            "{ echo '#!/usr/sbin/nft -f'; echo 'flush ruleset'; nft list ruleset; } > /etc/nftables.conf.new "
            "&& mv /etc/nftables.conf.new /etc/nftables.conf")
    return None


def action_label(manager: str) -> str:
    """What the second button does: reload (firewalld, ufw) or save so it survives a reboot (iptables, nftables)."""
    return "Save rules" if manager in ("iptables", "nftables") else "Reload rules"


def start_command(manager: str, ssh_port: int = 22) -> str | None:
    """Turn the firewall on, allowing SSH first so this connection is not cut."""
    if manager == "firewalld":
        return "systemctl enable --now firewalld"
    if manager == "ufw":
        return "sh -c " + shlex.quote(f"ufw allow {int(ssh_port)}/tcp && ufw --force enable")
    return None


def protects_ssh(rule: Rule, ssh_port: int) -> bool:
    """Would removing this rule probably cut the SSH connection? (never offered from here)"""
    v = rule.value.lower()
    if rule.kind == "service" and v == "ssh":
        return True
    if rule.kind == "rule" and rule.scope and ("accept" in v or "-j accept" in v):
        for m in re.finditer(r"(?:--dports?|dport)\s+([0-9,:{}\s-]+)", v):
            for part in re.split(r"[,\s{}]+", m.group(1).strip()):
                a = re.fullmatch(r"(\d+)(?:[:-](\d+))?", part)
                if a and int(a.group(1)) <= ssh_port <= int(a.group(2) or a.group(1)):
                    return True
    m = re.match(r"(\d+)(?:[-:](\d+))?(?:/\w+)?", v)
    if m and rule.kind in ("port", "allow", "limit"):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        return a <= ssh_port <= b
    return False
