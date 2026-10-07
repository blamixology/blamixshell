"""`blamixshell update --install`: finding the right file, checking it, and swapping it in safely."""
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import cli, updater  # noqa: E402

ELF = b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 64


def release(*names, sha="a" * 64):
    return updater.Release("9.9.9", "v9.9.9", "notes", "https://example.invalid/r",
                           [updater.Asset(n, f"https://example.invalid/{n}", 100, sha) for n in names])


def test_the_file_name_follows_the_system():
    with mock.patch.object(sys, "platform", "linux"), mock.patch.object(updater, "_is_musl", lambda: False):
        for machine, expect in (("x86_64", "blamixshell-linux-x86_64"), ("AMD64", "blamixshell-linux-x86_64"),
                                ("aarch64", "blamixshell-linux-aarch64"), ("arm64", "blamixshell-linux-aarch64")):
            with mock.patch("platform.machine", lambda m=machine: m):
                assert updater.cli_asset_name() == expect
    with mock.patch.object(sys, "platform", "linux"), mock.patch.object(updater, "_is_musl", lambda: True), \
            mock.patch("platform.machine", lambda: "x86_64"):
        assert updater.cli_asset_name() == "blamixshell-linux-musl-x86_64"
    with mock.patch.object(sys, "platform", "darwin"), mock.patch("platform.machine", lambda: "arm64"):
        assert updater.cli_asset_name() == "blamixshell-macos-arm64"
    with mock.patch.object(sys, "platform", "win32"):
        assert updater.cli_asset_name() == "" and updater.pick_cli_asset(release("anything")) is None


def test_the_right_asset_is_picked_from_a_release():
    with mock.patch.object(sys, "platform", "linux"), mock.patch.object(updater, "_is_musl", lambda: False), \
            mock.patch("platform.machine", lambda: "x86_64"):
        rel = release("BlamixShell-1.0.0-x64.msi", "blamixshell-linux-musl-x86_64", "blamixshell-linux-x86_64")
        assert updater.pick_cli_asset(rel).name == "blamixshell-linux-x86_64"
        assert updater.pick_cli_asset(release("blamixshell-linux-musl-x86_64")) is None


def test_only_the_one_file_binary_counts_as_one():
    assert not updater.is_onefile_binary()                                    # running from source here
    exe = str(Path(tempfile.mkdtemp()) / "blamixshell")
    with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
        with mock.patch.object(sys, "_MEIPASS", str(Path(tempfile.gettempdir()) / "_MEI12345"), create=True):
            assert updater.is_onefile_binary()                               # unpacks to a temp folder
        with mock.patch.object(sys, "_MEIPASS", str(Path(exe).parent / "_internal"), create=True):
            assert not updater.is_onefile_binary()                           # the folder build keeps it beside the exe


def test_a_good_download_replaces_the_program_and_leaves_nothing_behind():
    folder = Path(tempfile.mkdtemp())
    target = folder / "blamixshell"
    target.write_bytes(b"old program")

    def fake_download(asset, dest_dir, progress=lambda d, t: None):
        path = Path(dest_dir) / asset.name
        path.write_bytes(ELF + b"new")
        progress(100, 100)
        return path
    asset = release("blamixshell-linux-x86_64").assets[0]
    seen = []
    with mock.patch.object(sys, "platform", "linux"), mock.patch.object(updater, "download", fake_download):
        assert updater.install_cli_binary(asset, target, lambda d, t: seen.append((d, t))) == target.resolve()
    assert target.read_bytes() == ELF + b"new" and seen == [(100, 100)]
    assert [p.name for p in folder.iterdir()] == ["blamixshell"]               # the staging folder is gone
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o111                                   # executable


def test_a_bad_download_changes_nothing():
    folder = Path(tempfile.mkdtemp())
    target = folder / "blamixshell"
    target.write_bytes(b"old program")
    asset = release("blamixshell-linux-x86_64").assets[0]

    def html_page(asset, dest_dir, progress=lambda d, t: None):
        path = Path(dest_dir) / asset.name
        path.write_bytes(b"<html>rate limited</html>")
        return path

    def failed(asset, dest_dir, progress=lambda d, t: None):
        raise updater.UpdateError("Downloaded file failed its checksum check; the update was not installed.")
    with mock.patch.object(sys, "platform", "linux"):
        for fake, message in ((html_page, "isn't a program"), (failed, "checksum")):
            with mock.patch.object(updater, "download", fake):
                try:
                    updater.install_cli_binary(asset, target)
                    raise AssertionError("must fail")
                except updater.UpdateError as e:
                    assert message in str(e)
            assert target.read_bytes() == b"old program" and [p.name for p in folder.iterdir()] == ["blamixshell"]
        try:
            updater.install_cli_binary(release("blamixshell-linux-x86_64", sha="").assets[0], target)
            raise AssertionError("no checksum: must not install")
        except updater.UpdateError as e:
            assert "checksum" in str(e)
    assert updater.check_binary_file(folder / "missing") != ""


def run_update(*argv, rel=None, onefile=False, installed=None, answer=True):
    a = cli.build_parser().parse_args(["update", *argv])
    out = io.StringIO()
    err = io.StringIO()
    patches = [mock.patch.object(updater, "check", lambda: rel), mock.patch.object(updater, "is_onefile_binary", lambda: onefile),
               mock.patch.object(cli, "confirm", lambda q: answer)]
    if installed is not None:
        patches.append(mock.patch.object(updater, "install_cli_binary", installed))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.cmd_update(a)
            except SystemExit as e:
                code = e.code
    return code, out.getvalue() + err.getvalue()


def test_update_command_explains_and_installs():
    rel = release("blamixshell-linux-x86_64")
    with mock.patch.object(sys, "platform", "linux"), mock.patch.object(updater, "_is_musl", lambda: False), \
            mock.patch("platform.machine", lambda: "x86_64"):
        code, out = run_update(rel=None)
        assert code == 0 and "latest version" in out
        code, out = run_update(rel=rel, onefile=True)
        assert code == 0 and "blamixshell update --install" in out
        code, out = run_update("--install", rel=rel, onefile=False)                  # a copy that can't update itself
        assert code not in (0, None) and "one-file" in out
        code, out = run_update("--install", rel=release("BlamixShell.msi"), onefile=True)
        assert code not in (0, None) and "no file named blamixshell-linux-x86_64" in out
        called = []
        install = lambda asset, **k: called.append(asset.name) or Path("/usr/local/bin/blamixshell")   # noqa: E731
        code, out = run_update("--install", rel=rel, onefile=True, installed=install, answer=False)
        assert code == 1 and called == []                                            # declined: nothing happens
        code, out = run_update("--install", "-y", rel=rel, onefile=True, installed=install)
        assert code == 0 and called == ["blamixshell-linux-x86_64"] and "Updated to 9.9.9" in out

        def broken(asset, **k):
            raise updater.UpdateError("Can't write next to /usr/bin/blamixshell")
        code, out = run_update("--install", "-y", rel=rel, onefile=True, installed=broken)
        assert code not in (0, None) and "Can't write next to" in out
