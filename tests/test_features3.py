"""Docker, SSH keys, systemd timers and security checks: parsing and command building."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import cron, docker, security, sshkeys, timers  # noqa: E402

# ---------------------------------------------------------------- docker
PS = ('@@engine\ndocker\n@@ps\n'
      '{"ID":"a1b2c3d4e5f6","Names":"web","Image":"nginx:1.25","Status":"Up 3 hours","Ports":"0.0.0.0:80->80/tcp","State":"running"}\n'
      '{"ID":"ffeeddccbbaa","Names":"old-job","Image":"alpine","Status":"Exited (0) 2 days ago","Ports":""}\n'
      '{"ID":"112233445566","Names":"db","Image":"mysql","Status":"Up 1 minute (Paused)","Ports":""}\n'
      '@@stats\n{"ID":"a1b2c3d4e5f6","Name":"web","CPUPerc":"0.50%","MemUsage":"12.3MiB / 1.9GiB"}\n')


def test_docker_containers_are_listed_with_state_and_stats():
    c = docker.parse(PS)
    assert c.engine == "docker" and not c.needs_access
    assert [(x.name, x.state) for x in c.items] == [("web", "running"), ("db", "paused"), ("old-job", "exited")]
    web = c.items[0]
    assert (web.id, web.image, web.cpu, web.mem, web.running) == ("a1b2c3d4e5f6", "nginx:1.25", "0.50%", "12.3MiB", True)
    assert not c.items[2].running


def test_docker_access_problems_and_missing_engine():
    denied = "@@engine\ndocker\n@@ps\nGot permission denied while trying to connect to the Docker daemon socket\n"
    assert docker.parse(denied).needs_access and docker.parse(denied).items == []
    assert docker.parse("@@engine\nnone\n").engine == "none"
    assert docker.parse("").engine == "none"


def test_docker_commands():
    assert docker.action_command("docker", "stop", "web") == "docker stop web"
    assert docker.action_command("podman", "remove", "web", force=True) == "podman rm -f web"
    assert docker.action_command("docker", "restart", "a b") == "docker restart 'a b'"
    assert docker.logs_command("docker", "web", 50) == "docker logs --tail 50 web 2>&1"
    for bad in (("rkt", "stop"), ("docker", "exec")):
        try:
            docker.action_command(bad[0], bad[1], "x")
            raise AssertionError(bad)
        except ValueError:
            pass


# ---------------------------------------------------------------- ssh keys
ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl alice@laptop"


def test_key_parsing_fingerprint_and_options():
    ks = sshkeys.parse("# team keys\n\n" + ED + '\ncommand="/bin/date",no-pty ' + ED.replace("alice", "bob") +
                       "\nnot a key line\n")
    assert [(k.type, k.comment, k.valid) for k in ks] == [("ssh-ed25519", "alice@laptop", True),
                                                          ("ssh-ed25519", "bob@laptop", True), ("", "", False)]
    assert ks[0].fingerprint.startswith("SHA256:") and ks[0].fingerprint == ks[1].fingerprint
    assert ks[1].options == 'command="/bin/date",no-pty'
    assert sshkeys.fingerprint("!!!") == ""


def test_key_validation_add_remove():
    assert sshkeys.validate_public_key(ED) == ""
    assert "PRIVATE" in sshkeys.validate_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert sshkeys.validate_public_key("") and sshkeys.validate_public_key("hello world")
    text, added = sshkeys.add_key("", ED)
    assert added and text == ED + "\n"
    again, added2 = sshkeys.add_key(text, ED.replace("alice@laptop", "renamed"))
    assert not added2 and again == text                       # same key material: not added twice
    text2, _ = sshkeys.add_key(text, ED.replace("AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
                                                "AAAAC3NzaC1lZDI1NTE5AAAAIPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP") + "")
    assert sshkeys.remove_key(text2, ED) != text2 and ED not in sshkeys.remove_key(text2, ED)
    assert sshkeys.remove_key(ED + "\n", ED) == ""
    assert sshkeys.clean_read("cat: /home/x/.ssh/authorized_keys: No such file or directory") == ""
    assert sshkeys.clean_read(ED) == ED + "\n"


def test_key_save_command_sets_owner_and_permissions():
    cmd = sshkeys.save_command("/home/bob", "bob", ED + "\n")
    for part in ("mkdir -p /home/bob/.ssh", "chmod 700", "chmod 600", "chown -R bob:", "restorecon"):
        assert part in cmd, part
    assert "alice" not in cmd                                  # the key text travels base64-encoded
    assert sshkeys.read_command("/home/bob") == "cat /home/bob/.ssh/authorized_keys 2>&1"


# ---------------------------------------------------------------- timers
TIMERS = """@@list
## logrotate.timer
Description=Daily rotation of log files
ActiveState=active
UnitFileState=enabled
Unit=logrotate.service
NextElapseUSecRealtime=Tue 2026-10-06 00:00:00 UTC
LastTriggerUSec=Mon 2026-10-05 00:00:01 UTC
TimersCalendar={ OnCalendar=daily ; next_elapse=Tue 2026-10-06 00:00:00 UTC }
## backup.timer
Description=Nightly backup
ActiveState=inactive
UnitFileState=disabled
Unit=backup.service
NextElapseUSecRealtime=
LastTriggerUSec=0
TimersMonotonic={ OnBootSec=1min ; next_elapse=n/a }
"""


def test_timers_are_parsed():
    t = timers.parse(TIMERS)
    assert [x.name for x in t] == ["backup.timer", "logrotate.timer"]
    lr = t[1]
    assert (lr.unit, lr.active, lr.enabled) == ("logrotate.service", "active", "enabled")
    assert lr.schedule == "OnCalendar=daily" and lr.next == "Tue 2026-10-06 00:00:00 UTC"
    assert t[0].next == "–" and t[0].last == "–" and t[0].schedule == "OnBootSec=1min"


def test_cron_schedule_to_oncalendar():
    c = timers.calendar_from_cron
    assert c("30 2 * * *") == "*-*-* 2:30:00" and c("*/5 * * * *") == "*-*-* *:*/5:00"
    assert c("0 9 * * 1-5") == "Mon..Fri *-*-* 9:0:00" and c("0 6 * * 0") == "Sun *-*-* 6:0:00"
    assert c("0 3 15 * *") == "*-*-15 3:0:00" and c("@daily") == "*-*-* 0:0:00"
    assert c("@reboot") is None and c("0 0 13 * 5") is None and c("bad") is None


def test_timer_units_and_command():
    svc, tm = timers.build_units("Backup", 'tar czf /b/x-$(date +%F).tgz "/srv/data" >> /var/log/b.log 2>&1',
                                 user="backup", calendar="*-*-* 2:30:00")
    assert 'ExecStart=/bin/sh -c "tar czf /b/x-$$(date +%%F).tgz \\"/srv/data\\" >> /var/log/b.log 2>&1"' in svc
    assert "Type=oneshot" in svc and "User=backup" in svc
    assert "OnCalendar=*-*-* 2:30:00" in tm and "Persistent=true" in tm and "WantedBy=timers.target" in tm
    assert "OnBootSec=1min" in timers.build_units("x", "/bin/x", boot=True)[1]
    cmd = timers.create_command("nightly", svc, tm)
    assert "already exists" in cmd and "systemctl enable --now nightly.timer" in cmd and "tar czf" not in cmd
    assert timers.toggle_command("a.timer", False) == "systemctl disable --now a.timer"
    assert timers.run_now_command("a.service") == "systemctl start a.service"


# ---------------------------------------------------------------- security
GOOD = """@@sshd
/etc/ssh/sshd_config:PermitRootLogin no
/etc/ssh/sshd_config:PasswordAuthentication no
@@uid0
root
@@emptypw
@@failed
3
@@failedfile
0
@@sudoers
@@selinux
Enforcing
@@firewall
running
@@listen
tcp   LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
tcp   LISTEN 0 128 127.0.0.1:3306 0.0.0.0:*
"""

BAD = """@@sshd
/etc/ssh/sshd_config:PermitRootLogin yes
/etc/ssh/sshd_config:PasswordAuthentication yes
/etc/ssh/sshd_config:PermitEmptyPasswords yes
@@uid0
root
toor
@@emptypw
guest
@@failed
4500
@@failedfile
cat: /var/log/secure: Permission denied
@@sudoers
%wheel ALL=(ALL) NOPASSWD: ALL
@@selinux
Permissive
@@firewall
not running
ERROR: You need to be root
1
@@listen
tcp   LISTEN 0 128 *:22 *:*
tcp   LISTEN 0 128 *:80 *:*
tcp   LISTEN 0 128 [::]:443 [::]:*
"""

DENIED = """@@sshd
grep: /etc/ssh/sshd_config: Permission denied
@@emptypw
awk: cannot open /etc/shadow (Permission denied)
@@sudoers
grep: /etc/sudoers: Permission denied
"""


def test_security_good_server_has_no_problems():
    f = security.parse(GOOD)
    assert all(x.level == security.OK for x in f), [(x.title, x.level) for x in f]
    assert any(x.title == "Reachable from outside" and "22" in x.result for x in f)
    assert not security.needs_root(f)


def test_security_bad_server_is_flagged_worst_first():
    f = security.parse(BAD)
    by = {x.title: x for x in f}
    assert by["SSH root login"].level == security.BAD and by["SSH empty passwords"].level == security.BAD
    assert by["Accounts with root rights (UID 0)"].result == "toor" and by["Accounts without a password"].result == "guest"
    assert by["SSH password login"].level == security.WARN and by["Failed SSH logins (24 h)"].result == "4500"
    assert by["sudo without a password"].result == "1 rule" and by["SELinux"].level == security.WARN
    assert by["Firewall"].level == security.WARN and "443" in by["Reachable from outside"].result
    levels = [x.level for x in f]
    assert levels == sorted(levels, key=lambda l: {"bad": 0, "warn": 1, "unknown": 2, "ok": 3}[l])


def test_security_says_unknown_when_root_is_needed():
    f = security.parse(DENIED)
    by = {x.title: x for x in f}
    assert by["SSH settings"].level == security.UNKNOWN and by["Accounts without a password"].level == security.UNKNOWN
    assert by["sudo without a password"].level == security.UNKNOWN and security.needs_root(f)
