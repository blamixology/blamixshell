"""The dashboard's tables without Qt (used by the terminal dashboard and the CLI report)."""
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import test_features3 as tf  # noqa: E402
import test_firewall as tfw  # noqa: E402
from blamixshell import collect, cron, dashboard as d, docker, firewall as fw, security, storage, timers  # noqa: E402


class FakeRunner:
    """Answers the dashboard's scripts from canned output; everything else is empty and fine."""

    def __init__(self, replies: dict[str, str] | None = None, sudo_needs_password: bool = False):
        self.replies = replies or {}
        self.calls: list[str] = []
        self.sudo_pw_needed = sudo_needs_password

    def run(self, command: str, timeout: float = 30, stdin: str | None = None):
        self.calls.append(command)
        if command.startswith("sudo -n true"):
            return d.Result(1 if self.sudo_pw_needed else 0, "", "")
        if command.startswith("sudo -S"):
            return d.Result(0, self.replies.get("sudo", ""), "")
        return d.Result(0, self.replies.get(command, ""), "")


def ctx_with(replies=None, **kw):
    return collect.Context(FakeRunner(replies, **kw), username="deploy")


def test_services_processes_ports_updates_tables():
    svcs = [d.Service("nginx.service", "loaded", "active", "running", "web", "enabled", "systemd"),
            d.Service("backup.service", "loaded", "failed", "failed", "backup", "enabled", "systemd")]
    with mock.patch.object(d, "services", lambda r: (svcs, "")), \
            mock.patch.object(d, "processes", lambda r, sort, limit=0: [d.Process(7, "root", 60.0, 1.5, 2048, "01:00", "sshd")]), \
            mock.patch.object(d, "ports", lambda r: [d.Port("tcp", "0.0.0.0", "22", "sshd")]), \
            mock.patch.object(d, "updates", lambda r: ("yum", [d.Update("openssl", "1.1")])):
        c = ctx_with()
        t = collect.load_services(c)
        assert [r[0] for r in t.rows] == ["backup.service", "nginx.service"] and t.styles == ["bad", ""]
        assert t.keys[0] == ("systemd", "backup.service")
        p = collect.load_processes(c)
        assert p.rows[0][:3] == ["7", "root", "60.0"] and p.styles == ["warn"] and p.keys[0][0] == 7
        assert collect.load_ports(c).rows == [["TCP", "all interfaces", "22", "sshd"]]
        u = collect.load_updates(c)
        assert u.rows == [["openssl", "1.1"]] and "1 update available (yum)" in u.note


def test_overview_has_meters_and_flags_problems():
    ov = d.Overview(host="web-1", os="CentOS 7", kernel="3.10", uptime_s=90000, cpus=2, cpu_percent=95,
                    load=(0.5, 0.4, 0.3), mem_total_kb=1000, mem_avail_kb=50, disks=[d.Disk("/", "xfs", 100, 95, 5)],
                    failed_units=["a.service"], init="systemd", systemd="degraded")
    with mock.patch.object(d, "overview", lambda r: ov):
        t = collect.load_overview(ctx_with())
    rows = {r[0]: (r[1], s) for r, s in zip(t.rows, t.styles)}
    assert rows["CPU"] == ("95%", "bad") and rows["Memory"][1] == "bad" and rows["/"] == ("95%", "bad")
    assert rows["Failed services"] == ("1", "bad") and "web-1" in t.note and "█" in t.rows[0][2]


def test_firewall_docker_timers_security_storage_cron_users_tables():
    c = ctx_with({fw.READ_SCRIPT: tfw.UFW, docker.READ_SCRIPT: tf.PS, timers.READ_SCRIPT: tf.TIMERS,
                  security.READ_SCRIPT: tf.BAD, storage.FS_SCRIPT: DF})
    f = collect.load_firewall(c)
    assert ("allow", "22/tcp") == (f.rows[0][1], f.rows[0][2]) and "ufw: active" in f.note and f.styles[1] == "bad"
    dk = collect.load_docker(c)
    assert [r[1] for r in dk.rows] == ["web", "db", "old-job"] and dk.keys[0] == ("docker", "a1b2c3d4e5f6", "web", "running")
    assert "1 running, 2 not running" in dk.note
    tm = collect.load_timers(c)
    assert [r[1] for r in tm.rows] == ["backup.timer", "logrotate.timer"] and tm.keys[1] == ("logrotate.timer", "logrotate.service", True)
    sec = collect.load_security(c)
    assert sec.rows[0][0] == "BAD" and sec.styles[0] == "bad" and any(r[1] == "Firewall" for r in sec.rows)
    sto = collect.load_storage(c)
    assert [r[0] for r in sto.rows] == ["/"] and sto.styles == ["bad"] and sto.rows[0][5] == "91%"
    cr = ctx_with({cron.read_script(""): "@@cron\n*/5 * * * * /x.sh\n#off# 0 1 * * * /y.sh\n@@date\n2026-10-06 10:00\n@@tz\nUTC\n"})
    t = collect.load_cron(cr)
    assert [(r[0], r[1]) for r in t.rows] == [("yes", "Every 5 minutes"), ("off", "Every day at 01:00")] and "UTC" in t.note
    accounts, _ = d.parse_users("@@passwd\nroot:x:0:0:root:/root:/bin/bash\nbob:x:1001:1001::/home/bob:/bin/bash\n@@who\n")
    with mock.patch.object(d, "users", lambda r: (accounts, [], [])):
        u = collect.load_users(c)
    assert [r[0] for r in u.rows] == ["root", "bob"] and u.styles[0] == "warn"


