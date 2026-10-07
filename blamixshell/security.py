"""Quick security checks for the Security tab: what is probably wrong, from a few read-only commands. Some
answers need root; those say "unknown" until the checks are run with sudo. Every finding can explain itself:
why it matters, how to fix it and the facts behind it (shown when you double-click it). No Qt here."""
from __future__ import annotations

import re
from dataclasses import dataclass

OK, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"

READ_SCRIPT = r"""
echo @@sshd; grep -HEi '^[[:space:]]*(PermitRootLogin|PasswordAuthentication|PermitEmptyPasswords|Port|MaxAuthTries|X11Forwarding|AllowUsers|AllowGroups)[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>&1
echo @@uid0; awk -F: '$3==0{print $1}' /etc/passwd 2>&1
echo @@emptypw; awk -F: '($2==""){print $1}' /etc/shadow 2>&1
echo @@failed; (journalctl --no-pager --since "24 hours ago" 2>/dev/null | grep -c "Failed password") 2>/dev/null || true
echo @@failedfile; (cat /var/log/secure /var/log/auth.log 2>&1 | grep -c "Failed password") 2>&1 || true
echo @@failedips; (journalctl --no-pager --since "24 hours ago" 2>/dev/null; cat /var/log/secure /var/log/auth.log 2>/dev/null) | grep "Failed password" | awk '{for(i=1;i<NF;i++) if($i=="from") print $(i+1)}' | sort | uniq -c | sort -rn | head -5
echo @@sudoers; grep -rhE 'NOPASSWD' /etc/sudoers /etc/sudoers.d 2>&1
echo @@selinux; (getenforce 2>/dev/null || echo n/a)
echo @@firewall; (firewall-cmd --state 2>&1 | head -1; ufw status 2>&1 | head -1; iptables -S 2>&1 | wc -l)
echo @@listen; ss -tulnp 2>/dev/null | tail -n +2
echo @@perms; stat -c '%a %U:%G %n' /etc/passwd /etc/shadow /etc/group /etc/gshadow /etc/sudoers /etc/ssh/sshd_config 2>/dev/null
echo @@fail2ban; if command -v fail2ban-client >/dev/null 2>&1; then systemctl is-active fail2ban 2>/dev/null || echo installed; else echo missing; fi
echo @@autoupd; systemctl is-enabled unattended-upgrades apt-daily-upgrade.timer dnf-automatic.timer dnf-automatic-install.timer yum-cron 2>/dev/null
echo @@logins; last -n 6 2>/dev/null | head -6
if command -v apt >/dev/null 2>&1; then echo @@updates-apt; apt list --upgradable 2>/dev/null
elif command -v dnf >/dev/null 2>&1; then echo @@updates-dnf; dnf -q check-update 2>/dev/null
elif command -v yum >/dev/null 2>&1; then echo @@updates-yum; yum -q check-update 2>/dev/null; fi
"""

_NEEDS_ROOT = re.compile(r"permission denied|operation not permitted|must be root|not permitted|a password is required",
                         re.I)

# services that should rarely face the internet, and how bad it usually is when they do
RISKY_PORTS = {2375: ("Docker API without TLS", BAD), 6379: ("Redis", BAD), 27017: ("MongoDB", BAD),
               11211: ("Memcached", BAD), 9200: ("Elasticsearch", BAD), 5984: ("CouchDB", WARN),
               3306: ("MySQL / MariaDB", WARN), 5432: ("PostgreSQL", WARN), 1433: ("SQL Server", WARN),
               3389: ("Remote Desktop", WARN), 5900: ("VNC", WARN), 23: ("Telnet", BAD), 21: ("FTP", WARN)}


@dataclass
class Finding:
    level: str                # ok | warn | bad | unknown
    title: str
    result: str
    advice: str = ""          # one line, shown in the table
    details: str = ""         # the facts behind it (a list of ports, accounts, files ...)
    fix: str = ""             # how to fix it: commands and where

    def text(self) -> str:
        """Everything about this finding, for the details window."""
        label = {OK: "fine", WARN: "warning", BAD: "problem", UNKNOWN: "could not be checked"}[self.level]
        out = [f"{self.title}", f"Status: {label}", f"Result: {self.result}"]
        if self.advice:
            out += ["", "Why it matters / what to do", self.advice]
        if self.fix:
            out += ["", "How to fix it", self.fix]
        if self.details:
            out += ["", "Details", self.details]
        return "\n".join(out)


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


