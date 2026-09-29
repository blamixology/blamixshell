# Changelog

_Generated automatically from the commit history between release tags. Don't edit by hand._

## [1.1.0](https://github.com/blamixology/shelldeck/releases/tag/v1.1.0) - 2026-09-30

### New
- Add port forwarding (L/R/D), 2FA keyboard-interactive login, session restore; roadmap; realistic MSI upgrade tests (47bffb4)

### Fixes
- Help menu + About dialog with optional 'buy me a coffee' link; FUNDING.yml; fix MSI test variable clash (5fe875c)

### Improvements
- Command palette drops down from the search bar, fits its results, shows a no-match hint (a16d6a6)

## [1.0.4](https://github.com/blamixology/shelldeck/releases/tag/v1.0.4) - 2026-09-30

### Improvements
- MSI: block installing over a copy of the other scope (all users vs just me) with a clear message (de767b8)

## [1.0.3](https://github.com/blamixology/shelldeck/releases/tag/v1.0.3) - 2026-09-29

### Fixes
- Fix MSI upgrades: close running app, remove old version after init, keep install scope on update (426a8c9)

### Improvements
- Auto-generate CHANGELOG.md and release notes from commits between tags (320b543)

## [1.0.2](https://github.com/blamixology/shelldeck/releases/tag/v1.0.2) - 2026-09-29

### Improvements
- MSI: single embedded cabinet (a346cf8)

## [1.0.1](https://github.com/blamixology/shelldeck/releases/tag/v1.0.1) - 2026-09-29

### Fixes
- Server list: full-row selection, toggle sidebar (Ctrl+Shift+L); fix toolbar toggle state at startup (8b51765)
- SFTP: tolerate login-script output before handshake, inline errors; fix tab close button spacing (526a722)

### Improvements
- Update checker (GitHub Releases, SHA-256 verified); settings UI: dropdown arrows, checkmarks, gear icon (fa13c04)

## [1.0.0](https://github.com/blamixology/shelldeck/releases/tag/v1.0.0) - 2026-09-29

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
