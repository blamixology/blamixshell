"""Server dashboard: parsers (real command output from several distros) + live checks."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import dashboard as d  # noqa: E402

OVERVIEW = """@@host
web-1
@@os
Ubuntu 24.04.1 LTS
@@kernel
6.8.0-45-generic
@@arch
x86_64
@@uptime
1036845.12 4085923.33
@@load
0.52 0.40 0.31 2/301 12345
@@nproc
4
@@mem
MemTotal:        8039876 kB
MemFree:          402312 kB
MemAvailable:    5023312 kB
Buffers:          201544 kB
Cached:          4012132 kB
SwapTotal:       2097148 kB
SwapFree:        1048574 kB
@@stat1
cpu  10000 0 5000 80000 1000 0 100 0 0 0
@@stat2
cpu  10030 0 5010 80050 1000 0 100 0 0 0
@@df
Filesystem     Type 1024-blocks     Used Available Capacity Mounted on
/dev/sda1      ext4    41152812 37000000   4152812      90% /
/dev/sdb1      xfs    104857600  1048576 103809024       1% /data
/dev/sdc1      ext4           0        0         0       -  /boot/efi
@@who
2
@@systemd
degraded
@@failed
nginx.service
certbot.service
@@uid
1000
@@end
"""


def test_overview_parser():
    ov = d.parse_overview(OVERVIEW)
    assert (ov.host, ov.os, ov.cpus, ov.load) == ("web-1", "Ubuntu 24.04.1 LTS", 4, (0.52, 0.40, 0.31))
    assert d.human_uptime(ov.uptime_s) == "12d 0h"
    assert round(ov.mem_percent) == 38 and round(ov.swap_percent) == 50
    # 90 jiffies passed, 50 of them idle -> 40/90 busy
    assert round(ov.cpu_percent, 1) == 44.4
    assert [x.mount for x in ov.disks] == ["/", "/data"]        # zero-size /boot/efi skipped
    assert round(ov.disks[0].percent) == 90
    assert ov.systemd == "degraded" and ov.failed_units == ["nginx.service", "certbot.service"]
    assert ov.sessions == 2 and not ov.root


def test_overview_on_a_minimal_system():
    ov = d.parse_overview("@@host\nbox\n@@os\nFreeBSD 14.0\n@@uid\n0\n@@end\n")
    assert ov.host == "box" and ov.cpu_percent is None and ov.disks == [] and ov.root and ov.systemd == ""


def test_df_without_type_column():
    disks = d.parse_df("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vda1 1000 250 750 25% /\n")
    assert disks[0].mount == "/" and disks[0].fstype == "" and disks[0].percent == 25


SERVICES = """@@units
  cron.service                 loaded    active   running Regular background program processing daemon
● nginx.service                loaded    failed   failed  A high performance web server
  ssh.service                  loaded    active   running OpenBSD Secure Shell server
  apt-daily.service            loaded    inactive dead    Daily apt download activities