def _listening(body: str) -> list[tuple[str, str, int, str]]:
    """(protocol, address, port, process) for what listens on every interface (0.0.0.0, *, ::)."""
    seen, out = set(), []
    for line in body.splitlines():
        p = line.split()
        if len(p) < 5 or p[0] not in ("tcp", "udp"):
            continue
        addr, _, port = p[4].rpartition(":")
        if addr in ("0.0.0.0", "*", "[::]", "::") and port.isdigit():
            m = re.search(r'users:\(\("([^"]+)"', line)
            key = (p[0], int(port))
            if key not in seen:
                seen.add(key)
                out.append((p[0], "all interfaces", int(port), m.group(1) if m else ""))
    return sorted(out, key=lambda x: x[2])


def _perm_findings(body: str) -> Finding | None:
    rules = []                # (path, what is wrong)
    for line in body.splitlines():
        p = line.split(None, 2)
        if len(p) < 3 or not p[0].isdigit():
            continue
        mode, owner, path = int(p[0], 8), p[1], p[2].strip()
        other, group = mode & 0o007, (mode >> 3) & 0o007
        if path in ("/etc/shadow", "/etc/gshadow") and other:
            rules.append((path, f"readable by everyone ({p[0]}): password hashes can be copied"))
        elif path in ("/etc/passwd", "/etc/group") and (other & 0o2 or group & 0o2):
            rules.append((path, f"writable by group or others ({p[0]}): anyone could add an account"))
        elif path == "/etc/sudoers" and (other or group & 0o2):
            rules.append((path, f"too open ({p[0]}): should be 440"))
        elif path == "/etc/ssh/sshd_config" and (other & 0o2 or group & 0o2):
            rules.append((path, f"writable by group or others ({p[0]})"))
    if not body.strip():
        return None
    if not rules:
        return Finding(OK, "Sensitive file permissions", "as expected", details=body.strip())
    fixes = {"/etc/shadow": "chmod 640 /etc/shadow   # or 000 / 600; owner root", "/etc/gshadow": "chmod 640 /etc/gshadow",
             "/etc/passwd": "chmod 644 /etc/passwd", "/etc/group": "chmod 644 /etc/group",
             "/etc/sudoers": "chmod 440 /etc/sudoers", "/etc/ssh/sshd_config": "chmod 644 /etc/ssh/sshd_config"}
    return Finding(BAD, "Sensitive file permissions", f"{len(rules)} file{'s' if len(rules) != 1 else ''} too open",
                   "Account and password files must not be readable or writable by other users.",
                   details="\n".join(f"{path}: {why}" for path, why in rules) + "\n\nAll checked:\n" + body.strip(),
                   fix="\n".join(fixes[path] for path, _ in rules))


def _update_findings(s: dict[str, str]) -> Finding | None:
    from .dashboard import parse_updates
    for mgr in ("apt", "dnf", "yum"):
        if f"updates-{mgr}" in s:
            _m, ups = parse_updates(f"@@{mgr}\n{s[f'updates-{mgr}']}\n")
            cmd = {"apt": "apt update && apt upgrade", "dnf": "dnf upgrade", "yum": "yum update"}[mgr]
            if not ups:
                return Finding(OK, "Pending package updates", "none (from the cached package lists)")
            lst = "\n".join(f"{u.package} {u.version}" for u in ups[:60]) + (f"\n… and {len(ups) - 60} more" if len(ups) > 60 else "")
            return Finding(BAD if len(ups) >= 30 else WARN, "Pending package updates", f"{len(ups)} waiting",
                           "Updates often contain security fixes: install them, then reboot if the kernel changed.",
                           details=lst, fix=f"sudo {cmd}   # or use the Updates tab")
    return None