DF = ("@@df\nFilesystem Type 1024-blocks Used Available Capacity Mounted on\n/dev/a xfs 1000 910 90 91% /\n"
      "tmpfs tmpfs 10 0 10 0% /dev/shm\n@@inodes\nFilesystem Inodes IUsed IFree IUse% Mounted on\n/dev/a 100 80 20 80% /\n")


def test_reading_needs_sudo_and_asks_for_the_password_first():
    script = fw.READ_SCRIPT
    denied = "@@manager\nufw\n@@state\nERROR: You need to be root\n"
    c = ctx_with({script: denied}, sudo_needs_password=True)
    try:
        collect.load_firewall(c)
        raise AssertionError("should ask for sudo")
    except collect.NeedsSudo:
        pass
    root = collect.Context(FakeRunner({script: denied}), root=True)
    assert "needs root" in collect.load_firewall(root).note                        # root can't do better: says so
    c2 = ctx_with({script: denied, "sudo": tfw.UFW}, sudo_needs_password=True)
    c2.sudo_pw = "secret"
    t = collect.load_firewall(c2)                                                  # with the password: reads it as root
    assert t.rows and "ufw: active" in t.note
    assert any(call.startswith("sudo -S") for call in c2.runner.calls)


def test_actions_are_built_per_row():
    a = collect.actions_for("services", ("systemd", "nginx.service"))
    assert [x.label.split()[0] for x in a][:2] == ["Start", "Stop"] and any("restart" in x.command for x in a)
    assert all(x.command.startswith(("systemctl", "sudo", "sh -c")) or "nginx" in x.command for x in a)
    k = collect.actions_for("processes", (123, "node app.js"))
    assert [x.command for x in k] == ["kill -TERM 123", "kill -KILL 123"] and k[1].danger and k[0].allow_plain
    run = collect.actions_for("docker", ("docker", "abc", "web", "running"))
    assert [x.label.split()[0] for x in run][:3] == ["Stop", "Restart", "Remove"] and "rm -f abc" in run[2].command
    assert [x.label.split()[0] for x in run][3:5] == ["Details", "Processes"] and run[3].readonly
    assert any(x.label.startswith("Clean up") for x in run)                         # engine-wide ones follow
    assert [x.label.split()[0] for x in collect.actions_for("docker", ("docker", "abc", "db", "paused"))][0] == "Resume"
    assert collect.actions_for("docker", ("docker", "abc", "x", "exited"))[0].label.startswith("Start")
    tm = collect.actions_for("timers", ("a.timer", "a.service", True))
    assert [x.command for x in tm] == ["systemctl disable --now a.timer", "systemctl start a.service"]
    assert collect.actions_for("ports", None) == [] and collect.actions_for("services", None) == []
    assert "journalctl" in collect.log_command("services", ("systemd", "nginx.service")) or "nginx" in collect.log_command("services", ("systemd", "nginx.service"))
    assert collect.log_command("docker", ("docker", "abc", "web", "running")) == "docker logs --tail 300 abc 2>&1"
    assert collect.log_command("ports", None) is None


def test_run_asks_for_sudo_only_when_it_is_needed():
    c = ctx_with(sudo_needs_password=True)
    assert c.run("kill -TERM 1", allow_plain=True).ok                              # your own process: no sudo needed
    try:
        c.run("systemctl restart x")
        raise AssertionError("should ask for sudo")
    except collect.NeedsSudo:
        pass
    c.sudo_pw = "pw"
    assert c.run("systemctl restart x").ok and any(call.startswith("sudo -S") for call in c.runner.calls)


def test_report_collects_every_part_even_when_one_fails():
    with mock.patch.object(d, "overview", lambda r: d.Overview(host="h", os="CentOS")), \
            mock.patch.object(d, "services", lambda r: (_ for _ in ()).throw(RuntimeError("connection lost"))), \
            mock.patch.object(d, "updates", lambda r: ("", [])), mock.patch.object(d, "ports", lambda r: []), \
            mock.patch.object(d, "users", lambda r: ([], [], [])):
        md = collect.build_report("Web 1", "bob@web:22", ctx_with())
    assert "# Server report: Web 1" in md and "CentOS" in md and "_Not available: connection lost_" in md
    assert datetime.now().strftime("%Y-%m-%d") in md


def test_table_helper_keeps_rows_styles_and_keys_in_step():
    t = collect.Table(["A", "B"])
    t.add([1, "x"], "ok", key="k1")
    t.add(["2", 3])
    assert t.rows == [["1", "x"], ["2", "3"]] and t.styles == ["ok", ""] and t.keys == ["k1", None]
