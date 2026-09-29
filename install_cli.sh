#!/usr/bin/env bash
# Install the BlamixShell CLI + TUI for the current user (Linux/macOS, no GUI needed).
#   ./install_cli.sh           -> `blamixshell` command in ~/.local/bin
set -e
cd "$(dirname "$0")"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force ".[tui]"
else
  PY=${PYTHON:-python3}
  DEST="${XDG_DATA_HOME:-$HOME/.local/share}/blamixshell-cli"
  "$PY" -m venv "$DEST"
  "$DEST/bin/pip" install --upgrade pip >/dev/null
  "$DEST/bin/pip" install ".[tui]"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$DEST/bin/blamixshell" "$HOME/.local/bin/blamixshell"
fi
echo
echo "Installed. Run:  blamixshell        (TUI)"
echo "              blamixshell --help  (CLI)"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH";; esac
