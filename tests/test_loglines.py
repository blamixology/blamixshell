"""Log lines: how bad a line looks, and filtering with lines of context (grep -C)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import loglines  # noqa: E402


def test_severity_by_level_first_then_words():
    sev = loglines.severity
    assert sev("2026-10-08T10:00:01 web sshd[811]: Failed password for root from 1.2.3.4") == "error"
    assert sev("kernel: Out of memory: Killed process 1234 (java)") == "error"
    assert sev("nginx: [warn] conflicting server name") == "warn"
    assert sev("app level=info msg=\"0 errors found\"") == ""            # the level wins over the words
    assert sev("app level=error msg=\"db down\"") == "error"
    assert sev("systemd[1]: Started Daily apt download activities.") == ""
    assert sev("cron: connection timed out, retrying") == "warn"
    assert sev("myerrorhandler loaded") == ""                           # whole words only


LINES = [f"line {i}" for i in range(10)]
LINES[3] = "line 3 ERROR disk"
LINES[8] = "line 8 error again"


def test_matching_with_and_without_context():
    shown, hits = loglines.matching(LINES, "error")
    assert shown == [LINES[3], LINES[8]] and hits == 2
    shown, hits = loglines.matching(LINES, "error", around=1)
    assert shown == ["line 2", LINES[3], "line 4", "--", "line 7", LINES[8], "line 9"] and hits == 2
    shown, _ = loglines.matching(LINES, "error", around=3)                  # groups that touch merge, no "--"
    assert "--" not in shown and shown[0] == "line 0" and shown[-1] == "line 9"
    assert loglines.matching(LINES, "")[0] == LINES                            # no filter: everything


def test_only_errors_or_warnings():
    lines = ["ok", "a warning here", "boom: failed", "fine"]
    assert loglines.matching(lines, "", only="error")[0] == ["boom: failed"]
    assert loglines.matching(lines, "", only="warn")[0] == ["a warning here", "boom: failed"]
    assert loglines.matching(lines, "boom", only="warn", around=1)[0] == ["a warning here", "boom: failed", "fine"]
