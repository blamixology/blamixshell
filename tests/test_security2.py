"""The extra security checks (each explains itself), the reconnect plumbing of the shared context,
and the firewall and details parts of the desktop dashboard."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import collect, dashboard as d, security as sec  # noqa: E402
from test_collect import FakeRunner  # noqa: E402

RICH = """@@sshd
/etc/ssh/sshd_config:PermitRootLogin yes
/etc/ssh/sshd_config:PasswordAuthentication yes
/etc/ssh/sshd_config:MaxAuthTries 10
/etc/ssh/sshd_config:X11Forwarding yes
@@uid0
root
@@emptypw
@@failed
250
@@failedfile
0
@@failedips
    120 203.0.113.9
     80 198.51.100.7
@@sudoers
@@selinux
n/a
@@firewall
not running
0
@@listen
tcp   LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))
tcp   LISTEN 0 128 0.0.0.0:6379 0.0.0.0:* users:(("redis-server",pid=2,fd=6))
tcp   LISTEN 0 128 *:3306 *:* users:(("mysqld",pid=3,fd=9))
tcp   LISTEN 0 128 127.0.0.1:5432 0.0.0.0:*
@@perms
644 root:root /etc/passwd
644 root:shadow /etc/shadow
644 root:root /etc/group
640 root:shadow /etc/gshadow
440 root:root /etc/sudoers
664 root:root /etc/ssh/sshd_config
@@fail2ban
missing
@@autoupd
disabled
@@logins
bob      pts/0        10.0.0.9         Tue Oct  6 10:00   still logged in
wtmp begins Mon Oct  5 08:00:01 2026
@@updates-yum
openssl.x86_64   1.0.2k-26.el7   updates
bash.x86_64      4.2.46-35.el7   updates
"""


def by(findings):
    return {f.title: f for f in findings}


def test_more_checks_each_with_why_how_and_the_facts():
    f = by(sec.parse(RICH))
    assert f["SSH login attempts per connection"].result == "10" and f["SSH X11 forwarding"].level == sec.WARN
    ips = f["Failed SSH logins (24 h)"]
    assert ips.result == "250" and "203.0.113.9" in ips.details and "fail2ban" in ips.fix
    assert f["Brute-force protection (fail2ban)"].level == sec.WARN and "apt install fail2ban" in f["Brute-force protection (fail2ban)"].fix
    risky = f["Databases and admin ports open to the world"]
    assert risky.level == sec.BAD and "Redis (6379)" in risky.result and "MySQL / MariaDB (3306)" in risky.result
    assert "127.0.0.1" in risky.fix and "PostgreSQL" not in risky.result          # only reachable locally: not listed
    outside = f["Reachable from outside"]
    assert "redis-server" in outside.details and outside.result.startswith("3 ports")
    perms = f["Sensitive file permissions"]
    assert perms.level == sec.BAD and "/etc/shadow: readable by everyone (644)" in perms.details
    assert "/etc/ssh/sshd_config: writable by group" in perms.details and "chmod 640 /etc/shadow" in perms.fix
    upd = f["Pending package updates"]
    assert upd.level == sec.WARN and upd.result == "2 waiting" and "openssl" in upd.details and "sudo yum update" in upd.fix
    auto = f["Automatic security updates"]
    assert auto.level == sec.WARN and "yum-cron" in auto.fix
    assert f["Recent logins"].level == sec.OK and "bob" in f["Recent logins"].details
    assert f["Firewall"].level == sec.WARN and "Firewall tab" in f["Firewall"].fix


def test_a_finding_explains_itself_in_one_text():
    root = by(sec.parse(RICH))["SSH root login"]
    text = root.text()
    assert text.splitlines()[0] == "SSH root login" and "Status: problem" in text
    assert "Why it matters / what to do" in text and "How to fix it" in text and "sshd -t" in text and "PermitRootLogin yes" in text
    fine = sec.Finding(sec.OK, "SELinux", "enforcing").text()
    assert "Status: fine" in fine and "How to fix it" not in fine


def test_healthy_extras_are_ok_and_missing_sections_add_nothing():
    good = RICH.replace("PermitRootLogin yes", "PermitRootLogin no").replace("PasswordAuthentication yes", "PasswordAuthentication no")
    assert sec.parse("@@sshd\n/etc/ssh/sshd_config:PermitRootLogin no\n@@uid0\nroot\n@@emptypw\n")        # the old, short answers still work
    titles = [f.title for f in sec.parse("@@uid0\nroot\n")]
    assert "Sensitive file permissions" not in titles and "Pending package updates" not in titles
    f = by(sec.parse(good.replace("0.0.0.0:6379", "127.0.0.1:6379").replace("*:3306", "127.0.0.1:3306")
                     .replace("644 root:shadow /etc/shadow", "640 root:shadow /etc/shadow")
                     .replace("664 root:root /etc/ssh/sshd_config", "644 root:root /etc/ssh/sshd_config")))
    assert f["SSH root login"].level == sec.OK and f["Sensitive file permissions"].level == sec.OK
    assert "Databases and admin ports open to the world" not in f


def test_the_table_carries_each_findings_explanation():
    t = collect.load_security(collect.Context(FakeRunner({sec.READ_SCRIPT: RICH}), root=True))
    assert t.details and all(t.details) and t.describe(0).startswith(t.rows[0][1])
    plain = collect.Table(["A", "B"])
    plain.add(["x", "y"])
    assert plain.describe(0) == "A: x\nB: y" and plain.describe(5) == ""


def test_the_context_comes_back_by_itself_and_closes_what_it_replaced():
    closed = []

    class Client:
        def __init__(self, name, alive=True):
            self.name, self._alive = name, alive

        def close(self):
            closed.append(self.name)

        def get_transport(self):
            return SimpleNamespace(is_active=lambda: self._alive)

    old = Client("old", alive=False)
    ctx = collect.Context(SimpleNamespace(client=old, run=lambda *a, **k: None))
    ctx.chain = [Client("jump-old")]
    assert not ctx.alive() and collect.Context(FakeRunner()).alive()          # unknown counts as up
    try:
        ctx.reconnect()
        raise AssertionError("no opener")
    except RuntimeError:
        pass
    asked = []
    ctx.opener = lambda interactive: (asked.append(interactive) or Client("new"), [Client("jump-new")])
    orig_runner = d.Runner
    d.Runner = lambda client: SimpleNamespace(client=client, run=lambda *a, **k: None)
    try:
        ctx.reconnect(interactive=True)
    finally:
        d.Runner = orig_runner
    assert asked == [True] and closed == ["old", "jump-old"]
    assert ctx.runner.client.name == "new" and ctx.alive() and [c.name for c in ctx.chain] == ["jump-new"]
    ctx.close()
    assert closed[-2:] == ["new", "jump-new"] and ctx.chain == []


def test_reboot_and_shutdown_now_are_marked_as_dropping_the_connection():
    acts = {a.label: a for a in collect.actions_for("system", None)}
    assert acts["Reboot now"].drops == "reboot" and acts["Shut down now"].drops == "shutdown"
    assert not acts["Reboot in a while…"].drops and not acts["Set the time zone…"].drops
