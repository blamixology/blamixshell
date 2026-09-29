# ShellDeck

A modern SSH client for **Windows, macOS and Linux**, written in Python. Think PuTTY, plus a proper server manager, tabs and split panes, an SFTP browser, and an encrypted vault. It comes in three front-ends that share one vault:

| | What | Runs on |
|---|---|---|
| **Desktop app** | Windows/macOS-style GUI with xterm.js terminals, splits and SFTP | Windows 10/11, macOS 12+, Ubuntu 22.04+ (and other desktop Linux) |
| **TUI** | full-screen terminal UI: browse, search, connect, run on a group | any Linux/macOS terminal, including over SSH on a headless box |
| **CLI** | `shelldeck ls / connect / exec / add …` for scripts and quick use | Linux, macOS (Windows: everything except interactive `connect`) |

![desktop](docs/desktop.png)
![tui](docs/tui.png)

## Install / run

### Windows
- **Installer (MSI):** download `ShellDeck-x.y.z-x64.msi` from Releases. The installer asks whether to install **for all users** (Program Files, needs admin) or **just for you** (no admin). It adds a Start-menu shortcut and an optional desktop shortcut, uninstalls from *Apps & features*, and upgrades in place. An installed copy keeps its data in `%APPDATA%\ShellDeck`.
  - Silent install for IT rollouts: `msiexec /i ShellDeck-x.y.z-x64.msi /qn ALLUSERS=1` (all users) or `/qn ALLUSERS=2 MSIINSTALLPERUSER=1` (current user).
- **Portable:** `ShellDeck-windows-x64.zip`, or build it yourself with **`build.bat`**, which produces `dist\ShellDeck\`. Keeps its data in a `data` folder next to the exe.
- **Build the MSI yourself:** run `build.bat`, then **`build_msi.bat`** (needs the .NET 8 SDK; it installs WiX v5 automatically).
- **From source:** install Python 3.10+, then double-click **`run.bat`**.
- The builds aren't code-signed yet, so Windows SmartScreen may show *"Windows protected your PC"*: click **More info → Run anyway**.

### macOS
- **From source:** run `./run.sh` (needs `python3`; `brew install python` if missing).
- **App bundle:** run `./build_macos.sh`, which produces `dist/ShellDeck.app` and a zip. The app is unsigned, so open it the first time with right-click → **Open**.
- **Keys:** shortcuts use **⌘**: ⌘P palette, ⌘D / ⌘E split, ⌘C / ⌘V copy/paste, ⌘F find, ⌘K clear. Ctrl is left alone for the shell.

### Ubuntu desktop (and other Linux desktops)
- **Packages:** `sudo apt install python3-venv libxcb-cursor0 libegl1`
- **From source:** run `./run.sh`
- **Portable app:** run `./build_linux.sh`, which produces `dist/ShellDeck/ShellDeck`. Then run `dist/ShellDeck/install-desktop-entry.sh` to add it to the app menu.

### Headless Linux / servers: CLI + TUI
No Qt needed; only `paramiko`, `cryptography` and `textual` are required.

```bash
./install_cli.sh          # installs `shelldeck` into ~/.local/bin (uses pipx if present)
shelldeck                 # opens the TUI
```
Or build a portable folder with no Python needed on the target: `./build_cli.sh`, which produces `dist/shelldeck-cli/shelldeck`.

### Prebuilt downloads
Pushing a tag like `v1.0.0` makes GitHub Actions (`.github/workflows/release.yml`) build Windows, macOS (Apple Silicon + Intel) and Linux packages plus the Linux CLI, then attach them to a GitHub Release.

## Features

- **Server manager:** nested groups (`Prod/EU`), tags, colors and favorites. Drag and drop, search, or filter with `tag:prod`. A green dot marks servers with a live session. **Connect to all** opens every server in a group, tiled.
- **Real terminal:** xterm.js with 256 colors and truecolor. vim, htop, tmux and mc work. Clickable links, find in scrollback, and 4 color themes.
- **Tabs and splits:** split right or down without limit. Each pane shows its connection state and an uptime clock.
- **Broadcast:** send your typing, or a snippet, to every pane in a tab.
- **SFTP panel:** drag and drop to upload. Download, rename, delete (recursive), create folders. **Edit in place:** open a file in your editor and every save uploads it.
- **Encrypted vault:** one AES-256-GCM file; the key comes from your master password via scrypt. The same file works on every OS and in every front-end.
- **Host key verification:** you confirm the fingerprint on first connect. If a key changes, the connection is refused unless you explicitly replace the key (MITM protection).
- **Auth options:** password (or ask each time), private key (a file or a pasted key, with passphrase), or SSH agent (Pageant, Windows OpenSSH, `ssh-agent`).
- **Jump hosts / bastions:** any saved server can be a jump host, including chains.
- **Imports:** PuTTY sessions (Windows) and `~/.ssh/config`, including `ProxyJump`.
- **Snippets:** saved commands, one click away.

## CLI

```text
shelldeck                          TUI (or the desktop app via `shelldeck gui`)
shelldeck ls [query]               list servers  (e.g. `shelldeck ls tag:prod`)
shelldeck connect <name|user@host:port>
shelldeck exec <query> -- <cmd>    run on many servers in parallel, colored per-host output
      e.g.  shelldeck exec group:Prod/EU -- 'df -h / | tail -1'
            shelldeck exec tag:web --accept-new -y -- sudo systemctl reload nginx
