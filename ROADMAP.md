# Roadmap

What's planned for ShellDeck, roughly in order. Ideas and votes are welcome in
[issues](https://github.com/blamixology/shelldeck/issues).

Legend: ✅ done · 🚧 in progress · 🗓 planned · 💡 later / maybe

## Next release (in `main`, not released yet)

| | Feature | What it means |
|---|---|---|
| ✅ | **Port forwarding / tunnels** | Local (`-L`), remote (`-R`) and dynamic SOCKS (`-D`) tunnels saved per server, started with the connection, status shown in the pane header. Also `shelldeck tunnel <server>` for headless use. |
| ✅ | **2FA / keyboard-interactive login** | Servers that ask for a verification code (Google Authenticator, Duo, PAM OTP) or any other prompt; works for jump hosts too. |
| ✅ | **Session restore** | Reopen the tabs and splits you had when ShellDeck was closed; tabs connect when you open them. |

## Planned

| | Feature | What it means |
|---|---|---|
| 🗓 | **Local terminal tabs** | PowerShell, cmd, WSL or Git Bash in the same window (ConPTY on Windows), bash/zsh on macOS and Linux. |
| 🗓 | **Session logging** | Record a terminal to a file, on demand or always for a server, with optional timestamps. |
| 🗓 | **Agent forwarding** | `ssh -A`: use your local keys for `git pull` on the server or the next hop. |
| 🗓 | **Vault backup / sync** | Export and import the encrypted vault, or keep it in a folder you choose (OneDrive, Syncthing, …) to have the same servers on every machine. |
| 🗓 | **Per-server appearance** | Terminal theme and tab tint per server or group, so production is visibly different. |

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
