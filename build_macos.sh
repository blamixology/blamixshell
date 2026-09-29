#!/usr/bin/env bash
# Builds dist/BlamixShell.app (+ a zipped copy for sharing). Run on a Mac.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pyinstaller pillow

# keep existing portable data if rebuilding
[ -d dist/data ] && mv dist/data _data_backup

.venv/bin/pyinstaller --noconfirm --clean --windowed --name BlamixShell \
  --icon blamixshell/assets/app.png \
  --osx-bundle-identifier ro.blamixology.blamixshell \
  --add-data "blamixshell/assets:blamixshell/assets" \
  --exclude-module tkinter \
  run.py

.venv/bin/python packaging/prune_qt.py dist/BlamixShell.app
[ -d _data_backup ] && mv _data_backup dist/data
rm -rf build BlamixShell.spec dist/BlamixShell   # the .app is what you want
(cd dist && ditto -c -k --keepParent BlamixShell.app BlamixShell-macOS.zip)
echo
echo "Done: dist/BlamixShell.app  (and dist/BlamixShell-macOS.zip)"
echo "Unsigned app: first launch via right-click → Open (or: xattr -dr com.apple.quarantine dist/BlamixShell.app)"