@@files
cron.service                           enabled         enabled
nginx.service                          enabled         enabled
ssh.service                            enabled         enabled
apt-daily.service                      static          -
"""


def test_services_parser():
    svcs = d.parse_services(SERVICES)
    by = {s.unit: s for s in svcs}
    assert set(by) == {"cron.service", "nginx.service", "ssh.service", "apt-daily.service"}
    assert by["nginx.service"].failed and by["nginx.service"].description == "A high performance web server"
    assert by["cron.service"].sub == "running" and by["cron.service"].enabled == "enabled"
    assert by["apt-daily.service"].enabled == "static"


def test_ps_parser():
    out = d.parse_ps("    PID USER     %CPU %MEM   RSS     ELAPSED COMMAND\n"
                     "   1234 www-data 12.5  3.1 254000  2-03:04:05 nginx: worker process\n"
                     "      1 root      0.0  0.1  12000    12:00:01 /sbin/init splash\n")
    assert out[0].pid == 1234 and out[0].command == "nginx: worker process" and out[0].cpu == 12.5
    assert out[1].command == "/sbin/init splash"


def test_ss_and_netstat_parsers():
    ss = d.parse_ss('tcp LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
                    "tcp LISTEN 0 128 [::]:22 [::]:*\n"
                    "udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n")
    assert [(p.proto, p.address, p.port) for p in ss] == [("tcp", "::", "22"), ("tcp", "0.0.0.0", "80"),
                                                          ("udp", "127.0.0.53%lo", "53")]
    assert ss[1].process == "nginx (812)"
    ns = d.parse_netstat("Proto Recv-Q Send-Q Local Address Foreign Address State\n"
                         "tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN\n"
                         "tcp 0 0 10.0.0.5:22 10.0.0.9:51000 ESTABLISHED\n")
    assert [(p.proto, p.port) for p in ns] == [("tcp", "22")]


def test_proc_net_fallback():
    text = ("@@tcp\n  sl  local_address rem_address   st\n"
            "   0: 0100007F:08AE 00000000:0000 0A 00000000:00000000\n"      # 127.0.0.1:2222 LISTEN
            "   1: 0100007F:08AE 0100007F:C350 01 00000000:00000000\n"      # established: skipped
            "@@tcp6\n  sl  local_address rem_address st\n"
            "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A x\n"
            "@@udp\n  sl  local_address rem_address st\n"
            "   0: 00000000:0044 00000000:0000 07 x\n")
    got = [(p.proto, p.address, p.port) for p in d.parse_proc_net(text)]
    assert got == [("tcp", "*", "22"), ("tcp", "127.0.0.1", "2222"), ("udp", "*", "68")]


@pytest.mark.parametrize("text,manager,first", [
    ("@@apt\nListing...\nopenssl/noble-updates 3.0.13-0ubuntu3.4 amd64 [upgradable from: 3.0.13-0ubuntu3.1]\n"
     "curl/noble-security 8.5.0-2ubuntu10.4 amd64 [upgradable from: 8.5.0-2ubuntu10.1]\n", "apt", ("openssl", "3.0.13-0ubuntu3.4")),
    ("@@dnf\n\nkernel.x86_64    5.14.0-427.el9    baseos\nopenssl.x86_64   1:3.0.7-27.el9   baseos\n"
     "Obsoleting Packages\n", "dnf", ("kernel.x86_64", "5.14.0-427.el9")),
    ("@@zypper\nS | Repository | Name | Current Version | Available Version | Arch\n"
     "--+------------+------+-----------------+-------------------+-----\n"
     "v | Main       | curl | 8.0.1-1.1       | 8.0.1-2.1         | x86_64\n", "zypper", ("curl", "8.0.1-2.1")),
    ("@@pacman\nlinux 6.9.1.arch1-1 -> 6.9.2.arch1-1\n", "pacman", ("linux", "6.9.2.arch1-1")),
    ("@@apk\nmusl-1.2.5-r1 x86_64 {musl} (MIT) [upgradable from: musl-1.2.5-r0]\n", "apk", ("musl-1.2.5-r1", "")),
    ("@@none\n", "", None),
])
def test_updates_parsers(text, manager, first):
    got_manager, ups = d.parse_updates(text)
    assert got_manager == manager
    if first:
        assert (ups[0].package, ups[0].version) == first
        assert manager in d.UPGRADE_COMMANDS
    else:
        assert ups == []


def test_users_parser():
    accounts, sessions = d.parse_users(
        "@@passwd\nroot:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n"
        "deploy:x:1000:1000::/home/deploy:/bin/bash\nsvc:x:1001:1001::/srv:/usr/sbin/nologin\n"
        "nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n"
        "@@who\ndeploy   pts/0        2026-09-30 10:00 (10.0.0.9)\ndeploy   pts/1        2026-09-30 10:05 (10.0.0.9)\n")
    assert [(a.name, a.logged_in) for a in accounts] == [("root", 0), ("deploy", 2)]
    assert len(sessions) == 2


def test_action_commands_are_safe():
    assert d.service_action_command("restart", "nginx.service") == "systemctl restart nginx.service"
    assert d.service_action_command("start", "getty@tty1.service") == "systemctl start getty@tty1.service"
    with pytest.raises(ValueError):
        d.service_action_command("restart", "nginx; rm -rf /")
    with pytest.raises(ValueError):
        d.service_action_command("mask", "nginx.service")
    assert d.kill_command(1234) == "kill -TERM 1234" and d.kill_command("99", force=True) == "kill -KILL 99"
    assert d.logs_command("nginx.service", "err", 50).count("journalctl --no-pager -o short-iso -n 50 -u nginx.service -p err") == 1
    assert "'bad unit; x'" in d.logs_command("bad unit; x")         # quoted, never executed as shell


# ------------------------------------------------------------------ live (real OpenSSH)
LIVE = os.environ.get("BLAMIXSHELL_TEST_SSH", "")


@pytest.fixture
def runner(tmp_path, monkeypatch):
    if not LIVE:
        pytest.skip("no live sshd")
    monkeypatch.setenv("BLAMIXSHELL_HOME", str(tmp_path))
    from blamixshell.models import Server
    from blamixshell.ssh_core import UnknownHostKey, open_client, trust_host_key
    host, port, user, pw = LIVE.split(":", 3)
    s = Server(host=host, port=int(port), username=user, password=pw, keepalive=0)
    try:
        client, chain = open_client(s, lambda _i: None)
    except UnknownHostKey as e:
        trust_host_key(e.host_id, e.key)
        client, chain = open_client(s, lambda _i: None)
    yield d.Runner(client)
    client.close()


def test_live_dashboard(runner):
    ov = d.overview(runner)
    assert ov.host and ov.kernel and ov.cpus >= 1 and ov.mem_total_kb > 0
    assert ov.cpu_percent is not None and ov.disks
    ps = d.processes(runner)
    assert ps and all(p.pid > 0 for p in ps)
    assert any(p.port == LIVE.split(":")[1] for p in d.ports(runner))   # sshd's own port
    accounts, _ = d.users(runner)
    assert any(a.name == LIVE.split(":")[2] for a in accounts)
    svcs, problem = d.services(runner)
    if ov.systemd in ("running", "degraded"):          # CI runner: real systemd
        assert svcs and not problem and any(s.unit.startswith(("ssh", "cron")) for s in svcs)
    else:                                              # containers: no systemd, a clear message
        assert not svcs and problem
    manager, _ups = d.updates(runner)
    assert manager in ("apt", "dnf", "yum", "zypper", "pacman", "apk", "")
    r = d.run_privileged(runner, "true", root=ov.root)   # sudo -n without a password: must not hang
    assert isinstance(r.code, int)


def test_privileged_commands_never_put_the_password_on_the_command_line():
    calls = []

    class Fake:
        def run(self, command, timeout=30, stdin=None):
            calls.append((command, stdin))
            return d.Result(0, "", "")
    f = Fake()
    d.run_privileged(f, "systemctl restart nginx.service", root=True)
    d.run_privileged(f, "systemctl restart nginx.service", root=False)
    d.run_privileged(f, "systemctl restart nginx.service", root=False, password="s3cret")
    assert calls[0] == ("systemctl restart nginx.service", None)                 # root: no sudo
    assert calls[1] == ("sudo -n systemctl restart nginx.service", None)         # never prompts/hangs
    assert calls[2] == ("sudo -S -p '' systemctl restart nginx.service", "s3cret\n")
    assert all("s3cret" not in c for c, _ in calls)


def test_cli_status_command_is_wired():
    from blamixshell.cli import HANDLERS, build_parser
    a = build_parser().parse_args(["status", "web-1", "-s"])
    assert a.cmd == "status" and a.services and HANDLERS["status"].__name__ == "cmd_status"
