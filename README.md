# Fifine Control Deck — Linux

A native Linux control application for the **fifine Control Deck** (a Mirabox /
Hotspot "Stream Dock" style macro keypad with per-key LCDs, USB id
`3142:0060` "HOTSPOTEKUSB"). It is a from-scratch reimplementation of the
Windows software's core: it draws icons/labels on the keys, reacts to key
presses, and runs actions (launch apps, hotkeys, media/volume, scripts…),
with profiles, multiple pages, a configuration GUI, and a system tray.

> The Windows app is a closed-source Qt5 + Chromium (CEF) bundle. This project
> reuses only the vendor's **MIT-licensed** `StreamDock` device backend (the
> `libtransport.so` USB layer) and builds an original Python/PyQt6 app on top.

## Features

- Live per-key icons + text labels, colour backgrounds, custom images.
- Actions: launch app, run shell command, open URL/file, send hotkey, type
  text, media control, volume up/down/mute, brightness, switch page/profile,
  and multi-step actions.
- Multiple **profiles**, each with multiple **pages** (bind a key to
  next/prev/goto-page to build folders).
- Configuration GUI with a live grid that mirrors the device, plus a system
  tray; window close minimises to tray, daemon keeps running.
- Optional headless daemon mode + systemd user service for autostart.
- Hotplug aware (unplug/replug re-applies the current page).

## Requirements

Already present on most desktops / this machine:

- Python 3.10+
- **PyQt6** and **Pillow** (system packages: `python3-pyqt6`, `python3-pil`)

Optional, for specific actions (install what you use):

| Action            | Needs                                            |
|-------------------|--------------------------------------------------|
| Volume            | PipeWire (`wpctl`) or PulseAudio (`pactl`)       |
| Media play/pause  | `playerctl`                                      |
| Hotkey / type text| `ydotool` (Wayland) or `xdotool` (X11) / `wtype` |
| Open URL/file     | `xdg-open` (`xdg-utils`)                         |

The status bar shows what was detected on your session.

> **ydotool note (Wayland):** hotkey/type actions use `ydotool`, which needs
> its daemon running with access to `/dev/uinput`:
> ```bash
> sudo modprobe uinput
> sudo ydotoold           # or run as a service; creates /tmp/.ydotool_socket
> ```
> On X11, `xdotool` works with no daemon. `playerctl` (media) and `wpctl`
> (volume) need no daemon.

## Install (.deb — recommended)

Download the `.deb` from the
[latest release](https://github.com/ZoutMax/fifine-control-deck-linux/releases)
and install it like any normal application:

```bash
sudo apt install ./fifine-control-deck_*_amd64.deb
```

This installs the app, a desktop launcher (**fifine Control Deck** appears in
your app menu), the icon, and the udev rule. Make sure you're in the `plugdev`
group, then unplug/replug the device once:

```bash
sudo usermod -aG plugdev "$USER"   # then log out/in if it was just added
```

To build the `.deb` yourself: `./packaging/build-deb.sh` → `dist/`.

## Run from source (development)

1. **Install the udev rule** (one time, needs root) so the device is usable
   without `sudo`:

   ```bash
   sudo ./packaging/install-udev.sh
   ```

   Then **unplug and replug** the device. You must be in the `plugdev` group.

2. **Run the app:**

   ```bash
   ./run.sh
   ```

   or `python3 -m fifine_deck`. Use `--headless` for the daemon-only mode.

## Icons

The app ships a built-in icon library (`assets/icons/library/`) — pick icons in
the key editor via **Library…**, or load your own image with **File…**. Icons
are regenerated with `python3 tools/make_icons.py`.

## Autostart (optional)

```bash
mkdir -p ~/.config/systemd/user
cp packaging/fifine-deck.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fifine-deck.service
journalctl --user -u fifine-deck.service -f   # logs
```

## Configuration

Stored at `~/.config/fifine-control-deck/config.json`; imported icons live in
`~/.config/fifine-control-deck/icons/`. The GUI saves automatically.

## Device profile

The device geometry (key count, pixel size, image rotation, hardware key
mapping) lives in one place: `DEVICE_PROFILE` in `fifine_deck/device.py`.
`probe_device.py` confirms the correct values against your hardware.

## Project layout

```
fifine_deck/
  backend/StreamDock/   vendored MIT device SDK (+ libtransport.so)
  device.py             FifineDeck wrapper + DEVICE_PROFILE
  model.py              config data model (profiles/pages/keys/actions)
  actions.py            action engine + Linux environment detection
  rendering.py          key-image rendering (device + GUI preview)
  controller.py         runtime: device <-> config <-> actions, hotplug
  gui/                  PyQt6 GUI (grid, editor, profiles, pages, tray)
  app.py                entry point (GUI / --headless)
probe_device.py         one-off hardware profiler
packaging/              udev rule, installer, systemd unit
```

## Licensing

The vendored `backend/StreamDock/` directory is the MIT-licensed
`MiraboxSpace/StreamDock-Device-SDK` (see `backend/StreamDock/LICENSE.vendor`).
Application code in this repo is original.
