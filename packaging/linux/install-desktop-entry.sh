#!/usr/bin/env bash
# Adds BlamixShell to the Ubuntu/GNOME/KDE application menu (current user only).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/blamixshell.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=BlamixShell
Comment=SSH client with server manager, split panes and SFTP
Exec="$DIR/BlamixShell" %U
Icon=$DIR/blamixshell.png
Terminal=false
Categories=Network;RemoteAccess;System;
StartupWMClass=blamixshell
Keywords=ssh;sftp;terminal;server;
DESKTOP
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: $APPS/blamixshell.desktop"
