# Changelog

All notable changes to **fifine Control Deck** are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/), and the project
follows [Semantic Versioning](https://semver.org/).

## [0.8.0] - 2026-07-19
### Added
- **Press-and-hold key actions** (#4): every key can carry a second action
  that fires after holding it ~0.5 s — long-press *Back* to exit a folder,
  long-press a monitor key to run something, double what 15 keys can do.
  Keys without a hold action keep firing instantly on press-down, exactly as
  before; only keys that define one wait for the release/threshold.
- **GPU temperature, one click** (#4): a dedicated `gputemp` monitor metric
  that auto-picks the sensor (NVIDIA via NVML, AMD via the `amdgpu` chip's
  edge sensor) — no more manual `chip:label` targets for the common case.
- **Clock formats** (#4): 12h/24h with or without seconds, and date styles
  (weekday, ISO, US, or none) for the clock key. "Auto" keeps the previous
  behavior (seconds only when refreshing under 5 s).

## [0.7.0] - 2026-07-18
### Added
- **Monitor keys, round 2** (#3): **GPU load** (NVIDIA via NVML, AMD via
  sysfs `gpu_busy_percent` — same graceful per-vendor detection as VRAM) and
  **temperatures** (any `psutil` sensor; auto-picks the CPU package, or set
  the target to `chip` / `chip:label`, e.g. `nvme:Composite`) as new metrics,
  plus a simple **clock key** (time + date; shows seconds at refresh
  intervals under 5 s).
- **Flatpak: "Start on login" now works.** Inside the sandbox the toggle used
  to write a `.desktop` file into the sandbox home — a silent no-op. It now
  asks the XDG **Background portal**; if the desktop denies the request the
  toggle reverts and says why instead of pretending.
### Changed
- **Gauge face is readable at arm's length**: the value text grew from 20% to
  26% of the key size and the metric label moved out of the arc into the
  gauge's bottom opening (live-dogfooding feedback from the physical deck).
- Autostart entry path honors `XDG_CONFIG_HOME` instead of hardcoding
  `~/.config`.
### Fixed
- **VRAM metric no longer dead on AMD systems.** pynvml is installed by
  default (deb Recommends, snap bundles it) and imports fine without an
  NVIDIA driver — the probe treated any NVML failure as "retry later" and
  never reached the working amdgpu sysfs backend. NVML failure now falls
  through to sysfs (flaw carried since 0.6.0; the new GPU-load metric uses
  the corrected probe from the start).

## [0.6.2] - 2026-07-18
### Fixed
- **Icons finally keep the one you picked.** 0.6.1's fix was incomplete: an
  icon chosen from the Library was still lost when you then typed the
  command/URL/hotkey (the natural order). Icons and labels now track
  *provenance* — only an untouched auto-assigned icon ever follows the
  action; your choice survives every edit, an explicit clear stays cleared,
  and dropping a second action onto a key re-skins auto identities without
  touching custom ones.
- **A full editor audit (25 more fixes).** Highlights: selecting a password
  key no longer touches the keyring or pops warnings; unknown action types,
  values and deleted profile targets from other versions round-trip instead
  of being silently rewritten; a dropped *Switch profile* key works
  immediately; a device reconnect no longer wipes your selection or an open
  picker dialog; page keys navigate folder pages correctly and *Go to page*
  is clamped; knob editors follow page/profile switches; autosave failures
  are shown instead of silently losing edits; double-click-created folders
  are saved; an undecodable GIF renders statically instead of freezing the
  key; stale monitor frames can't repaint the wrong page.
- **Type-text via legacy ydotool 0.1.8 (jammy)**: the text is passed as
  `/dev/stdin` instead of `-`, which old ydotool misread as a filename.

## [0.6.1] - 2026-07-18
### Fixed
- **Choosing an icon from the Library did nothing.** The key editor re-applied
  the action's default icon on every edit — and picking an icon is itself an
  edit, so the chosen icon was overwritten in the same moment. The icon now
  follows the action only when the action actually changes; custom File… icons
  were never affected.
- **Config no longer lost under Flatpak**: `XDG_CONFIG_HOME` is honored, so the
  configuration persists instead of being written into the sandbox's throwaway
  home.
- **Key text no longer renders tiny on Fedora/Flatpak runtimes**: the font
  search now covers their DejaVu/Liberation layouts instead of silently falling
  back to a bitmap font.

## [0.6.0] - 2026-07-17
### Added
- **System-monitor keys** (#2): a new *System monitor* action turns a key into
  a live readout of **CPU, RAM, VRAM, network rate, or disk space**, styled as
  a big number, a 270° gauge (percentage metrics — a network key falls back to
  the number face), or a scrolling graph, refreshed at a per-key interval
  (0.5–60 s). Number/gauge keys are re-pushed to the device only when their
  displayed value changes (graphs advance every interval), keys showing the
  same metric share one sample stream, and with no monitor keys on the visible
  page nothing is ever sampled. VRAM is detected per vendor (NVIDIA via NVML —
  install the recommended `python3-pynvml`; AMD via sysfs; unavailable on
  shared-memory iGPUs). Pressing a monitor key does nothing, and press-flash
  skips it so the readout is never overpainted. New dependency:
  `python3-psutil`.

## [0.5.8] - 2026-07-17
### Fixed
- **Device access for users not in `plugdev`.** The udev rule was numbered
  `99-`, but systemd dispatches the `uaccess` tag at `73-seat-late.rules`, so
  the tag was set too late and never granted an ACL — on a stock install the
  deck was inaccessible unless you happened to be in `plugdev`. The rule is now
  `70-fifine-deck.rules` (verified on hardware: the active-seat user gets an
  explicit ACL), and installers clean up the stale `99-` copy.
- **Passwords no longer pass through argv.** The "type password" action handed
  the secret to xdotool/ydotool/wtype on the command line, where any local
  process could read it via `/proc/<pid>/cmdline`. All three tools now receive
  the text on stdin; a hung helper can no longer leak it into the journal
  either. Side benefit: backslashes in passwords are now typed literally.
- **A locked keyring no longer destroys a saved password.** Opening a key's
  editor while the keyring was locked rendered the password field empty, and
  editing any other field then silently dropped the binding forever. The
  binding is now preserved whenever the secret couldn't be read; clearing a
  *readable* password still works. Falling back to cleartext storage (no usable
  keyring) now warns instead of happening silently.
- **Folders survive action changes.** Changing a folder key's action type —
  including by accidentally scrolling the mouse wheel over the Action dropdown
  — destroyed the folder and every page in it, with no confirmation and no
  undo. Folders now go dormant and return intact; dropdowns no longer respond
  to hover-scroll at all.
- **Profile add / config import no longer strand the app inside a stale
  folder.** Both paths now reset navigation and re-render, so edits land where
  the window says they do and the deck shows the profile you switched to.
- **Single instance is actually enforced.** The old startup unconditionally
  removed the IPC socket — including a *live* instance's — so racing launches
  (autostart + launcher click) yielded two apps fighting over the deck and
  overwriting each other's config. The socket is now claimed before any heavy
  startup work, and a stale socket is only reclaimed after proving nobody is
  listening.
- `config.json` is created with private permissions from the first byte
  (previously written world-readable and chmod'd afterwards).
- `./install.sh` looked for a `.deb` filename no build ever produced and
  failed 100% of the time; it now finds or builds the right package.
### Changed
- GitHub releases are now gated on the full test suite, type check, lint, and
  smoke test running against the tagged commit (previously tags published
  entirely untested builds).
- `build-deb.sh` refuses to package a payload missing the USB transport
  library (same guard the PPA path already had).
- AppStream metainfo now tracks releases (was stuck at 0.5.2) and `release.sh`
  maintains it automatically; CI fails on version skew.
- Test suite: 43 → 220 tests, covering the GUI (thread marshalling, snap
  access dialog, editors), the action engine, device input decoding, packaging
  invariants, and every fix above via regression tests proven to fail on the
  old code.

## [0.5.7] - 2026-07-13
### Added
- **Snap: classic-confinement build that actually drives the deck.** The deck is
  controlled over `/dev/hidraw`, which strict confinement cannot grant (its
  transport uses hidapi's hidraw backend); the classic build opens the device
  like the `.deb` does.
- **Snap: one-click "Enable device access" button.** A snap can't install the
  udev rule the deck needs, so the classic snap bundles the rule and, when the
  device isn't reachable, offers a button that installs it via `pkexec`
  (graphical auth) and reconnects live — no terminal, no relaunch.
### Fixed
- Snap: bundle the Python interpreter + stdlib and pin `PYTHONHOME` so the
  classic snap boots reliably (core24's base provides `python3.12`, so snapcraft
  otherwise prunes it from the payload — fatal for a classic snap at runtime).
- Snap: show the device-access hint even when the deck enumerates over libusb
  with empty firmware (previously the false "connected" suppressed it).
### Changed
- Packaging: `debian/source/options` keeps build artifacts (`dist/`, `*.snap`,
  caches) out of the native source tarball, slimming PPA source uploads.
- Docs: `SNAP.md` documents the working classic build; the README leads with the
  PPA and notes the strict store snap can't drive the deck.

## [0.5.6] - 2026-07-12
### Fixed
- Eliminated a harmless Qt 6 / Wayland startup warning (*"Failed to register
  with host portal … Connection already associated with an application ID"*) by
  setting the application identity (name and desktop file name) via the static
  `QGuiApplication` setters **before** constructing `QApplication`, so the
  Wayland / xdg-desktop-portal integration has the correct app-id at init time.

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
