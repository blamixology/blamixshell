#!/usr/bin/env bash
# Install the ShellDeck CLI + TUI for the current user (Linux/macOS, no GUI needed).
#   ./install_cli.sh           -> `shelldeck` command in ~/.local/bin
set -e
cd "$(dirname "$0")"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force ".[tui]"
else
  PY=${PYTHON:-python3}
  DEST="${XDG_DATA_HOME:-$HOME/.local/share}/shelldeck-cli"
  "$PY" -m venv "$DEST"
  "$DEST/bin/pip" install --upgrade pip >/dev/null
  "$DEST/bin/pip" install ".[tui]"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$DEST/bin/shelldeck" "$HOME/.local/bin/shelldeck"
fi
echo
echo "Installed. Run:  shelldeck        (TUI)"
echo "              shelldeck --help  (CLI)"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH";; esac
