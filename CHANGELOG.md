# Changelog

All notable changes to **fifine Control Deck** are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/), and the project
follows [Semantic Versioning](https://semver.org/).

## [0.5.5] - 2026-07-12
### Fixed
- `.deb`/PPA packages now recommend **`python3-pyudev`**, restoring
  netlink-based USB hotplug on fresh installs (previously the package omitted
  it and silently fell back to polling).
### Added
- When running as a confined **snap** with no device detected, the app now
  shows an in-app hint explaining how to grant USB access
  (`sudo snap connect … raw-usb` / `hardware-observe`), with a
  "don't show again" option — instead of appearing to do nothing.
- `[snap]` marker in the status-bar environment summary.

## [0.5.4] - 2026-07-12
### Added
- Unit + **controller test suite** with a mock-device harness (no hardware needed).
- **`logging`** framework for diagnostics (level via `$FIFINE_LOG`, default INFO).
- **mypy** type-checking in CI (advisory).
- "Type password" action stores secrets in the **system keyring**, not the config.
- CHANGELOG, CONTRIBUTING, GitHub issue templates, and a vendored-binary
  provenance note (`docs/PROVENANCE.md`).

## [0.5.3] - 2026-07-12
### Added
- AppStream metainfo for the `.deb` (rich GNOME Software / App Center listing).
- GitHub Pages landing page; **tag→release** GitHub Actions workflow.
- **ruff** lint in CI (replacing flake8).
- Flatpak packaging scaffold with sandbox-aware action routing.
- arm64 + Ubuntu 26.04 (resolute) PPA builds.
### Changed
- Slimmer packages (listing assets excluded from the payload).
- `.deb` downloads served from GitHub Releases (not committed in-repo).

## [0.5.2] - 2026-07-12
### Added
- Snap Store + Launchpad PPA publishing; custom app icon and store assets.

## [0.5.1] - 2026-07-12
### Added
- Multi-action (multi-step) editor.

## [0.5.0] - 2026-07-12
### Added
- Folders (nested key-sets) with breadcrumb + Back navigation.

## [0.4.0] - 2026-07-12
### Changed
- Production hardening: device-I/O locking, subprocess timeouts, config safety
  (0600 + corrupt-config recovery), portable `lib:` icon references.

## [0.3.0] - 2026-07-11
### Added
- Drag-and-drop actions catalog, multiple profiles and pages, knob/dial support,
  export/import config, custom + generated key icons.

## [0.1.0] - 2026-07-11
### Added
- Initial release: Stream Dock (293V3, USB 3142:0060) device I/O, per-key image
  rendering, core actions, and the PyQt6 configuration GUI.
