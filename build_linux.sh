#!/usr/bin/env bash
# Builds a portable folder dist/ShellDeck/ (ShellDeck + _internal/ + data/). Run on Linux.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pyinstaller

[ -d dist/ShellDeck/data ] && mv dist/ShellDeck/data _data_backup

.venv/bin/pyinstaller --noconfirm --clean --onedir --windowed --name ShellDeck \
  --add-data "shelldeck/assets:shelldeck/assets" \
  --exclude-module tkinter \
  run.py

.venv/bin/python packaging/prune_qt.py dist/ShellDeck
[ -d _data_backup ] && mv _data_backup dist/ShellDeck/data
cp shelldeck/assets/app.png dist/ShellDeck/shelldeck.png
cp packaging/linux/install-desktop-entry.sh dist/ShellDeck/
rm -rf build ShellDeck.spec
tar -C dist -czf dist/ShellDeck-linux-x86_64.tar.gz ShellDeck
echo
echo "Done: dist/ShellDeck/ShellDeck  (and dist/ShellDeck-linux-x86_64.tar.gz)"
echo "Add it to your app menu:  dist/ShellDeck/install-desktop-entry.sh"
