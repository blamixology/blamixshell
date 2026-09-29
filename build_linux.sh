#!/usr/bin/env bash
# Builds a portable folder dist/BlamixShell/ (BlamixShell + _internal/ + data/). Run on Linux.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pyinstaller

[ -d dist/BlamixShell/data ] && mv dist/BlamixShell/data _data_backup

.venv/bin/pyinstaller --noconfirm --clean --onedir --windowed --name BlamixShell \
  --add-data "blamixshell/assets:blamixshell/assets" \
  --exclude-module tkinter \
  run.py

.venv/bin/python packaging/prune_qt.py dist/BlamixShell
[ -d _data_backup ] && mv _data_backup dist/BlamixShell/data
cp blamixshell/assets/app.png dist/BlamixShell/blamixshell.png
cp packaging/linux/install-desktop-entry.sh dist/BlamixShell/
rm -rf build BlamixShell.spec
tar -C dist -czf dist/BlamixShell-linux-x86_64.tar.gz BlamixShell
echo
echo "Done: dist/BlamixShell/BlamixShell  (and dist/BlamixShell-linux-x86_64.tar.gz)"
echo "Add it to your app menu:  dist/BlamixShell/install-desktop-entry.sh"
