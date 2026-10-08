"""Unlock with the Windows account: the encrypted copy of the master password (real DPAPI on Windows), the unlock
window, and the command line."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import oskey  # noqa: E402


class FakeWindows:
    """oskey without Windows: the same rules (per vault, forget), kept in memory."""

    def __init__(self, vault=None, password=None):
        self.kept = {"vault": str(vault), "pw": password} if password else None

    def patches(self):
        return [mock.patch.object(oskey, "available", lambda: True),
                mock.patch.object(oskey, "remember", self.remember),
                mock.patch.object(oskey, "recall", self.recall),
                mock.patch.object(oskey, "remembered", self.remembered),
                mock.patch.object(oskey, "forget", self.forget)]

    def remember(self, pw, vault):
        self.kept = {"vault": str(vault), "pw": pw}

    def recall(self, vault):
        return self.kept["pw"] if self.kept and self.kept["vault"] == str(vault) else None

    def remembered(self, vault=None):
        return bool(self.kept) and (vault is None or self.kept["vault"] == str(vault))

    def forget(self):
        self.kept = None


def applied(fake):
    import contextlib
    stack = contextlib.ExitStack()
    for p in fake.patches():
        stack.enter_context(p)
    return stack


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_real_dpapi_round_trip_per_vault():
    with mock.patch.dict(os.environ, {"BLAMIXSHELL_HOME": tempfile.mkdtemp()}):
        oskey.remember("päss wörd 123", "C:/v/a.sdv")
        assert oskey.remembered("C:/v/a.sdv") and oskey.recall("C:/v/a.sdv") == "päss wörd 123"
        assert oskey.recall("C:/v/other.sdv") is None
        from blamixshell.paths import data_dir
        assert "päss" not in (data_dir() / oskey.FILE).read_text(encoding="utf-8")
        oskey.forget()
        assert not oskey.remembered() and oskey.recall("C:/v/a.sdv") is None


def test_not_offered_elsewhere():
    with mock.patch.object(sys, "platform", "linux"):
        assert not oskey.available() and oskey.recall("/v.sdv") is None


def test_the_unlock_window_remembers_forgets_and_unlocks_with_one_click():
    from PySide6.QtWidgets import QApplication
    from blamixshell.dialogs import UnlockDialog
    QApplication.instance() or QApplication([])
    vault = "C:/v/a.sdv"
    fake = FakeWindows()
    tried = []

    def attempt(pw):
        tried.append(pw)
        return "" if pw == "right-password" else "Wrong master password."
    with applied(fake):
        dlg = UnlockDialog(False, attempt, vault_path=vault)
        assert not dlg.remember.isHidden() and not dlg.remember.isChecked() and dlg.win_btn.isHidden()
        dlg.remember.setChecked(True)
        dlg.pw.setText("wrong")
        dlg._go()
        assert fake.kept is None                                          # a wrong password is never kept
        dlg.pw.setText("right-password")
        dlg._go()
        assert fake.recall(vault) == "right-password"

        again = UnlockDialog(False, attempt, vault_path=vault)            # after "Lock vault"
        assert not again.win_btn.isHidden() and again.remember.isChecked()
        again._go_windows()
        assert again.result() == 1 and tried[-1] == "right-password"                  # accepted

        fake.kept["pw"] = "old-password"                                  # the master password changed elsewhere
        stale = UnlockDialog(False, attempt, vault_path=vault)
        stale._go_windows()
        assert fake.kept is None and "no longer opens this vault" in stale.err.text() and stale.win_btn.isHidden()

        off = UnlockDialog(False, attempt, vault_path=vault)
        fake.remember("right-password", vault)
        off.remember.setChecked(False)                                    # untick: forgotten
        off.pw.setText("right-password")
        off._go()
        assert fake.kept is None
    with mock.patch.object(oskey, "available", lambda: False):
        plain = UnlockDialog(False, attempt, vault_path=vault)
        assert plain.remember.isHidden() and plain.win_btn.isHidden()


def test_the_command_line_uses_it_and_forgets_a_stale_one():
    from blamixshell import cli
    from blamixshell.vault import Vault
    home = tempfile.mkdtemp()
    with mock.patch.dict(os.environ, {"BLAMIXSHELL_HOME": home}):
        from blamixshell.paths import vault_path
        path = vault_path()
        Vault.create(path, "right-password", n_log2=10)
        fake = FakeWindows(path, "right-password")
        with applied(fake), mock.patch.object(cli.getpass, "getpass", lambda *a: (_ for _ in ()).throw(AssertionError)):
            store = cli.unlock()                                          # no prompt at all
        assert store.vault.path == Path(path)
        fake = FakeWindows(path, "old-password")
        with applied(fake), mock.patch.object(cli.getpass, "getpass", lambda *a: "right-password"):
            cli.unlock()                                                  # asks once, and drops the stale copy
        assert fake.kept is None
