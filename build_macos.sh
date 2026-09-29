#!/usr/bin/env bash
# Builds dist/ShellDeck.app (+ a zipped copy for sharing). Run on a Mac.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pyinstaller pillow

# keep existing portable data if rebuilding
[ -d dist/data ] && mv dist/data _data_backup

.venv/bin/pyinstaller --noconfirm --clean --windowed --name ShellDeck \
  --icon shelldeck/assets/app.png \
  --osx-bundle-identifier ro.blamixology.shelldeck \
  --add-data "shelldeck/assets:shelldeck/assets" \
  --exclude-module tkinter \
  run.py

.venv/bin/python packaging/prune_qt.py dist/ShellDeck.app
[ -d _data_backup ] && mv _data_backup dist/data
rm -rf build ShellDeck.spec dist/ShellDeck   # the .app is what you want
(cd dist && ditto -c -k --keepParent ShellDeck.app ShellDeck-macOS.zip)
echo
echo "Done: dist/ShellDeck.app  (and dist/ShellDeck-macOS.zip)"
echo "Unsigned app: first launch via right-click → Open (or: xattr -dr com.apple.quarantine dist/ShellDeck.app)"
