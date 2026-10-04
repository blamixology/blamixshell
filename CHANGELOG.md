# Changelog

_Generated automatically from the commit history between release tags. Don't edit by hand._

## [1.11.0](https://github.com/blamixology/blamixshell/releases/tag/v1.11.0) - 2026-10-05

### Improvements
- Dashboard: Storage tab, new systemd service form + unit viewer, low-resource warnings (opt-in), Markdown server report (2833133)

## [1.10.1](https://github.com/blamixology/blamixshell/releases/tag/v1.10.1) - 2026-10-05

### Improvements
- Build only on tags and manual runs, readable run names; users: also read /etc/passwd and /etc/group directly (c1a34ce)
- README: add build, release, downloads, license, python and platform badges (ec055ad)

## [1.10.0](https://github.com/blamixology/blamixshell/releases/tag/v1.10.0) - 2026-10-04

### Improvements
- Dashboard: Firewall tab (firewalld/ufw edit, iptables/nft read-only), cron run now + backups/restore, logs filter and saved views (12fb3fb)

## [1.9.4](https://github.com/blamixology/blamixshell/releases/tag/v1.9.4) - 2026-10-02

### Improvements
- Cron tab: read the spool file when crontab -l prints nothing, show exit code and source in the diagnostic (b811c3f)
- Roadmap: note CI duplicate builds and run names (53f530b)

## [1.9.3](https://github.com/blamixology/blamixshell/releases/tag/v1.9.3) - 2026-10-02

### Improvements
- Cron tab: hint about system/root jobs when a user's crontab is empty, list periodic scripts (cron.daily etc.) in the system view (2dd4245)

## [1.9.2](https://github.com/blamixology/blamixshell/releases/tag/v1.9.2) - 2026-10-02

### Improvements
- Dashboard users: find directory (LDAP/SSSD) accounts by name, show system accounts toggle, note when the account list is empty (bc3190e)

## [1.9.1](https://github.com/blamixology/blamixshell/releases/tag/v1.9.1) - 2026-10-02

### Improvements
- Cron form: browse server for script/folder, script exists/executable check, output to log or discard, run-in folder, login shell, % escaping (e1bd8c3)

## [1.9.0](https://github.com/blamixology/blamixshell/releases/tag/v1.9.0) - 2026-10-02

### Improvements
- Dashboard: Cron tab with a simple job form, next-run preview, enable/disable, edit as text (44d0ccb)

## [1.8.1](https://github.com/blamixology/blamixshell/releases/tag/v1.8.1) - 2026-10-02

### Improvements
- Dashboard users: list accounts below UID 1000 and logged-in users, custom home folder when adding a user (8cc5611)

## [1.8.0](https://github.com/blamixology/blamixshell/releases/tag/v1.8.0) - 2026-10-02

### Improvements
- Dashboard: refresh interval selector; add, delete, lock users and edit groups (0289457)

## [1.7.0](https://github.com/blamixology/blamixshell/releases/tag/v1.7.0) - 2026-10-02

### Improvements
- Install updates from the dashboard and autocomplete from command history (both opt-in, off by default) (2e388f9)

## [1.6.2](https://github.com/blamixology/blamixshell/releases/tag/v1.6.2) - 2026-10-01

### Improvements
- Health strip: CPU shows immediately, swap, detailed tooltip, stale marker, refresh interval setting (d49b6f5)

## [1.6.1](https://github.com/blamixology/blamixshell/releases/tag/v1.6.1) - 2026-10-01

### Fixes
- Fix misaligned split-button dropdown arrows in the toolbar (3eaac4a)

## [1.6.0](https://github.com/blamixology/blamixshell/releases/tag/v1.6.0) - 2026-10-01

### Improvements
- Command log, session recordings, status-bar health strip; offline/managed installs (UPDATECHECK, install update from file) (379291c)

## [1.5.3](https://github.com/blamixology/blamixshell/releases/tag/v1.5.3) - 2026-10-01

### Fixes
- Splits: fix nested splits closing terminals; rotate split; split with another server (8ef80b6)

## [1.5.2](https://github.com/blamixology/blamixshell/releases/tag/v1.5.2) - 2026-10-01

### Fixes
- Local terminal: keep the output of programs that exit at once (macOS); fixes the macOS build (28244e0)

## [1.5.1](https://github.com/blamixology/blamixshell/releases/tag/v1.5.1) - 2026-10-01

