"""Dashboard tables: click-to-sort by value (numbers, sizes, durations), not as text."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blamixshell import dashboard_ui as ui  # noqa: E402


def test_cells_sort_by_value():
    k = ui._auto_key
    assert k("2") < k("10") < k("100")                       # numbers, not "10" < "2"
    assert k("0.5") < k("12.5") and k("3%") < k("90%") and k("1,024") > k("999")
    assert k("900 KB") < k("12.5 MB") < k("1.1 GB") < k("2 TB")
    assert k("00:30") < k("10:00") < k("1-00:00:00") < k("2-03:04:05")
    assert k("42") < k("abc") and k("abc") < k("abd") and k("Zed") > k("alpha")      # numbers first, then text
    assert k("–")[0] == 1


def test_table_sorts_on_header_click_and_keeps_the_choice_while_refilling():
    app = QApplication.instance() or QApplication([])
    t = ui._table(["PID", "Memory", "Name"], sortable=True)

    def fill(rows):
        with ui._Filling(t):
            t.setRowCount(len(rows))
            for i, (pid, mem, name) in enumerate(rows):
                t.setItem(i, 0, ui._item(pid, align_right=True))
                t.setItem(i, 1, ui._item(mem, align_right=True))
                t.setItem(i, 2, ui._item(name))
    rows = [(10, "900 KB", "b"), (2, "1.1 GB", "a"), (100, "12.5 MB", "c")]
    fill(rows)
    col = lambda c: [t.item(i, c).text() for i in range(t.rowCount())]          # noqa: E731
    assert col(0) == ["10", "2", "100"]                                         # no column chosen: as it arrived
    t.sortByColumn(0, Qt.AscendingOrder)
    assert col(0) == ["2", "10", "100"]
    t.sortByColumn(1, Qt.DescendingOrder)
    assert col(1) == ["1.1 GB", "12.5 MB", "900 KB"]
    fill(rows + [(7, "55 MB", "d")])                                            # a refresh keeps the sort
    assert t.horizontalHeader().sortIndicatorSection() == 1
    assert col(1) == ["1.1 GB", "55 MB", "12.5 MB", "900 KB"] and col(2) == ["a", "d", "c", "b"]
    assert app is not None