shelldeck add [--name --host --user --auth --key --group --tags --jump]
shelldeck rm <name>
shelldeck import ssh-config|putty
shelldeck passwd                   change the master password
shelldeck where                    show where data is stored
```

TUI keys: `/` search · `⏎` connect · `a` add · `e` edit · `d` delete · `f` favorite · `x` run a command on the selected group · `q` quit. When you connect, the TUI steps aside and gives you the real shell; exit the shell to come back.

## Desktop keyboard shortcuts

| Windows / Linux | macOS | Action |
|---|---|---|
| Ctrl+Shift+P / T | ⌘P / ⌘T | Command palette and quick connect |
| Ctrl+Shift+D / E | ⌘D / ⌘E | Split right / down |
| Ctrl+Shift+W | ⌘W | Close pane |
| Ctrl+Shift+C / V | ⌘C / ⌘V | Copy / paste |
| Ctrl+Shift+F | ⌘F | Find in terminal |
| Ctrl+Shift+S | ⌘S | Files (SFTP) panel |
| Ctrl+Shift+B | ⌘B | Broadcast |
| Ctrl+Shift+R | ⌘R | Reconnect |
| Ctrl+Shift+1…9 | ⌘1…9 | Switch tab |
| Ctrl + = / − / 0 | ⌘ = / − / 0 | Font size |

## Updates

ShellDeck checks GitHub Releases for a newer version at most once a day (a single anonymous request to `api.github.com`; nothing about you or your servers is sent). You can switch this off, or run **Check now**, in **Settings → Updates**. From the CLI, run `shelldeck update`.

When an update is found, an **Update x.y.z** button appears in the status bar. It opens the release notes with these options:
- **MSI install:** *Install & restart* downloads the new MSI, checks its SHA-256, and upgrades in place.
- **Portable Windows folder:** *Install & restart* downloads the new zip, swaps the program files after ShellDeck closes (your `data` folder is never touched), and restarts.
- **macOS / Linux / pip / source:** *Release page* opens the download page (or use `pipx upgrade shelldeck` / `git pull`).

## Where data lives

**Portable by default:** data goes in a `data/` folder next to the executable (next to `ShellDeck.app` on macOS, or next to `run.py` when running from source). It holds:
- `vault.sdv`: encrypted servers, passwords, keys and snippets. **Back it up.** If you forget the master password, the data can't be recovered.
- `known_hosts`: trusted host keys (OpenSSH format).
- `settings.json`: UI preferences only. No secrets.

If that folder isn't writable, or the app is installed (in `/Applications`, via pip/pipx, or under Program Files), ShellDeck uses the per-user folder instead: `%APPDATA%\ShellDeck`, `~/Library/Application Support/ShellDeck`, or `~/.config/shelldeck`. Run `shelldeck where` to see which one is in use. To share one vault between machines, copy `data/`. You can also set `SHELLDECK_HOME` to point anywhere.

## Notes
- PuTTY `.ppk` keys: in PuTTYgen, open the key and choose **Conversions → Export OpenSSH key**, then use the exported file.
- Tests: `pip install pytest pexpect textual`, then `pytest tests`. To include the live SSH tests, set `SHELLDECK_TEST_SSH=host:port:user:password`.

## Project layout

```
shelldeck/
  main.py          desktop entry: unlock/create vault
  app.py           main window, palette, tree actions, imports
  server_tree.py   sidebar tree + custom row painting
  session_tab.py   tab with nested split panes, broadcast routing
  terminal.py      xterm.js <-> Python bridge (QWebChannel), pane UI
  ssh_session.py   Qt wrapper: shell I/O thread → signals
  ssh_core.py      Qt-free SSH core: auth, host keys, jump hosts (shared by all front-ends)
  sftp_panel.py    SFTP browser, transfers, edit-in-place
  cli.py           command line + interactive raw-tty shell + parallel exec
  tui.py           Textual full-screen UI
  vault.py         AES-256-GCM + scrypt encrypted store
  models.py        Server / Snippet / Store
  dialogs.py       unlock, server editor, settings, snippets, palette
  theme.py         colors, stylesheet, SVG icons, Windows title bar
  platform_ui.py   per-OS fonts, scaling and shortcut labels
  assets/          xterm.js 6 (MIT), terminal.html, icons
```

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
