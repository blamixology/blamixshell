"""`blamixshell show` / `report`: dashboard tabs for scripts (JSON, filters, sorting, a health-check exit code)."""
import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import cli, dashboard as d  # noqa: E402
from test_collect import FakeRunner  # noqa: E402

SERVER = SimpleNamespace(label="web-1", address="deploy@web-1:22", username="deploy")
SVCS = [d.Service("sshd.service", "loaded", "active", "running", "OpenSSH", "enabled", "systemd"),
        d.Service("backup.service", "loaded", "failed", "failed", "Nightly backup", "enabled", "systemd")]
PROCS = [d.Process(10, "root", 0.5, 1.0, 900, "01:00", "sshd"), d.Process(2, "deploy", 55.0, 12.0, 1200000, "00:30", "node"),
         d.Process(100, "root", 3.0, 0.1, 12800, "10:00", "nginx")]


def run_cli(*argv, extra=()):
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(1))
    parser = cli.build_parser()
    a = parser.parse_args(list(argv))
    out = io.StringIO()
    patches = [mock.patch.object(cli, "_open_server", lambda store, target, accept_new=False: (SERVER, client, [])),
               mock.patch.object(d, "Runner", lambda cl: FakeRunner()),
               mock.patch.object(d, "services", lambda r: (SVCS, "")),
               mock.patch.object(d, "processes", lambda r, sort, limit=0: PROCS), *extra]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out):
            code = cli.HANDLERS[a.cmd](None, a)
    assert closed == [1]                                          # the connection is always closed
    return code, out.getvalue()


def test_show_json_with_status_and_check_exit_code():
    code, out = run_cli("show", "web-1", "services", "--json", "--check")
    data = json.loads(out)
    assert data["server"] == "web-1" and data["tab"] == "services" and data["columns"][0] == "Service"
    assert [r["Service"] for r in data["rows"]] == ["backup.service", "sshd.service"]
    assert data["rows"][0]["status"] == "bad" and "status" not in data["rows"][1]
    assert code == 1                                                # a failed service: a failing health check
    code, _ = run_cli("show", "web-1", "services", "--filter", "sshd", "--check")
    assert code == 0                                                # filtered to the healthy row only
    code, _ = run_cli("show", "web-1", "serv")
    assert code == 0                                                # without --check it is just output


def test_show_filters_and_sorts_by_value():
    code, out = run_cli("show", "web-1", "proc", "--sort", "cpu", "--desc", "--json")
    pids = [r["PID"] for r in json.loads(out)["rows"]]
    assert pids == ["2", "100", "10"]                                # 55.0, 3.0, 0.5 (as numbers)
    _, out = run_cli("show", "web-1", "proc", "--sort", "pid", "--json")
    assert [r["PID"] for r in json.loads(out)["rows"]] == ["2", "10", "100"]    # numeric, not 10 < 2
    _, out = run_cli("show", "web-1", "processes", "-f", "root", "--json")
    assert [r["PID"] for r in json.loads(out)["rows"]] == ["10", "100"]


def test_show_prints_a_readable_table():
    _, out = run_cli("show", "web-1", "services")
    lines = out.splitlines()
    assert "web-1" in lines[0] and "services" in lines[0] and lines[1].split()[:2] == ["Service", "State"]
    assert "backup.service" in lines[2] and "sshd.service" in lines[3]


def test_unknown_tab_or_column_exits_with_a_hint():
    for argv in (("show", "web-1", "nonsense"), ("show", "web-1", "proc", "--sort", "nope")):
        try:
            run_cli(*argv)
            raise AssertionError(argv)
        except SystemExit as e:
            assert e.code not in (0, None)


def test_report_goes_to_the_screen_or_a_file(tmp_path=None):
    import tempfile
    from blamixshell import collect
    extra = [mock.patch.object(d, "overview", lambda r: d.Overview(host="h", os="CentOS")),
             mock.patch.object(d, "updates", lambda r: ("", [])), mock.patch.object(d, "ports", lambda r: []),
             mock.patch.object(d, "users", lambda r: ([], [], []))]
    code, out = run_cli("report", "web-1", extra=extra)
    assert code == 0 and out.startswith("# Server report: web-1") and "backup.service" in out
    target = Path(tempfile.mkdtemp()) / "r.md"
    code, out = run_cli("report", "web-1", "-o", str(target), extra=extra)
    assert "Report saved" in out and target.read_text(encoding="utf-8").startswith("# Server report")
    assert collect is not None


def test_report_as_json_for_scripts():
    disks = [d.Disk("/", "ext4", 100, 93, 7), d.Disk("/data", "xfs", 100, 20, 80)]
    extra = [mock.patch.object(d, "overview", lambda r: d.Overview(host="h", os="CentOS", disks=disks)),
             mock.patch.object(d, "updates", lambda r: ("yum", [d.Update("openssl.x86_64", "1.0.2k")])),
             mock.patch.object(d, "ports", lambda r: [d.Port("tcp", "*", "22", "sshd")]),
             mock.patch.object(d, "users", lambda r: ([d.Account("bob", 1000, "/home/bob", "/bin/bash")], ["bob pts/0"], []))]
    code, out = run_cli("report", "web-1", "--json", extra=extra)
    data = json.loads(out)
    assert code == 0 and data["server"] == "web-1" and data["overview"]["os"] == "CentOS"
    assert data["services"]["failed"] == ["backup.service"] and data["updates"]["manager"] == "yum"
    assert data["users"]["accounts"][0]["name"] == "bob" and data["ports"][0]["port"] == "22"
    s = data["summary"]
    assert s["failed_services"] == 1 and s["pending_updates"] == 1 and s["fullest_disk_percent"] == 93
    assert s["listening_ports"] == 1 and data["overview"]["disks"][0]["percent"] == 93.0
    assert data["cron"] == [] or data["cron"] is None or isinstance(data["cron"], list)