def parse(text: str) -> list[Finding]:
    s = _sections(text)
    f: list[Finding] = []

    ssh = _sshd(s.get("sshd", ""))
    reload_hint = "then:  sudo sshd -t && sudo systemctl reload sshd    (keep this session open until you have tested a new login)"
    if s.get("sshd", "").strip() and not _NEEDS_ROOT.search(s["sshd"]):
        raw = s["sshd"]
        root = ssh.get("permitrootlogin")
        if root in ("no",):
            f.append(Finding(OK, "SSH root login", "disabled", details=raw))
        elif root in ("prohibit-password", "without-password", "forced-commands-only"):
            f.append(Finding(OK, "SSH root login", f"keys only ({root})", details=raw))
        elif root == "yes":
            f.append(Finding(BAD, "SSH root login", "allowed with a password",
                             "Set PermitRootLogin no (or prohibit-password) in /etc/ssh/sshd_config.",
                             details=raw, fix=f"PermitRootLogin prohibit-password   # in /etc/ssh/sshd_config\n{reload_hint}"))
        else:
            f.append(Finding(WARN, "SSH root login", "not set explicitly",
                             "Older OpenSSH allows it by default: set PermitRootLogin prohibit-password.",
                             details=raw, fix=f"PermitRootLogin prohibit-password   # in /etc/ssh/sshd_config\n{reload_hint}"))
        pw = ssh.get("passwordauthentication")
        if pw == "no":
            f.append(Finding(OK, "SSH password login", "disabled (keys only)", details=raw))
        else:
            f.append(Finding(WARN, "SSH password login", "enabled" if pw == "yes" else "on by default",
                             "Passwords can be guessed: use keys and set PasswordAuthentication no.", details=raw,
                             fix="1. Make sure your SSH key works (add it in the Users tab, SSH keys).\n"
                                 "2. PasswordAuthentication no   # in /etc/ssh/sshd_config\n" + reload_hint))
        if ssh.get("permitemptypasswords") == "yes":
            f.append(Finding(BAD, "SSH empty passwords", "allowed", "Set PermitEmptyPasswords no.", details=raw,
                             fix=f"PermitEmptyPasswords no   # in /etc/ssh/sshd_config\n{reload_hint}"))
        tries = ssh.get("maxauthtries", "")
        if tries.isdigit() and int(tries) > 6:
            f.append(Finding(WARN, "SSH login attempts per connection", tries,
                             "A high limit lets a script try many passwords per connection.", details=raw,
                             fix=f"MaxAuthTries 4   # in /etc/ssh/sshd_config\n{reload_hint}"))
        if ssh.get("x11forwarding") == "yes":
            f.append(Finding(WARN, "SSH X11 forwarding", "enabled",
                             "Only needed for graphical programs over SSH; it widens what a login can reach.",
                             details=raw, fix=f"X11Forwarding no   # in /etc/ssh/sshd_config\n{reload_hint}"))
    else:
        f.append(Finding(UNKNOWN, "SSH settings", "could not read sshd_config", "Run the checks with sudo.",
                         details=s.get("sshd", "").strip()))

    extra = [u for u in s.get("uid0", "").split() if u != "root"]
    f.append(Finding(BAD, "Accounts with root rights (UID 0)", ", ".join(extra), "Only root should have UID 0.",
                     details="\n".join(extra), fix="Check each one (usermod -u <new uid> <name>) or remove it (userdel <name>).")
             if extra else Finding(OK, "Accounts with root rights (UID 0)", "only root"))

    ep = s.get("emptypw", "").strip()
    if _NEEDS_ROOT.search(ep) or (not ep and "emptypw" not in s):
        f.append(Finding(UNKNOWN, "Accounts without a password", "needs root to check", "Run the checks with sudo."))
    elif ep:
        f.append(Finding(BAD, "Accounts without a password", ", ".join(ep.split()),
                         "Lock them (usermod -L) or set a password.", details="\n".join(ep.split()),
                         fix="usermod -L <name>      # lock the account\npasswd <name>         # or give it a password"))
    else:
        f.append(Finding(OK, "Accounts without a password", "none"))

    counts = []
    for key in ("failed", "failedfile"):
        body = s.get(key, "").strip()
        if body.isdigit():
            counts.append(int(body))
    ips = [l.strip() for l in s.get("failedips", "").splitlines() if l.strip() and not _NEEDS_ROOT.search(l)]
    failed_n = max(counts) if counts else 0
    if counts and max(counts) > 0 or (counts and not _NEEDS_ROOT.search(s.get("failedfile", ""))):
        n = max(counts)
        f.append(Finding(WARN if n >= 100 else OK, "Failed SSH logins (24 h)", str(n),
                         "Many failures usually mean automated guessing: keys only, a firewall, or fail2ban."
                         if n >= 100 else "",
                         details=("Top sources (count, address):\n" + "\n".join(ips)) if ips else "",
                         fix="Block them with fail2ban (see below), or allow SSH only from known addresses in the firewall."
                         if n >= 100 else ""))
    else:
        f.append(Finding(UNKNOWN, "Failed SSH logins", "log not readable", "Run the checks with sudo."))

    f2b = s.get("fail2ban", "").strip().lower()
    if f2b:
        if f2b == "active":
            f.append(Finding(OK, "Brute-force protection (fail2ban)", "running"))
        elif f2b in ("installed", "inactive", "failed", "unknown"):
            f.append(Finding(WARN, "Brute-force protection (fail2ban)", "installed but not running",
                             "It bans addresses that keep failing to log in.",
                             fix="systemctl enable --now fail2ban"))
        elif failed_n >= 50:
            f.append(Finding(WARN, "Brute-force protection (fail2ban)", "not installed",
                             "With this many failed logins, banning repeat offenders helps.",
                             fix="apt install fail2ban     # Debian / Ubuntu\nyum install epel-release && yum install fail2ban    # CentOS / RHEL\n"
                                 "systemctl enable --now fail2ban"))
        else:
            f.append(Finding(OK, "Brute-force protection (fail2ban)", "not installed (few failed logins)"))

    sud = s.get("sudoers", "").strip()
    if _NEEDS_ROOT.search(sud):
        f.append(Finding(UNKNOWN, "sudo without a password", "needs root to check", "Run the checks with sudo."))
    elif sud:
        rules = [l.strip() for l in sud.splitlines() if l.strip() and not l.strip().startswith("#")]
        n = len(rules)
        f.append(Finding(WARN if n else OK, "sudo without a password", f"{n} rule{'s' if n != 1 else ''}",
                         "NOPASSWD gives anyone who reaches that account full root: keep it to what's needed.",
                         details="\n".join(rules), fix="Edit with  visudo  (or a file in /etc/sudoers.d/) and remove NOPASSWD where it isn't needed."))
    else:
        f.append(Finding(OK, "sudo without a password", "no NOPASSWD rules"))

    se = s.get("selinux", "").strip().lower()
    if se == "enforcing":
        f.append(Finding(OK, "SELinux", "enforcing"))
    elif se in ("permissive", "disabled"):
        f.append(Finding(WARN, "SELinux", se, "Enforcing mode blocks a lot of attacks; switch when you can test it.",
                         fix="Set SELINUX=enforcing in /etc/selinux/config, then reboot.\nTest first with:  setenforce 1"))

    fw = [l.strip() for l in s.get("firewall", "").splitlines() if l.strip()]
    text_fw = " ".join(fw).lower()
    if any(l.lower() in ("running", "status: active") for l in fw):
        f.append(Finding(OK, "Firewall", "running", details="\n".join(fw)))
    elif fw and fw[-1].isdigit() and int(fw[-1]) > 4 and "denied" not in text_fw:
        f.append(Finding(OK, "Firewall", "iptables rules loaded", details="\n".join(fw)))
    elif _NEEDS_ROOT.search(text_fw):
        f.append(Finding(UNKNOWN, "Firewall", "needs root to check", "Run the checks with sudo."))
    else:
        f.append(Finding(WARN, "Firewall", "no active firewall found", "Enable firewalld or ufw and allow only what's needed.",
                         details="\n".join(fw), fix="Open the Firewall tab: Start the firewall (it allows SSH first), then Add rule."))

    listening = _listening(s.get("listen", ""))
    if listening:
        lines = [f"{proto:3} {port:<6} {proc or '(process not visible without root)'}" for proto, _a, port, proc in listening]
        f.append(Finding(WARN if len(listening) > 4 else OK, "Reachable from outside",
                         f"{len(listening)} port{'s' if len(listening) != 1 else ''}: "
                         + ", ".join(str(p[2]) for p in listening[:12]),
                         "Every open port is attack surface: close what isn't needed (Firewall tab)."
                         if len(listening) > 4 else "", details="\n".join(lines),
                         fix="Close it in the firewall, or make the program listen on 127.0.0.1 only."
                         if len(listening) > 4 else ""))
        risky = [(p[2], RISKY_PORTS[p[2]]) for p in listening if p[2] in RISKY_PORTS]
        if risky:
            worst = BAD if any(lvl == BAD for _p, (_n, lvl) in risky) else WARN
            f.append(Finding(worst, "Databases and admin ports open to the world",
                             ", ".join(f"{name} ({port})" for port, (name, _l) in risky),
                             "These usually have no business facing the internet, and several are often left without a password.",
                             details="\n".join(f"{port}  {name}" for port, (name, _l) in risky),
                             fix="Bind them to 127.0.0.1 or a private network, and block the port in the firewall.\n"
                                 "Example (Redis): bind 127.0.0.1 in redis.conf, then restart it."))

    perm = _perm_findings(s.get("perms", ""))
    if perm:
        f.append(perm)

    upd = _update_findings(s)
    if upd:
        f.append(upd)

    auto = s.get("autoupd", "").split()
    if "autoupd" in s:
        on = [x for x in auto if x == "enabled"]
        f.append(Finding(OK if on else WARN, "Automatic security updates", "enabled" if on else "not enabled",
                         "" if on else "Unpatched servers are the usual way in; automatic updates close the gap between your visits.",
                         fix="" if on else "apt install unattended-upgrades    # Debian / Ubuntu\n"
                                          "yum install yum-cron && systemctl enable --now yum-cron    # CentOS 7\n"
                                          "dnf install dnf-automatic && systemctl enable --now dnf-automatic.timer    # newer RHEL / Fedora"))

    logins = s.get("logins", "").strip()
    if logins:
        f.append(Finding(OK, "Recent logins", f"{len([l for l in logins.splitlines() if l.strip() and not l.startswith(('wtmp', 'reboot'))])} listed",
                         details=logins))

    order = {BAD: 0, WARN: 1, UNKNOWN: 2, OK: 3}
    return sorted(f, key=lambda x: order[x.level])


def needs_root(findings: list[Finding]) -> bool:
    return any(x.level == UNKNOWN for x in findings)
