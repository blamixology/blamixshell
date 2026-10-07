#!/usr/bin/env bash
# Builds a portable CLI/TUI folder (no Python needed on the target): dist/blamixshell-cli/
# Copy it to any Linux box of the same CPU arch and run ./blamixshell
#   ONEFILE=1 ./build_cli.sh   makes one single executable instead: dist/blamixshell-<os>-<arch>
#                              (OUTNAME=... picks the file name; CI uses it for the release files)
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
# packaging/cli.spec: what goes in (textual's widgets are loaded by name at run time) and what stays out
if [ -n "$ONEFILE" ]; then
  ONEFILE=1 .venv-cli/bin/pyinstaller --noconfirm --clean --distpath dist/cli-build packaging/cli.spec
  out="dist/${OUTNAME:-blamixshell-$(uname -s | tr A-Z a-z)-$(uname -m)}"
  mv dist/cli-build/blamixshell "$out"
  rm -rf dist/cli-build build .cli_entry.py
  echo "Done: $out (one file)"
  "$out" selftest
  exit 0
fi
.venv-cli/bin/pyinstaller --noconfirm --clean --distpath dist/cli-build packaging/cli.spec
rm -rf dist/blamixshell-cli && mv dist/cli-build/blamixshell dist/blamixshell-cli && rm -rf dist/cli-build
rm -rf build .cli_entry.py
tar -C dist -czf "dist/blamixshell-cli-$(uname -s | tr A-Z a-z)-$(uname -m).tar.gz" blamixshell-cli
echo "Done: dist/blamixshell-cli/blamixshell"
