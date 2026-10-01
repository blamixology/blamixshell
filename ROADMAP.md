# Roadmap

What's planned for BlamixShell, roughly in order. Ideas and votes are welcome in
[issues](https://github.com/blamixology/blamixshell/issues).

Legend: ✅ done · 🚧 in progress · 🗓 planned · 💡 later / maybe

## Next release (in `main`, not released yet)

| | Feature | What it means |
|---|---|---|
| ✅ | **Server dashboard** | Agentless, over the existing SSH connection: overview (CPU, memory, load, disks), services with confirmed actions (systemd, SysV init, OpenRC, supervisord), processes, logs, ports, pending updates, users. Also `blamixshell status <server>`. |

## Released in 1.3

| | Feature | What it means |
|---|---|---|
| ✅ | **Agent forwarding** | `ssh -A` per server (off by default), and `-A` for `blamixshell connect` / `exec`. |
| ✅ | **Vault backup / sync** | Daily encrypted backups, export / import / restore, and a vault in a synced folder (OneDrive, Syncthing, …) with automatic merging between computers. |
| ✅ | **Per-server appearance** | Server and group colors tint the tab, pane header and terminal, so production is visibly different. |

## Released in 1.1 and 1.2

| | Feature | What it means |
|---|---|---|
| ✅ | **Port forwarding / tunnels** | Local (`-L`), remote (`-R`) and dynamic SOCKS (`-D`) tunnels saved per server, started with the connection, status shown in the pane header. Also `blamixshell tunnel <server>` for headless use. |
| ✅ | **2FA / keyboard-interactive login** | Servers that ask for a verification code (Google Authenticator, Duo, PAM OTP) or any other prompt; works for jump hosts too. |
| ✅ | **New name: BlamixShell** | Formerly ShellDeck. Data and Windows installs carry over automatically. |
| ✅ | **Session restore** | Reopen the tabs and splits you had when BlamixShell was closed; tabs connect when you open them. |

## Planned

| | Feature | What it means |
|---|---|---|
| 🗓 | **Local terminal tabs** | PowerShell, cmd, WSL or Git Bash in the same window (ConPTY on Windows), bash/zsh on macOS and Linux. |
| 🗓 | **Session logging** | Record a terminal to a file, on demand or always for a server, with optional timestamps. |

## Gaps vs. other tools

Where BlamixShell doesn't match Cockpit/Webmin, Teleport or Guacamole yet. Kept here so
comparison tables stay honest.

| | Gap | Plan |
|---|---|---|
| 🚧 | **OS management GUI** (Cockpit/Webmin style) | The server dashboard covers monitoring, services, processes, logs, ports, updates and users. Still missing compared to Cockpit: installing updates from the GUI, editing users and groups, firewall and storage management. |
| 🗓 | **Air-gapped / managed networks** (already works offline: no account, no telemetry) | Turn the update check off for everyone: an MSI property (`UPDATECHECK=0`) and a setting admins can lock. "Update from file" for offline machines. A documented offline install. |
| 🗓 | **Single binary** | A one-file CLI/TUI binary for servers (no Qt, starts fast). The desktop app stays a portable folder: a one-file Qt WebEngine build unpacks ~300 MB on every start. |
| 💡 | **Teams / RBAC** (Teleport, Guacamole) | BlamixShell is single-user with a local vault. Possible path: a shared encrypted team vault (per-member keys, synced through git or a shared folder), roles (connect-only / read-only / admin) and an audit log. Real multi-tenant RBAC with enforced access needs a server component, which is a different product. |

## Later / maybe

| | Feature |
|---|---|
| 💡 | X11 forwarding |
| 💡 | Serial and Telnet connections (network gear) |
| 💡 | Mosh |
| 💡 | Server health strip (CPU, RAM, disk) in the status bar |
| 💡 | Terminal autocomplete from shell history |

## Done

| | Feature |
|---|---|
| ✅ | Server tree with groups, tags, favorites, colors and search |
| ✅ | Tabs, split panes and broadcast input |
| ✅ | SFTP browser with drag-and-drop and edit-in-place |
| ✅ | Encrypted vault (AES-256-GCM, scrypt) |
| ✅ | Jump hosts, SSH agent / Pageant, key files and pasted keys |
| ✅ | Snippets, command palette, quick connect |
| ✅ | Import from PuTTY and `~/.ssh/config` |
| ✅ | CLI and TUI for headless Linux |
| ✅ | Windows installer (per-user or all users), update checker |
