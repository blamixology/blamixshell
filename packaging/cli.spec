# PyInstaller spec for the command-line / TUI / terminal-dashboard build (used by build_cli.sh).
#   ONEFILE=1  -> one single executable      (default: a folder)
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.getcwd()   # build_cli.sh runs from the project folder (a spec file otherwise resolves paths from its own)
onefile = bool(os.environ.get("ONEFILE"))
name = "blamixshell"

# textual loads its widgets by name at run time, so everything of it must be in the build (and rich's unicode tables)
datas, binaries, hidden = collect_all("textual")
hidden += collect_submodules("rich")

a = Analysis(
    [os.path.join(ROOT, ".cli_entry.py")], pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
    # the desktop UI is not part of this build; curses, readline and crypt are unused and would pull in libraries
    # from the build machine
    excludes=["PySide6", "tkinter", "_curses", "curses", "readline", "_crypt"],
)

# libgcc_s from a recent build machine can need a newer glibc than an old server has (glibc systems all ship their
# own libgcc_s). On musl (Alpine) the bundled copy is kept: a minimal Alpine doesn't have one.
if sys.platform.startswith("linux") and not any(os.path.exists(p) for p in ("/lib/ld-musl-x86_64.so.1",
                                                                          "/lib/ld-musl-aarch64.so.1")):
    a.binaries = [b for b in a.binaries if not b[0].startswith("libgcc_s.so")]

pyz = PYZ(a.pure)
if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=name, console=True, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=True, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name=name, upx=False)
