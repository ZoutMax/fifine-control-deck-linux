# Changelog

All notable changes to **fifine Control Deck** are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/), and the project
follows [Semantic Versioning](https://semver.org/).

## [0.11.2] - 2026-07-22

Follow-up to 0.11.1, from a targeted re-audit and the first real soak test
against the hardware.

### Fixed
- **Memory no longer climbs while you use the app.** Every page, profile or
  folder switch repainted all fifteen keys, even the ones already showing
  exactly that picture. Redundant writes are now skipped. Over a soak of 600
  switches the process grew 82 MB before and 7 MB after. The underlying cost is
  inside the vendored device library and cannot be removed here, so the fix is
  to stop asking for work that changes nothing — which also cuts USB traffic.
- **Animated keys stop re-decoding when a page has more than six of them.** The
  decode cache held six entries while the deck has fifteen keys, so a page full
  of animations evicted and re-decoded on every switch, and each key visibly
  flicked from static back to animated.
- **A password inside a Multi-action is no longer exported without warning.**
  The export check looked at the wrong level of a multi-action step, so it never
  saw those passwords — precisely the case the warning exists for.
- **An animated key no longer loses its background decoding after an unplug.**
  A decode skipped because the deck had just been unplugged was recorded as a
  permanent failure, so after plugging back in that key decoded on the interface
  thread again, stalling the window for about a quarter of a second.
- **The app now says when it cannot read your configuration.** It renames the
  unreadable file and starts fresh, which from the outside looks exactly like
  losing every profile. It now tells you, and where the old file went.

### Internal
- The test suite was failing on a developer machine while passing in CI, because
  tests could reach the real home directory. Every test now runs against a
  sandboxed home, with a tripwire that fails if that protection is removed.

## [0.11.1] - 2026-07-21

### Fixed
- **Animated keys no longer stall the window the first time they appear.**
  0.11.0 stopped re-decoding an animation every time you switched to its page,
  but the very first decode still ran on the interface thread — about a quarter
  of a second of frozen window per animated key, and key presses arriving in
  that window were handled late. Decoding now happens on its own thread: the
  key shows its static face immediately and starts animating a moment later.
  Measured with a 90-frame animation, the interface thread's share went from
  ~244ms to 0.1ms.

## [0.11.0] - 2026-07-21

Clears the audit backlog: every open finding from the pre-0.10.0 reviews is
fixed except one, which is documented rather than rushed. Most of this is in
the device layer, where the app was doing the wrong thing quietly.

### Fixed
- **A quick unplug-and-replug no longer leaves the deck dead.** Reconciliation
  compared device *paths*, and `/dev/hidrawN` is reused — so a replug that
  completed within about a second looked like nothing had happened. The app
  kept a handle on a torn-down connection: every key silently dead, the status
  bar still reading "connected", and no way back except restarting. It now
  compares the identity of the device node itself, which changes when the node
  is recreated. Confirmed with a real replug.
- **Installing the udev rule now takes effect without a restart.** The
  `udevadm trigger` command the documentation tells you to run emits a "change"
  event, and the app ignored those entirely — so the documented fix for "no
  device access" could never work while the app was running.
- **A deck that is plugged in but unusable no longer claims to be connected.**
  A handle that opened but never identified itself was reported as a working
  connection, showing "connected" with every key dead. The status bar now
  distinguishes a deck that is absent from one it cannot open, and says which.
- **The hotplug watchdog can no longer be starved.** Its periodic rescan ran
  only after a full minute with no USB activity of any kind, so on a machine
  with a webcam or dock it could go hours without running. It now runs on a
  schedule. A failure to start the hotplug listener also falls back to polling
  instead of leaving hotplug dead for the session.
- **Animated keys no longer re-decode on every page switch.** Switching pages,
  profiles or folders decoded and re-encoded every frame of every animated key
  again from scratch, on the UI thread. Measured on a 90-frame animation: 244ms
  before, 0ms once cached. The first decode of each file still costs that, and
  is tracked in `docs/KNOWN-ISSUES.md`.
- **Unplugging during quit can no longer crash the app**, and a stale frame can
  no longer be left lit on a key after the app closes.
- **Typing keys, password keys and config export now say what they are doing.**
  A "Type text" key with no keystroke tool installed did nothing silently; a
  password key typed an empty string when the keyring was locked; exporting a
  configuration wrote a plaintext password into the file with no warning.
- **`--enable-autostart` no longer reports success without doing anything**
  when the autostart entry was changed behind the running app's back.
- **The window no longer appears frozen for two seconds when quitting.** The
  remaining delay is inside a vendored binary and cannot be removed here; the
  window now closes immediately while cleanup finishes.

## [0.10.2] - 2026-07-21

### Fixed
- **Pulling the cable while an animated key is playing can no longer kill the
  app.** The animation worker writes straight to the device outside every lock,
  and shutdown freed the USB handle once the reader thread had stopped — without
  checking whether that worker had. A worker still inside a native write then had
  its handle freed underneath it, which is a use-after-free in C: the process
  dies instead of disconnecting cleanly. Shutdown now confirms every thread that
  can touch the handle has actually stopped, and holds the handle back if one
  has not.
- **Quitting is no longer stuck behind the heartbeat.** The keep-alive worker
  slept in ten-second blocks, so it never noticed the request to stop and every
  shutdown waited out a two-second timeout on the UI thread — the same two
  seconds also blocked the hotplug listener on every unplug. It now wakes
  immediately: measured on hardware, that join went from a guaranteed 2.00s to
  0.00s.

  Quitting is also far more **predictable**, which matters more than the
  average. Over three start-and-quit cycles on the same machine, the old code
  took 30.0s (hanging, and reporting failure), 5.7s and 2.9s; the fixed code
  took 2.9s, 3.1s and 2.8s, succeeding every time. Roughly two seconds of that
  remain and are being tracked separately in `docs/KNOWN-ISSUES.md`.

## [0.10.1] - 2026-07-21

Fixes two regressions introduced by 0.10.0's own hardening work, found in a
follow-up audit of that release.

### Fixed
- **A config with no profiles is no longer treated as corrupt.** 0.10.0 added a
  structural check so that a file which parses but is not a deck config gets
  preserved instead of silently replaced. That check was the one backing the
  *import* dialog, which requires a non-empty profile list — reasonable when
  the user picks a file by hand, wrong on load, because the loader has always
  turned an empty list into a working default. The effect was that such a
  config was moved aside to `config.json.corrupt` and the brightness, glow and
  dismissed-hint settings alongside it were reset to defaults. The file was
  preserved, so nothing was destroyed, but the settings were lost. Load now
  uses its own weaker check that accepts an empty profile list while still
  requiring the `profiles` key, which is what catches the case the check was
  added for.
- **The window no longer fails to open silently when the background service is
  running.** 0.10.0 made headless mode take the single-instance lock, so the
  background service and the GUI can no longer both drive the deck. That made
  "service enabled, then click the app icon" a normal path into the
  already-running branch, which only wrote to stderr — and a launch from the
  desktop entry sends stderr to the journal, so clicking the icon appeared to
  do nothing. It now shows a dialog naming the command that frees the deck.

## [0.10.0] - 2026-07-21

### Fixed
- **The app no longer burns a CPU core doing nothing.** The vendored SDK's
  hotplug listener re-enumerated the entire USB HID bus on every idle
  second as a safety net behind pyudev. Each scan costs about 105 ms, which
  measured as **6.7% of a CPU core burned continuously** by an idle app
  (4456 CPU seconds over an 18 hour run on the dev machine). The redundant
  rescan is throttled to once a minute; pyudev events still arrive
  instantly, so hotplug is unaffected. Measured after the fix on the same
  machine: **0.73% of a core, roughly 9x less**. Laptop users get the
  battery life back.
- **The key grid no longer drifts apart on a tall window.** The deck panel
  was stretched to fill the whole central area and the grid handed the slack
  to its rows, so on a maximised window the rows of keys sat in separate
  bands with large empty gaps between them. The panel now keeps its natural
  size and is centred instead.
- **Deselecting a key now clears the Key settings panel.** The header
  switched to "No key selected" but the label, icon, colours and both action
  dropdowns kept showing the last key's settings, so the panel read as though
  that key were still selected. Every field is reset now.
- **The action catalog no longer stays highlighted after a drag.** The
  dragged row kept its selection highlight after the drop, which read as the
  selected key's action even when no key was selected.
- **A hand-edited config can no longer be silently thrown away.** Config
  loading only ever caught JSON *syntax* errors: a file that parsed but had
  the wrong shape (a mistyped top-level key, a top-level list, some other
  app's JSON) loaded as one empty "Default" profile, left no `.corrupt`
  backup, and was then overwritten by the first autosave 600 ms later — the
  user's whole layout gone with nothing to restore from. Such files now take
  the same preserve-and-restart path as unparseable ones.
- **A config from a newer version is backed up before being downgraded.**
  Opening it with an older build strips every setting that build does not
  know and writes the result back under the *newer* version number, so the
  newer build cannot tell it was downgraded. A copy is now kept as
  `config.json.v<N>` first. The usual way in is syncing the config between
  two machines on different versions.
- **The headless service and the GUI can no longer both drive the deck.**
  Headless mode took no single-instance lock, so running the shipped systemd
  user service alongside the GUI opened the device twice. Linux delivers key
  reports to every open reader, so one physical press fired its action twice
  (a shell command ran twice, a page-switch skipped two pages) and the two
  instances repainted the LCDs over each other.
- **Key buttons no longer stay highlighted after a page or profile switch.**
  The grid kept drawing a key as selected while the settings panel said "No
  key selected" — the same contradiction as above, one widget over.
- **Dropping an action can no longer land on the wrong page.** Dragging runs
  a nested event loop, so a page switch coming from the deck arrives
  mid-drag. Key *moves* were already guarded against this; catalog drops were
  not, so aiming at a key on one page could overwrite that key on another —
  replacing its action while keeping its old label, then autosaving.
- **An icon picked while the page changes underneath is no longer silently
  discarded.** It was applied to nothing and left sitting in an otherwise
  blank panel. The app now says so.
- **The brightness slider follows the deck.** Changing brightness from a deck
  key left the slider stale, so the next nudge of it slammed the device back
  to the old value. Brightness set from the deck is also saved now, instead
  of being lost on restart.
- **A failing monitor key no longer floods the log.** A key pointed at a
  missing mount or an absent sensor logged a warning on every sample, which
  at the 0.5 s floor is about 172,800 identical lines a day, indefinitely.
  Each distinct failure is logged once.
- **A failed device setup no longer leaks.** Any failure after the device was
  opened returned without closing it, stranding an open handle and two live
  threads for the lifetime of the app, once per attempt.

### Packaging
- `release.sh` now stages the whole tree. It staged a fixed list of version
  files, and `git add` on an already-clean path is a silent no-op — so a fix
  sitting uncommitted stayed uncommitted while the tag captured the changelog
  entry claiming it, and the release CI built the published .deb from exactly
  that tag.
- `install.sh` rebuilds when `dist/` holds a .deb for a different version.
  It only built when `dist/` was empty, so `git pull && ./install.sh` in a
  clone that had ever been built silently reinstalled the old package and
  reported success.
- The udev rule ships to `/usr/lib/udev/rules.d` instead of
  `/lib/udev/rules.d`. On a merged-`/usr` system `/lib` is a symlink into
  `/usr/lib`, so the old path was an aliased location: DEP-17 forbids it and
  lintian raised `aliased-location` on every PPA upload. Every systemd-era
  udev reads the new path. Upgrading across the move was tested on a real
  merged-`/usr` install: the rule survives and device access is unaffected.
- The release workflow validates the AppStream and desktop metadata it
  publishes. Those checks lived only in the branch-push workflow, so a
  tag-only push or a re-tag could publish a .deb whose `metainfo.xml` nothing
  had validated.

## [0.9.0] - 2026-07-20
Packaging and store-submission work; no functional changes for deb/PPA users.

## [0.8.2] - 2026-07-20
Hardening release: every finding from a 13-point adversarial audit of 0.8.1,
fixed with a regression test each. None of these affect normal day-to-day use;
they cover data safety, crash recovery, and multi-instance edge cases.

### Fixed
- **Deleting the currently-viewed first page no longer loses later edits.**
  The key/knob editors stayed bound to the deleted page's objects, so
  everything typed afterwards was silently discarded on restart.
- **Config saves are now crash-durable.** The config is fsync'd before the
  atomic rename, so a power cut can no longer leave an empty `config.json`
  (all profiles lost). The corrupt-config recovery also preserves the corpse
  as `config.json.corrupt` instead of overwriting `config.json.bak`, the
  import flow's backup of a known-good config.
- **A hand-edited config can no longer crash the app on every launch.**
  Wrong-typed scalars (`"icon": null`, an unquoted color) used to load
  structurally and then crash the GUI at startup, bypassing the recovery
  path. They are now coerced to defaults; the rest of the config survives.
- **Wrong-GPU pinning on hybrid machines, the init-failure half.** If the
  NVIDIA driver was still loading at login, one failed `nvmlInit()` made the
  GPU temp/VRAM/load keys silently cache the AMD iGPU sensor forever. The
  probes now detect NVIDIA hardware via PCI sysfs (driver-independent) and
  keep retrying, bounded, then settle on the best remaining source.
- **Exported configs are private (0600).** The export can contain a
  plaintext password (the no-keyring fallback); it was written world-readable
  while the live config is carefully kept 0600.
- **Single instance is now an atomic claim.** The IPC socket moved to
  `XDG_RUNTIME_DIR` (another local user could squat the predictable /tmp
  name and silently swallow every launch), and instance ownership is a
  flock: two racing launches can no longer both come up live and clobber
  each other's config saves. `--quit` and hand-off still reach a pre-0.8.2
  instance across an upgrade.
- **SIGTERM (logout, service stop) now runs the orderly shutdown**: the
  pending debounced edit is saved and the deck is cleared; previously the
  process died instantly, losing both.
- **`--enable/--disable-autostart` while the app is running** is delegated
  to the running instance, so its menu toggle and the saved state stay in
  sync (the CLI's change used to be overwritten by the GUI's next autosave).
- **Hotplug racing a manual reconnect can no longer double-open the deck**
  (which dispatched every keypress twice and leaked a zombie device handle).
- **The color picker no longer leaks a dialog per pick**, and an unknown
  action type from a newer build's config no longer pollutes the action
  dropdown of every other key edited afterwards.

## [0.8.1] - 2026-07-19
### Fixed
- **Color picker readable in dark mode.** The dialog could open as the
  platform's native chooser — a white window unreadable against the app's
  themed text. The app now always builds Qt's own dialog with the dark
  stylesheet applied directly to it (verified on GNOME/Wayland hardware).
- **`--quit` waits for the instance to actually exit** (up to 10 s). It used
  to return immediately while the old process was still shutting down, so
  "quit && relaunch" raced and the relaunch deferred to the dying instance —
  leaving stale code running.

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
