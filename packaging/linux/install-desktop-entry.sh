#!/usr/bin/env bash
# Adds ShellDeck to the Ubuntu/GNOME/KDE application menu (current user only).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/shelldeck.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ShellDeck
Comment=SSH client with server manager, split panes and SFTP
Exec="$DIR/ShellDeck" %U
Icon=$DIR/shelldeck.png
Terminal=false
Categories=Network;RemoteAccess;System;
StartupWMClass=shelldeck
Keywords=ssh;sftp;terminal;server;
DESKTOP
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: $APPS/shelldeck.desktop"
