"""Desktop dashboard, Logs tab: errors only, lines around a match, jump to the next error, saved views keep it."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard_fw_details import make  # noqa: E402

LOG = "\n".join(["10:00 cron: started", "10:01 app: connecting", "10:02 app: ERROR db refused",
                 "10:03 app: retrying in 5s", "10:04 app: ok", "10:05 nginx: [warn] slow upstream",
                 "10:06 app: done"])


def shown(w):
    return w.log_view.toPlainText().splitlines()


def test_errors_only_context_and_next_error():
    w, _ = make()
    w._show_logs(LOG)
    assert len(shown(w)) == 7 and w.log_count.text() == ""
    w.log_only.setCurrentIndex(w.log_only.findData("error"))
    assert shown(w) == ["10:02 app: ERROR db refused"] and w.log_count.text() == "1 of 7 lines"
    w.log_only.setCurrentIndex(w.log_only.findData("warn"))
    assert shown(w) == ["10:02 app: ERROR db refused", "10:03 app: retrying in 5s", "10:05 nginx: [warn] slow upstream"]
    w.log_only.setCurrentIndex(0)
    w.log_find.setText("refused")
    w.log_around.setCurrentIndex(w.log_around.findData(2))
    assert shown(w) == ["10:00 cron: started", "10:01 app: connecting", "10:02 app: ERROR db refused",
                        "10:03 app: retrying in 5s", "10:04 app: ok"]
    w.log_find.setText("")
    w._next_log_error()
    assert w.log_view.textCursor().blockNumber() == 2 and "error at line 3" in w.log_count.text()


def test_a_saved_view_remembers_the_new_choices():
    w, _ = make()
    w.settings = {}
    w.log_only.setCurrentIndex(w.log_only.findData("warn"))
    w.log_around.setCurrentIndex(w.log_around.findData(5))
    from unittest import mock
    with mock.patch("blamixshell.dashboard_ui.QInputDialog.getText", lambda *a, **k: ("mine", True)):
        w._save_log_view()
    w.log_only.setCurrentIndex(0)
    w.log_around.setCurrentIndex(0)
    w.log_views.setCurrentIndex(w.log_views.findText("mine"))
    w._apply_log_view(0)
    assert w.log_only.currentData() == "warn" and w.log_around.currentData() == 5
