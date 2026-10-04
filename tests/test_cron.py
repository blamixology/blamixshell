"""Crontab parsing, form <-> expression, descriptions and next-run previews."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import cron  # noqa: E402

TAB = """# backups
MAILTO=ops@example.com
*/5 * * * * /usr/local/bin/check.sh >/dev/null 2>&1
#off# 30 2 * * 1-5 /opt/backup.sh --full
@reboot /srv/start.sh
0 3 1 * * echo "monthly"

not a job at all
"""


def test_parse_keeps_everything_and_finds_jobs():
    entries = cron.parse_crontab(TAB)
    assert [e.kind for e in entries].count("job") == 4
    jobs = [e for e in entries if e.kind == "job"]
    assert jobs[0].schedule == "*/5 * * * *" and jobs[0].command.startswith("/usr/local/bin/check.sh")
    assert jobs[1].enabled is False and jobs[1].schedule == "30 2 * * 1-5"
    assert jobs[2].schedule == "@reboot"
    assert next(e for e in entries if e.kind == "env").raw.startswith("MAILTO")
    assert cron.build_crontab(entries) == TAB            # nothing lost on a round trip


def test_system_crontab_has_user_and_source():
    text = "# ---- /etc/cron.d/foo\n17 * * * * root run-parts /etc/cron.hourly\n@daily www-data /bin/job\n"
    jobs = cron.parse_crontab(text, system=True)
    assert [(j.user, j.source) for j in jobs] == [("root", "/etc/cron.d/foo"), ("www-data", "/etc/cron.d/foo")]
    assert jobs[0].command == "run-parts /etc/cron.hourly"


def test_validation():
    for good in ("* * * * *", "*/15 9-17 * * mon-fri", "0 0 1,15 jan,jul *", "@daily", "5/10 * * * *"):
        assert cron.validate_schedule(good) == "", good
    for bad in ("", "* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *", "@sometimes", "5-1 * * * *", "a * * * *"):
        assert cron.validate_schedule(bad), bad


def test_form_round_trip():
    cases = [("minutes", {"every": 5}), ("minutes", {"every": 1}), ("hourly", {"minute": 15}),
             ("daily", {"minute": 30, "hour": 2}), ("weekly", {"minute": 0, "hour": 6, "weekday": 1}),
             ("monthly", {"minute": 5, "hour": 4, "day": 28}), ("reboot", {})]
    for kind, params in cases:
        expr = cron.build_schedule(kind, **params)
        assert cron.classify(expr) == (kind, params), expr
    assert cron.classify("*/5 9-17 * * 1-5")[0] == "custom"
    assert cron.classify("0 0 * * 7") == ("weekly", {"minute": 0, "hour": 0, "weekday": 0})


def test_describe():
    assert cron.describe("* * * * *") == "Every minute"
    assert cron.describe("*/10 * * * *") == "Every 10 minutes"
    assert cron.describe("30 2 * * *") == "Every day at 02:30"
    assert cron.describe("0 6 * * 1") == "Every Monday at 06:00"
    assert cron.describe("0 9 * * 1-5") == "Every weekday at 09:00"
    assert cron.describe("0 9 * * 6,0") == "Every weekend day at 09:00"
    assert cron.describe("0 */4 * * *") == "Every 4 hours at :00"
    assert cron.describe("@reboot") == "At server startup"
    assert cron.describe("nonsense") == "Invalid schedule"


def test_next_runs():
    now = datetime(2026, 10, 2, 19, 27)               # a Friday
    assert cron.next_runs("*/15 * * * *", now, 2) == [datetime(2026, 10, 2, 19, 30), datetime(2026, 10, 2, 19, 45)]
    assert cron.next_runs("0 3 * * *", now, 1) == [datetime(2026, 10, 3, 3, 0)]
    assert cron.next_runs("0 9 * * 1", now, 1) == [datetime(2026, 10, 5, 9, 0)]          # next Monday
    assert cron.next_runs("0 0 1 * *", now, 2) == [datetime(2026, 11, 1), datetime(2026, 12, 1)]
    assert cron.next_runs("0 0 29 2 *", now, 1) == [datetime(2028, 2, 29)]               # leap day
    assert cron.next_runs("@reboot", now) == [] and cron.next_runs("bad", now) == []
    # day-of-month OR day-of-week when both are given (the classic cron rule)
    runs = cron.next_runs("0 0 13 * 5", datetime(2026, 10, 1), 3)
    assert runs == [datetime(2026, 10, 2), datetime(2026, 10, 9), datetime(2026, 10, 13)]


def test_read_and_save_helpers():
    out = "@@cron\nno crontab for bob\n@@date\n2026-10-02 19:27\n@@tz\nUTC\n"
    text, now, tz = cron.parse_read(out)
    assert text == "" and now == datetime(2026, 10, 2, 19, 27) and tz == "UTC"
    assert "-u root" in cron.read_script("root") and "/etc/cron.d" in cron.read_script("@system")
    cmd = cron.save_command("", "* * * * * echo 'hi'\n")
    assert cmd.startswith("sh -c ") and "base64 -d | crontab -" in cmd and "hi" not in cmd   # sent encoded
    assert "crontab -u alice -" in cron.save_command("alice", "x\n")


def test_command_parts_round_trip():
    P = cron.Parts
    cases = [P("/usr/local/bin/backup.sh"),
             P("/usr/local/bin/backup.sh --full", out=cron.OUT_DISCARD),
             P("/srv/job.sh", out=cron.OUT_LOG, log="$HOME/cron-job.log"),
             P("./run.sh", folder="/srv/my app"),
             P("./run.sh", folder="~/app", login=True, out=cron.OUT_LOG, log="/var/log/x.log"),
             P("echo 'done' && date +%F")]
    for p in cases:
        line = cron.compose(p)
        assert cron.decompose(line) == p, line
    assert cron.compose(P("date +%F")) == "date +\\%F"                 # % would be a newline in cron
    assert cron.decompose("date +\\%F").command == "date +%F"
    odd = "cd /x; echo hi  |  tee /tmp/o"                                # unusual lines stay as they are
    assert cron.decompose(odd).command == odd and cron.decompose(odd).folder == ""


def test_script_path_and_check():
    assert cron.script_path("/usr/local/bin/backup.sh --full") == ("/usr/local/bin/backup.sh", True)
    assert cron.script_path("bash /srv/job.sh") == ("/srv/job.sh", False)
    assert cron.script_path("FOO=1 python3 -u ~/jobs/run.py") == ("~/jobs/run.py", False)
    assert cron.script_path("./relative.sh") is None and cron.script_path("echo hi") is None
    assert cron.script_path("'unbalanced") is None
    assert "p=~/jobs/run.py;" in cron.check_script("~/jobs/run.py", False)
    assert "true" in cron.check_script("/x.sh", True) and "false" in cron.check_script("/x.sh", False)


def test_listing_parser():
    cwd, dirs, files = cron.parse_listing("/srv/app\nbin/\n.config/\nrun.sh\nREADME\n")
    assert (cwd, dirs, files) == ("/srv/app", [".config", "bin"], ["README", "run.sh"])
    try:
        cron.parse_listing("sh: line 0: cd: /nope: No such file or directory\n")
        raise AssertionError("should fail")
    except ValueError as e:
        assert "No such file" in str(e)
    assert cron.list_dir_command("").startswith("cd && pwd") and "~/'my dir'" in cron.list_dir_command("~/my dir")


def test_system_view_lists_periodic_scripts_and_counts():
    text = ("# ---- /etc/cron.d/0hourly\n01 * * * * root run-parts /etc/cron.hourly\n"
            "# ---- /etc/cron.daily\n@daily root /etc/cron.daily/logrotate\n")
    jobs = cron.parse_crontab(text, system=True)
    assert [(j.schedule, j.user, j.source) for j in jobs] == [
        ("01 * * * *", "root", "/etc/cron.d/0hourly"), ("@daily", "root", "/etc/cron.daily")]
    assert jobs[1].command == "/etc/cron.daily/logrotate"
    assert "/etc/cron.$p" in cron.read_script("@system") and "@@sys" not in cron.read_script("@system")
    assert "@@sys" in cron.read_script("")
    assert cron.parse_sys("@@sys\n3 12 \n") == (3, 12) and cron.parse_sys("") == (0, 0)


def test_read_falls_back_to_the_spool_file_and_reports_how():
    script = cron.read_script("")
    assert "crontab -l" in script and "/var/spool/cron/" in script and "src=spool" in script
    assert "crontab -u alice -l" in cron.read_script("alice") and "U=alice" in cron.read_script("alice")
    out = "@@cron\n@reboot /x.sh\n@@rc\n0 spool /usr/bin/crontab\n@@me\nbob\n@@home\n/home/bob\n"
    said, me, home, how = cron.parse_diag(out)
    assert (me, home, how) == ("bob", "/home/bob", "exit 0, via spool, /usr/bin/crontab")
    assert cron.parse_read(out)[0] == "@reboot /x.sh"


def test_backups_keep_the_newest_and_skip_duplicates(tmp_path):
    from datetime import timedelta
    t0 = datetime(2026, 10, 2, 18, 0, 0)
    assert cron.save_backup("srv1", "", "  \n", tmp_path) is None                     # nothing to keep
    first = cron.save_backup("srv1", "", "@reboot /a.sh\n", tmp_path, t0)
    assert first and first.read_text() == "@reboot /a.sh\n"
    assert cron.save_backup("srv1", "", "@reboot /a.sh\n", tmp_path, t0 + timedelta(seconds=5)) is None
    for i in range(cron.BACKUP_KEEP + 3):
        cron.save_backup("srv1", "", f"* * * * * /job{i}\n", tmp_path, t0 + timedelta(minutes=i + 1))
    found = cron.list_backups("srv1", "", tmp_path)
    assert len(found) == cron.BACKUP_KEEP and found[0].read_text().endswith(f"/job{cron.BACKUP_KEEP + 2}\n")
    assert cron.list_backups("srv1", "root", tmp_path) == [] and cron.list_backups("other", "", tmp_path) == []
    assert "1 job" in cron.backup_label(found[0]) and "2026-10-02" in cron.backup_label(found[0])


def test_run_now_command_unescapes_percent():
    assert cron.run_now_command("date +\\%F") == "sh -c 'date +%F' 2>&1"
    assert cron.run_now_command("echo 'a b'").startswith("sh -c ") and cron.run_now_command("x").endswith("2>&1")
