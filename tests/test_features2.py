"""Storage parsing, systemd unit creation, status-bar alerts and the server report."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import cron, dashboard as d, firewall, report, storage, units  # noqa: E402

DF = """@@df
Filesystem     Type     1024-blocks     Used Available Capacity Mounted on
/dev/sda1      xfs         41931756 38000000   3931756      91% /
tmpfs          tmpfs         508000        0    508000       0% /dev/shm
/dev/sdb1      ext4       103081248 20000000  77000000      21% /data disk
@@inodes
Filesystem      Inodes   IUsed    IFree IUse% Mounted on
/dev/sda1      2097152  900000  1197152   43% /
tmpfs           127000       1   126999    1% /dev/shm
/dev/sdb1      6553600  100000  6453600    2% /data disk
"""


def test_filesystems_with_inodes_and_virtual_flag():
    fs = storage.parse_filesystems(DF)
    by = {f.mount: f for f in fs}
    assert by["/"].inode_percent == 43 and round(by["/"].percent) == 91
    assert by["/data disk"].fstype == "ext4" and by["/data disk"].inode_percent == 2     # a mount with a space
    assert by["/dev/shm"].virtual and not by["/"].virtual


def test_du_parsing_and_command():
    out = "9000\t/var\n5000\t/var/lib\n3000\t/var/log\n100\t/var/tmp\n"
    total, inside = storage.parse_du(out, "/var")
    assert total == 9000 and inside == [(5000, "/var/lib"), (3000, "/var/log"), (100, "/var/tmp")]
    assert storage.parse_du("", "/") == (0, [])
    assert storage.du_command("/srv/my data", 10) == "du -xk -d 1 -- '/srv/my data' 2>/dev/null | sort -rn | head -n 11"
    assert storage.parent("/var/log/") == "/var" and storage.parent("/var") == "/" and storage.parent("/") == "/"


def test_unit_is_built_and_validated():
    text = units.build_unit("My app", "/usr/bin/node /srv/app.js --fmt=%F", user="deploy", workdir="/srv",
                            restart="on-failure", env=["PORT=8080", 'MSG=say "hi"', "bad line"])
    assert "ExecStart=/usr/bin/node /srv/app.js --fmt=%%F" in text                 # % escaped for systemd
    assert "User=deploy" in text and "WorkingDirectory=/srv" in text and "Restart=on-failure" in text
    assert 'Environment="PORT=8080"' in text and 'Environment="MSG=say \\"hi\\""' in text and "bad line" not in text
    assert "WantedBy=multi-user.target" in text and "Restart=" not in units.build_unit("x", "/bin/x", restart="no")
    assert units.valid_name("my-app") and units.valid_name("app@1") and not units.valid_name("a b")
    assert not units.valid_name("x.service") and not units.valid_name("../etc")
    assert units.exec_error("node app.js") and units.exec_error("") and not units.exec_error("-/usr/bin/x")


def test_create_command_refuses_to_overwrite_and_stops_on_error():
    cmd = units.create_command("my-app", "[Unit]\n", enable=True, start=False)
    assert cmd.startswith("sh -c ") and "already exists" in cmd and "daemon-reload" in cmd
    assert "systemctl enable my-app" in cmd and "systemctl start" not in cmd and " && " in cmd
    assert "base64 -d > /etc/systemd/system/my-app.service" in cmd and "[Unit]" not in cmd     # sent encoded


def test_alerts_only_for_real_problems():
    ok = d.Health(cpu=10, mem=50, disk=60, load=0.5, cpus=4, swap=None)
    assert d.health_alerts(ok) == {}
    bad = d.Health(mem=93, disk=95, load=7.0, cpus=4, swap=70)
    assert set(d.health_alerts(bad)) == {"disk", "mem", "swap", "load"}
    assert d.health_alerts(bad)["load"] == "load 7.00 on 4 CPUs"
    assert d.health_alerts(d.Health(load=1.4, cpus=1)) == {}        # under 1.5 per CPU


def test_report_has_every_section_and_says_what_is_missing():
    ov = d.Overview(host="web-1", os="CentOS 7", kernel="3.10", arch="x86_64", uptime_s=90000, cpus=4,
                    cpu_percent=12, mem_total_kb=8000000, mem_avail_kb=2000000,
                    disks=[d.Disk("/", "xfs", 100, 91, 9)])
    svc = ([d.Service("nginx.service", "loaded", "failed", "failed", "web | server")], "")
    accounts, _ = d.parse_users("@@passwd\nbob:x:1001:1001::/home/bob:/bin/bash\n@@who\n")
    data = {"overview": ov, "services": svc, "updates": ("yum", [d.Update("openssl", "1.0.2k")]),
            "ports": [d.Port("tcp", "0.0.0.0", "22", "sshd")], "users": (accounts, [], []),
            "cron": cron.parse_crontab("*/5 * * * * /x.sh\n#off# 0 1 * * * /y.sh\n"),
            "firewall": firewall.parse("@@manager\nufw\n@@state\nStatus: active\n@@rules\n")}
    md = report.build("Web 1", "bob@web-1:22", datetime(2026, 10, 5, 12, 0), data)
    for part in ("# Server report: Web 1", "## Overview", "## Services", "## Pending updates", "## Listening ports",
                 "## Accounts", "## Scheduled jobs", "## Firewall", "**1 failed**", "openssl", "all interfaces",
                 "web \\| server", "| bob |", "*/5 * * * *", "ufw", "2026-10-05 12:00"):
        assert part in md, part
    empty = report.build("X", "x", datetime(2026, 1, 1), {}, {"services": "connection lost"})
    assert "_Not available: connection lost_" in empty and "_Not available: not collected_" in empty
