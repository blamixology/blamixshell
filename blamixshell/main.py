"""Entry point: unlock (or create) the vault, then show the main window."""
from __future__ import annotations

import os
import sys


def main() -> None:
    # Chromium flags must be set before QtWebEngine starts
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-logging --log-level=3")
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlamixShell.SSH.1")
        except Exception:
            pass

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QIcon
    import PySide6.QtWebEngineWidgets  # noqa: F401  (must load before QApplication)
    from PySide6.QtWidgets import QApplication, QDialog

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("BlamixShell")
    app.setOrganizationName("BlamixShell")

    from .app import MainWindow
    from .dialogs import UnlockDialog
    from .models import Store
    from .paths import assets_dir, vault_path
    from .settings import Settings
    from .theme import apply_palette
    from .vault import Vault, VaultError, WrongPassword

    apply_palette(app)
    ico = assets_dir() / ("app.ico" if sys.platform == "win32" else "app.png")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))
    app.setDesktopFileName("blamixshell")   # matches the Linux .desktop entry

    path = vault_path()
    create = not Vault.exists(path)
    holder: dict = {}

    def attempt(pw: str) -> str:
        try:
            if create:
                holder["store"] = Store(Vault.create(path, pw), {})
            else:
                v, data = Vault.open(path, pw)
                holder["store"] = Store(v, data)
            return ""
        except WrongPassword:
            return "Wrong master password."
        except VaultError as e:
            return str(e)
        except Exception as e:  # corrupted file etc.
            return f"Could not open the vault: {e}"

    try:  # close the PyInstaller one-file splash screen, if any
        import pyi_splash  # type: ignore
        pyi_splash.close()
    except Exception:
        pass

    if os.environ.get("BLAMIXSHELL_SELFTEST"):
        sys.exit(_selftest(app))

    dlg = UnlockDialog(create, attempt)
    if dlg.exec() != QDialog.Accepted:
        sys.exit(0)

    win = MainWindow(holder["store"], Settings())
    win.show()
    win.restore_session()
    sys.exit(app.exec())


def _selftest(app) -> int:
    """Packaging check (CI): open the window + one terminal and verify xterm.js boots.
    Uses a throwaway in-memory vault; never touches your data."""
    import tempfile
    import time
    from pathlib import Path

    from .app import MainWindow
    from .models import Server, Store
    from .settings import Settings
    from .vault import Vault

    tmp = Path(tempfile.mkdtemp(prefix="blamixshell-selftest-"))
    store = Store(Vault.create(tmp / "v.sdv", "selftest", n_log2=10), {})
    store.upsert(Server(name="selftest", host="127.0.0.1", port=1, username="x", password="x"))
    win = MainWindow(store, Settings())
    win.show()
    win.connect_server(next(iter(store.servers)))
    pane = win.active_pane()
    end = time.time() + 30
    while time.time() < end and not pane._ready:
        app.processEvents()
        time.sleep(0.05)
    ok = pane._ready
    print("SELFTEST", "OK: terminal engine loaded" if ok else "FAILED: terminal did not load")
    win._force_quit = True
    for p in win.all_panes():
        p.shutdown()
    return 0 if ok else 3


if __name__ == "__main__":
    main()