### Improvements
- About: made by Blamixology (logo + link); source runs show the release version (b316dbb)

## [1.5.0](https://github.com/blamixology/blamixshell/releases/tag/v1.5.0) - 2026-10-01

### Improvements
- AWS SSM: SSH over SSM, SSM shells, EC2 Instance Connect, SSO sign-in, instance import (e346dff)

## [1.4.1](https://github.com/blamixology/blamixshell/releases/tag/v1.4.1) - 2026-10-01

### Improvements
- Dashboard: services on CentOS 7 and non-systemd servers (SysV, OpenRC, supervisord) (08a0c50)

## [1.4.0](https://github.com/blamixology/blamixshell/releases/tag/v1.4.0) - 2026-09-30

### New
- Add server dashboard: overview, services, processes, logs, ports, updates, users; blamixshell status (b9caffc)

## [1.3.0](https://github.com/blamixology/blamixshell/releases/tag/v1.3.0) - 2026-09-30

### New
- Add agent forwarding (ssh -A), vault backups and sync between computers, server and group colors (c85e872)

### Fixes
- Fix macOS CI: short socket path for the agent-forwarding test (db94ebe)

## [1.2.1](https://github.com/blamixology/blamixshell/releases/tag/v1.2.1) - 2026-09-30

### Improvements
- Improve release script: tests, confirmation, version checks; skip CI for CHANGELOG-only pushes (c76b3ac)
- Updater: never wait forever for the old app, no console window, restart the renamed app, log to %TEMP% (9797d05)

## [1.2.0](https://github.com/blamixology/blamixshell/releases/tag/v1.2.0) - 2026-09-30

### Improvements
- Rename ShellDeck to BlamixShell (data and MSI installs carry over); roadmap gaps; AI disclosure (da8c0a5)

## [1.1.0](https://github.com/blamixology/blamixshell/releases/tag/v1.1.0) - 2026-09-30

### New
- Add port forwarding (L/R/D), 2FA keyboard-interactive login, session restore; roadmap; realistic MSI upgrade tests (47bffb4)

### Fixes
- Help menu + About dialog with optional 'buy me a coffee' link; FUNDING.yml; fix MSI test variable clash (5fe875c)

### Improvements
- Command palette drops down from the search bar, fits its results, shows a no-match hint (a16d6a6)

## [1.0.4](https://github.com/blamixology/blamixshell/releases/tag/v1.0.4) - 2026-09-30

### Improvements
- MSI: block installing over a copy of the other scope (all users vs just me) with a clear message (de767b8)

## [1.0.3](https://github.com/blamixology/blamixshell/releases/tag/v1.0.3) - 2026-09-29

### Fixes
- Fix MSI upgrades: close running app, remove old version after init, keep install scope on update (426a8c9)

### Improvements
- Auto-generate CHANGELOG.md and release notes from commits between tags (320b543)

## [1.0.2](https://github.com/blamixology/blamixshell/releases/tag/v1.0.2) - 2026-09-29

### Improvements
- MSI: single embedded cabinet (a346cf8)

## [1.0.1](https://github.com/blamixology/blamixshell/releases/tag/v1.0.1) - 2026-09-29

### Fixes
- Server list: full-row selection, toggle sidebar (Ctrl+Shift+L); fix toolbar toggle state at startup (8b51765)
- SFTP: tolerate login-script output before handshake, inline errors; fix tab close button spacing (526a722)

### Improvements
- Update checker (GitHub Releases, SHA-256 verified); settings UI: dropdown arrows, checkmarks, gear icon (fa13c04)

## [1.0.0](https://github.com/blamixology/blamixshell/releases/tag/v1.0.0) - 2026-09-29

### New
- Add Windows MSI installer (per-user or all-users), test it in CI; run tests on all OSes + live SSH before builds (f8b6d5a)

### Fixes
- Fix MSI file paths; pin cryptography<49 on Intel macOS (44796df)
- Fix MSI file paths; pin cryptography<49 on Intel macOS (no wheels, broken OpenSSL bundling) (ca43acd)
- Fix test imports in CI: add repo root to pytest pythonpath (4fb7170)

### Improvements
- MSI: install to 64-bit Program Files / LocalAppData\Programs (b0935f1)
- Macos-13 runners are retired, build Intel macOS on macos-15-intel (bcac78a)
- Enable CI workflow, mark shell scripts executable, drop unused splash image (5d86190)
- Initial release: ShellDeck v1.0.0 (371e2eb)
