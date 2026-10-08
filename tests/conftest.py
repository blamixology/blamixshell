"""One Qt application for the whole test run, created before any test and kept alive until the end.

Tests use `QApplication.instance() or QApplication([])`; without this, the first test to run would create the
application without keeping a reference to it (or create a plain QCoreApplication), and Python could delete it while
windows still exist. Newer PySide6 versions then crash when the process exits."""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:                     # the CLI-only test environments have no Qt
    QApplication = None

APP = (QApplication.instance() or QApplication([])) if QApplication else None


def pytest_sessionfinish(session, exitstatus):
    """Close what tests left open and let Qt delete it now, while the application still exists."""
    if APP is None:
        return
    for w in APP.topLevelWidgets():
        w.close()
        w.deleteLater()
    from PySide6.QtCore import QEvent
    APP.processEvents()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    APP.processEvents()
