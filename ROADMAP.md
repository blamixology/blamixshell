# Roadmap

What's planned for BlamixShell, roughly in order. Ideas and votes are welcome in
[issues](https://github.com/blamixology/blamixshell/issues).

Legend: ✅ done · 🚧 in progress · 🗓 planned · 💡 later / maybe

## Next release (in `main`, not released yet)

| | Feature | What it means |
|---|---|---|
| ✅ | **Dashboard reconnects** | When the connection drops, or after a reboot you started from the dashboard, a banner counts down and reconnects by itself (growing delays, patient for 15 minutes after a reboot, none after a shutdown); the tabs reload when the server is back. Reconnect now: the button in the desktop dashboard, `R` in `dash`. |
| ✅ | **Firewall actions for iptables and nftables** | Open or close a port, save the rules, start the firewall, with the same protection for the SSH port as firewalld / ufw. Buttons that can't be used say why (no sudo, firewall stopped, read-only manager). |
| ✅ | **More security checks, with details** | Listening services on risky ports, sensitive file permissions, fail2ban, automatic updates, pending security updates, failed-login sources, recent logins. Double-click a row (desktop) or press Enter / `d` (terminal) for a window with the details and how to fix it. Double-click works the same on the other tables. |
| ✅ | **One-file CLI binaries and self-update** | Release assets `blamixshell-linux-x86_64`, `-aarch64` and `-musl-x86_64` (built for old glibc, so they run on CentOS 7; selftest in CI). `blamixshell update --install` replaces the running binary after checking its SHA-256. |
| ✅ | **Command log** | Who ran what, where and when: one line per command (as shown on screen), plus dashboard actions and `exec`. Per server or for all. |
| ✅ | **Session recordings** | ● on a terminal, or always per server: everything it shows, as clean text or raw, optionally time-stamped. Retention in days. |
| ✅ | **Health strip** | CPU, memory, disk and load of the active server in the status bar; click for the dashboard. |
| ✅ | **Install updates from the dashboard** | Optional (Settings, off by default): an *Install updates* button on the Updates tab runs the non-interactive upgrade (apt, dnf, yum, zypper, pacman, apk) after a confirmation. |
| ✅ | **Users and groups** | Add, delete, lock / unlock accounts and change their groups from the dashboard (confirmed, run with sudo). Passwords are set in the terminal, never sent from the dashboard. |
| ✅ | **Cron editor** | A Cron tab in the dashboard: see a server's scheduled jobs in plain words, add or edit them from a simple form (every few minutes / hour / day / week / month / at startup, with a preview of the next runs), browse the server for the script (with an exists / executable check), choose where the output goes, a working folder and a login shell, switch jobs off without deleting them, or edit the whole crontab as text. Own jobs, root or another user's (sudo), and system jobs read-only. |
| ✅ | **System, Network and Mounts tabs** | System: time zone, clock drift against your computer, NTP state, reboot or shut down now or in N minutes (and cancel), "reboot required", add a swap file (checked for free space, written to fstab) or remove one. Mounts: `/etc/fstab` compared with what is mounted, mount / unmount (never the system's own mounts), and a dry check of the fstab. Network: interfaces, addresses, routes, DNS, and name lookup / ping / TCP checks run from the server. In the desktop and the terminal dashboards, `blamixshell show <server> system|mounts|network`, and in the report. Every change asks first. |
| ✅ | **Dashboard in the terminal** | `blamixshell dash <server>` (or `i` in the TUI): the desktop dashboard's tabs (overview, services, processes, logs, ports, updates, users, cron, firewall, docker, timers, storage, security) on any headless box, with sorting, filtering, confirmed actions (services, processes, containers, timers) and sudo on request. Also `blamixshell show <server> <tab>` (`--json`, `--filter`, `--sort`, `--check` as a health-check exit code) and `blamixshell report <server>`. |
| ✅ | **Sortable, filterable dashboard tables** | Click a column header to sort Services, Processes, Ports, Updates, Storage and the biggest-folders list (by value: 2 < 10, 900 KB < 1.2 GB, 10:00 < 1-00:00:00); the choice and the selected row survive refreshes. Filters: services by text and by state (running, not running, starts at boot, doesn't), processes by text and by user (500 loaded, "N of M" shown), ports and updates by text. |
| ✅ | **Appearance settings** | A new Settings > Appearance tab: interface theme (Midnight, Graphite, Nord, Solarized Dark, Light, High contrast), interface font and text size, and font and size of the tabs (terminal tabs and dashboard tabs). Every choice previews live as you select it, including the terminal's font, size, line height, cursor and colors in the open terminals, and is kept on Save; Cancel puts the previous look back. The terminal's own font and colors stay on the Terminal tab, now labelled "Terminal color theme". |
| ✅ | **Focus mode** | `Ctrl+Shift+H` hides the server list, toolbar, status bar and files panel: only the tabs and the terminal stay. Leave it with the same shortcut, the right-click menu of a tab or of the terminal, the button at the left of the tabs, or the command palette. It remembers your layout and status messages appear as a short note on screen. |
| ✅ | **Docker / Podman tab** | Containers with state, image, ports, CPU and memory; start, stop, restart, resume, logs and remove (confirmed; sudo when the account isn't in the docker group). |
| ✅ | **SSH keys** | In the Users tab: see an account's authorized_keys with fingerprints, add a public key (paste or .pub file), remove one; a copy of the previous file is kept. |
| ✅ | **Systemd timers** | List timers with schedule, next and last run; enable / disable, run now, view the unit; make a new timer from the same simple form as cron jobs. |
| ✅ | **Security checks** | Quick read-only checks: SSH root and password login, UID 0 and password-less accounts, failed logins, NOPASSWD sudo, SELinux, firewall, ports open to the world; checks that need root say so and can be re-run with sudo. |
| ✅ | **Storage tab** | Filesystems with size, use and inode use (colored when nearly full), and the biggest folders under any path (stays on one filesystem; double-click to go deeper). |
| ✅ | **New systemd service** | A form (command, user, folder, restart policy, environment) with a live preview of the unit file; it writes the unit, reloads systemd and optionally enables and starts it. Also a viewer for any service's unit file. |
| ✅ | **Low-resource warnings** | Optional (Settings, off by default): a status-bar message and a flashing taskbar icon when the active server crosses disk or memory 90%, swap 60% or load 1.5 per CPU. |
| ✅ | **Server report** | One button collects overview, failed services, updates, ports, accounts, cron jobs and firewall into a Markdown report to save or paste into a ticket. |
| ✅ | **Firewall tab** | firewalld and ufw: see the rules, open or close a port, allow a service, reload (confirmed, with sudo); rules for the SSH port can't be removed from here. iptables and nftables are editable too (see above). |
| ✅ | **Cron: run now and backups** | Run a job once and see its output; a copy of the crontab is kept before every change, with Restore… to go back. |
| ✅ | **Logs: filter and saved views** | Filter the lines on screen as you type; save a service + level + size + filter as a named view per server. |
| ✅ | **Dashboard refresh interval** | Pick 2 / 5 / 10 / 30 / 60 seconds in the dashboard header; remembered. |
| ✅ | **Autocomplete from history** | Optional (Settings, off by default): a grey suggestion from the commands you typed on this server; Right arrow accepts it. |
| ✅ | **Offline / managed installs** | `UPDATECHECK=0` (MSI), Group Policy, or `policy.ini` turns the update check off and locks it; **Install update from file** for offline machines; documented silent installs. |

## Released in 1.5

| | Feature | What it means |
|---|---|---|
| ✅ | **AWS Systems Manager** | SSH over SSM (files, tunnels, dashboard work), plain SSM shells, EC2 Instance Connect one-time keys, AWS SSO sign-in with automatic reconnect, and import of SSM-managed instances. CLI: `blamixshell aws login / instances / import`. |
| ✅ | **Better splits** | Mix servers in one tab (the arrow on the split buttons), rotate a split, and nested splits no longer close terminals. |
| ✅ | **Made by Blamixology** | In About and the Help menu. |

## Released in 1.4

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
| 🗓 | **Local terminal tabs** | PowerShell, cmd, WSL or Git Bash in the same window (ConPTY on Windows), bash/zsh on macOS and Linux. The terminal layer is already in place (it runs SSM shells). |
| 🗓 | **Open a server from the command line / from BlamixFiles** | `blamixshell gui --connect "<server>"` or `--connect user@host:port` (optional `--key`, `--jump`; never a password on the command line). If BlamixShell is already open, the new launch hands the request to it over a local socket instead of opening a second window; a server with the same host, port and user in the vault is reused, otherwise the New server dialog opens prefilled. Needed for BlamixFiles' planned "Open in BlamixShell" button (SFTP / SCP sites only). |

## Gaps vs. other tools

Where BlamixShell doesn't match Cockpit/Webmin, Teleport or Guacamole yet. Kept here so
comparison tables stay honest.

| | Gap | Plan |
|---|---|---|
| 🚧 | **OS management GUI** (Cockpit/Webmin style) | The server dashboard covers monitoring, services, processes, logs, ports, updates and users. Updates can now be installed from the GUI (opt-in). Users, groups, cron jobs and the firewall (firewalld, ufw, iptables, nftables) can be edited. Still missing compared to Cockpit: creating partitions / volumes (the Storage tab only reads; mounts, swap and the fstab are covered). |
| ✅ | **Air-gapped / managed networks** (no account, no telemetry) | Done in the next release: update check off by policy, install update from file, documented offline install. |
| ✅ | **Single binary** | Done: one-file CLI/TUI/dashboard binaries on the release page, with `blamixshell update --install`. The desktop app stays a portable folder: a one-file Qt WebEngine build unpacks ~300 MB on every start. |
| 💡 | **Teams / RBAC** (Teleport, Guacamole) | BlamixShell is single-user with a local vault. Possible path: a shared encrypted team vault (per-member keys, synced through git or a shared folder), roles (connect-only / read-only / admin) and an audit log. Real multi-tenant RBAC with enforced access needs a server component, which is a different product. |

## Later / maybe

| | Feature |
|---|---|
| 💡 | X11 forwarding |
| 💡 | Serial and Telnet connections (network gear) |
| 💡 | Mosh |

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
