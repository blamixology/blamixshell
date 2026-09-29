#!/usr/bin/env bash
# Builds a portable CLI/TUI folder (no Python needed on the target): dist/blamixshell-cli/
# Copy it to any Linux box of the same CPU arch and run ./blamixshell
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -x .venv-cli/bin/python ] || "$PY" -m venv .venv-cli
.venv-cli/bin/pip install --upgrade pip
.venv-cli/bin/pip install -r requirements-cli.txt pyinstaller
cat > .cli_entry.py <<'PY'
from blamixshell.cli import main
main()
PY
.venv-cli/bin/pyinstaller --noconfirm --clean --onedir --console --name blamixshell \
  --collect-data textual --exclude-module PySide6 --exclude-module tkinter \
  --distpath dist/cli-build .cli_entry.py
rm -rf dist/blamixshell-cli && mv dist/cli-build/blamixshell dist/blamixshell-cli && rm -rf dist/cli-build
rm -rf build blamixshell.spec .cli_entry.py
tar -C dist -czf "dist/blamixshell-cli-$(uname -s | tr A-Z a-z)-$(uname -m).tar.gz" blamixshell-cli
echo "Done: dist/blamixshell-cli/blamixshell"
