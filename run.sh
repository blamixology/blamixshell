#!/usr/bin/env bash
# Run the BlamixShell desktop app from source on macOS or Linux.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
