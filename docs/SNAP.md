# Packaging as a Snap (Ubuntu App Center)

> **Status (2026-07): parked, nothing published.** The deck needs
> `/dev/hidraw`, which strict confinement cannot grant, so the working build is
> a **classic** one — and classic confinement requires Canonical approval. The
> request ([forum topic 52368](https://forum.snapcraft.io/t/classic-confinement-request-fifine-control-deck/52368))
> was declined for now: a device-control utility is not on the supported
> classic categories, and the project was judged too young. The old strict
> revisions are unpublished (the store page 404s) because they could not drive
> the deck at all. The name stays registered; revisit once the project has a
> longer track record. Until then, install via the PPA or the `.deb`.
> See also [`snap-classic-request.md`](snap-classic-request.md).

The Ubuntu App Center surfaces the **Snap Store**, so publishing there means
building a snap and uploading it. This directory (`snap/`) contains the
packaging.

## Build

Snapcraft needs a build backend (LXD is recommended):

```bash
sudo snap install snapcraft --classic
sudo snap install lxd && sudo lxd init --auto
sudo usermod -aG lxd "$USER"   # then re-login

cd FifineControlDeck
snapcraft            # builds ./fifine-control-deck_<version>_amd64.snap
```

Test locally before uploading:

```bash
sudo snap install ./fifine-control-deck_*.snap --dangerous
# connect the USB interfaces (needed for device access):
sudo snap connect fifine-control-deck:raw-usb
sudo snap connect fifine-control-deck:hardware-observe
fifine-control-deck
```

## Publish to the Store / App Center

```bash
snapcraft login                       # your Ubuntu One account
snapcraft register fifine-control-deck  # once; name must be free
snapcraft upload --release=edge ./fifine-control-deck_*.snap
# promote when happy: beta -> candidate -> stable
```

Once on `stable`, it appears in the Ubuntu **App Center** / Snap Store.

## Build result (verified 2026-07-13, v0.5.6 from `stable`)

The strict snap **builds, installs, and launches**, and with `raw-usb` +
`hardware-observe` connected it **enumerates** the deck (`keys=15`) — but it
**cannot drive it**. The firmware read comes back empty
(`connected: fw='' keys=15`) and key presses do nothing. A working deck logs a
populated firmware string (`fw='V3.D6.1.009'`); an empty **`fw=''` is the
tell-tale that the snap is running and is blocked from device I/O.**

**Root cause (confirmed):** the bundled `libtransport.so` uses hidapi's
**HIDRAW** backend — verified with `strings` (`"… not a HIDRAW device?"`,
`hidraw`) and `ldd` (links `libudev`, **not** `libusb`). It opens `/dev/hidraw*`
directly. Strict confinement's `raw-usb` grants only `/dev/bus/usb/**`
(usbfs/libusb), and there is **no** general interface to give a desktop snap
arbitrary `/dev/hidraw*` access. So **strict confinement does not work for this
device — full stop.**

> An earlier version of this note claimed strict was "viable / firmware read OK".
> That was a misdiagnosis: the test machine had the **`.deb`** installed, whose
> `/usr/bin/fifine-control-deck` shadows the snap in `PATH` — so the deb, not the
> snap, was what opened the device. Run `hash -r` after (un)installing either.

## ⚠️ Device access — the one real constraint

The deck is driven through **`/dev/hidraw*`** (the bundled `libtransport.so`
uses hidraw via libudev). Under a **strict** snap:

- `raw-usb` grants USB access via **usbfs (`/dev/bus/usb`)**, which libusb-based
  code uses — but our transport uses **hidraw**, and there is no general
  auto-connecting `hidraw` interface for arbitrary devices on a classic Ubuntu
  desktop. So a strict snap **enumerates the device but cannot do I/O** (the
  `/dev/hidraw*` open for read/write is blocked) — confirmed above.
- **Realistic fix:** ship a **classic**-confinement snap (`confinement: classic`)
  for full `/dev/hidraw` access. Classic snaps require **manual review** by the
  Snap Store team before they can be released (open a request on the
  snapcraft forum). This is the common route for hardware-control apps.

Also note under strict confinement:
- **Hotkey / type-text** (ydotool → `/dev/uinput`) will not work — document it
  as a limitation, or provide it only in the classic build.
- **Autostart** and config live under `~/snap/fifine-control-deck/…` instead of
  `~/.config`.

## Recommendation

`snap/snapcraft.yaml` is now the **classic** build — verified 2026-07-13 to
fully drive the deck (firmware read + key events), which the strict snap could
not. Strict is a dead end for this hidraw device; classic is what we ship.

1. **Classic Snap** (`confinement: classic`, no gnome extension) — drives the
   deck for real. Two caveats remain: classic snaps need **manual Snap Store
   review** before release (open a request on the snapcraft forum), and a snap
   **cannot install a udev rule**, so the host still needs `70-fifine-deck.rules`
   (plugdev on VID `3142`) for `/dev/hidraw*` access.
2. **`.deb` / Launchpad PPA (apt)** — the zero-friction channel: bundles the
   udev rule and drives the deck out of the box (`ppa:zoutmax/fifine`).

### How the classic build works (the non-obvious parts)

- **Bundle the interpreter yourself.** core24's base ships `python3.12`, so
  snapcraft prunes it from the payload — fine for strict (uses the base's Python
  at runtime), fatal for classic (no base at runtime). `override-build` copies
  `python3.12` + stdlib + `libpython3.12` into the payload **before**
  `craftctl default` (into `usr/`, so the plugin's venv in `bin/` doesn't
  collide, and its relocation fixup then finds the interpreter).
- **No gnome extension** (it is strict-only). The launch wrapper points Qt at
  the bundled Qt6 from the PyQt6 wheel (`QT_QPA_PLATFORM_PLUGIN_PATH`,
  `LD_LIBRARY_PATH`) and the part stage-packages the xcb/GL/font libraries the
  Qt platform plugin needs.
- **Arch-aware** override-build (`CRAFT_ARCH_TRIPLET_BUILD_FOR`) and wrapper, so
  amd64 + arm64 both build; the other-arch `libtransport` blob is stripped.

Result: ~168 MB snap (bundles Python + Qt6 + libs), installs with
`snap install --classic`, and opens `/dev/hidraw0` like the deb.
