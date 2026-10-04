"""Quick security checks for the Security tab: what is probably wrong, from a few read-only
commands. Some answers need root; those say "unknown" until the checks are run with sudo.
No Qt here."""
from __future__ import annotations

import re
from dataclasses import dataclass

OK, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"

READ_SCRIPT = r"""
echo @@sshd; grep -HEi '^[[:space:]]*(PermitRootLogin|PasswordAuthentication|PermitEmptyPasswords|Port|MaxAuthTries)[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>&1
echo @@uid0; awk -F: '$3==0{print $1}' /etc/passwd 2>&1
echo @@emptypw; awk -F: '($2==""){print $1}' /etc/shadow 2>&1
echo @@failed; (journalctl --no-pager --since "24 hours ago" 2>/dev/null | grep -c "Failed password") 2>/dev/null || true
echo @@failedfile; (cat /var/log/secure /var/log/auth.log 2>&1 | grep -c "Failed password") 2>&1 || true
echo @@sudoers; grep -rhE 'NOPASSWD' /etc/sudoers /etc/sudoers.d 2>&1
echo @@selinux; (getenforce 2>/dev/null || echo n/a)
echo @@firewall; (firewall-cmd --state 2>&1 | head -1; ufw status 2>&1 | head -1; iptables -S 2>&1 | wc -l)
echo @@listen; ss -tuln 2>/dev/null | tail -n +2
"""

_NEEDS_ROOT = re.compile(r"permission denied|operation not permitted|must be root|not permitted|a password is required",
                         re.I)


@dataclass
class Finding:
    level: str                # ok | warn | bad | unknown
    title: str
    result: str
    advice: str = ""


def _sections(text: str) -> dict[str, str]:
    from .dashboard import split_sections
    return split_sections(text)


def _sshd(body: str) -> dict[str, str]:
    """Last value of each sshd option (a later line wins in sshd_config)."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        line = line.split(":", 1)[-1] if line.startswith("/etc/") else line
        p = line.split(None, 1)
        if len(p) == 2:
            out[p[0].lower()] = p[1].strip().lower()
    return out


def parse(text: str) -> list[Finding]:
    s = _sections(text)
    f: list[Finding] = []

    ssh = _sshd(s.get("sshd", ""))
    if s.get("sshd", "").strip() and not _NEEDS_ROOT.search(s["sshd"]):
        root = ssh.get("permitrootlogin")
        if root in ("no",):
            f.append(Finding(OK, "SSH root login", "disabled"))
        elif root in ("prohibit-password", "without-password", "forced-commands-only"):
            f.append(Finding(OK, "SSH root login", f"keys only ({root})"))
        elif root == "yes":
            f.append(Finding(BAD, "SSH root login", "allowed with a password",
                             "Set PermitRootLogin no (or prohibit-password) in /etc/ssh/sshd_config."))
        else:
            f.append(Finding(WARN, "SSH root login", "not set explicitly",
                             "Older OpenSSH allows it by default: set PermitRootLogin prohibit-password."))
        pw = ssh.get("passwordauthentication")
        if pw == "no":
            f.append(Finding(OK, "SSH password login", "disabled (keys only)"))
        else:
            f.append(Finding(WARN, "SSH password login", "enabled" if pw == "yes" else "on by default",
                             "Passwords can be guessed: use keys and set PasswordAuthentication no."))
        if ssh.get("permitemptypasswords") == "yes":
            f.append(Finding(BAD, "SSH empty passwords", "allowed", "Set PermitEmptyPasswords no."))
    else:
        f.append(Finding(UNKNOWN, "SSH settings", "could not read sshd_config", "Run the checks with sudo."))

    extra = [u for u in s.get("uid0", "").split() if u != "root"]
    f.append(Finding(BAD, "Accounts with root rights (UID 0)", ", ".join(extra), "Only root should have UID 0.")
             if extra else Finding(OK, "Accounts with root rights (UID 0)", "only root"))

    ep = s.get("emptypw", "").strip()
    if _NEEDS_ROOT.search(ep) or (not ep and "emptypw" not in s):
        f.append(Finding(UNKNOWN, "Accounts without a password", "needs root to check", "Run the checks with sudo."))
    elif ep:
        f.append(Finding(BAD, "Accounts without a password", ", ".join(ep.split()),
                         "Lock them (usermod -L) or set a password."))
    else:
        f.append(Finding(OK, "Accounts without a password", "none"))

    counts = []
    for key in ("failed", "failedfile"):
        body = s.get(key, "").strip()
        if body.isdigit():
            counts.append(int(body))
    if counts and max(counts) > 0 or (counts and not _NEEDS_ROOT.search(s.get("failedfile", ""))):
        n = max(counts)
        f.append(Finding(WARN if n >= 100 else OK, "Failed SSH logins (24 h)", str(n),
                         "Many failures usually mean automated guessing: keys only, a firewall, or fail2ban."
                         if n >= 100 else ""))
    else:
        f.append(Finding(UNKNOWN, "Failed SSH logins", "log not readable", "Run the checks with sudo."))

    sud = s.get("sudoers", "").strip()
    if _NEEDS_ROOT.search(sud):
        f.append(Finding(UNKNOWN, "sudo without a password", "needs root to check", "Run the checks with sudo."))
    elif sud:
        n = len([l for l in sud.splitlines() if l.strip() and not l.strip().startswith("#")])
        f.append(Finding(WARN if n else OK, "sudo without a password", f"{n} rule{'s' if n != 1 else ''}",
                         "NOPASSWD gives anyone who reaches that account full root: keep it to what's needed."))
    else:
        f.append(Finding(OK, "sudo without a password", "no NOPASSWD rules"))

    se = s.get("selinux", "").strip().lower()
    if se == "enforcing":
        f.append(Finding(OK, "SELinux", "enforcing"))
    elif se in ("permissive", "disabled"):
        f.append(Finding(WARN, "SELinux", se, "Enforcing mode blocks a lot of attacks; switch when you can test it."))

    fw = [l.strip() for l in s.get("firewall", "").splitlines() if l.strip()]
    text_fw = " ".join(fw).lower()
    if any(l.lower() in ("running", "status: active") for l in fw):
        f.append(Finding(OK, "Firewall", "running"))
    elif fw and fw[-1].isdigit() and int(fw[-1]) > 4 and "denied" not in text_fw:
        f.append(Finding(OK, "Firewall", "iptables rules loaded"))
    elif _NEEDS_ROOT.search(text_fw):
        f.append(Finding(UNKNOWN, "Firewall", "needs root to check", "Run the checks with sudo."))
    else:
        f.append(Finding(WARN, "Firewall", "no active firewall found", "Enable firewalld or ufw and allow only what's needed."))

    public = []
    for line in s.get("listen", "").splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] in ("tcp", "udp"):
            addr, _, port = p[4].rpartition(":")
            if addr in ("0.0.0.0", "*", "[::]", "::") and port not in public:
                public.append(port)
    if public:
        f.append(Finding(WARN if len(public) > 4 else OK, "Reachable from outside",
                         f"{len(public)} port{'s' if len(public) != 1 else ''}: " + ", ".join(sorted(public, key=int)[:12]),
                         "Every open port is attack surface: close what isn't needed (Firewall tab)."
                         if len(public) > 4 else ""))
    order = {BAD: 0, WARN: 1, UNKNOWN: 2, OK: 3}
    return sorted(f, key=lambda x: order[x.level])


def needs_root(findings: list[Finding]) -> bool:
    return any(x.level == UNKNOWN for x in findings)
