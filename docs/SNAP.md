# Packaging as a Snap (Ubuntu App Center)

The Ubuntu App Center surfaces the **Snap Store**, so publishing there means
building a snap and uploading it. This directory (`snap/`) contains the
packaging.

## Build

Snapcraft needs a build backend (LXD is recommended):

```bash
sudo snap install snapcraft --classic
sudo snap install lxd && sudo lxd init --auto
sudo usermod -aG lxd "$USER"   # then re-login

cd fifine-control-deck-linux
snapcraft            # builds ./fifine-control-deck_0.5.2_amd64.snap
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

## ⚠️ Device access — the one real constraint

The deck is driven through **`/dev/hidraw*`** (the bundled `libtransport.so`
uses hidraw via libudev). Under a **strict** snap:

- `raw-usb` grants USB access via **usbfs (`/dev/bus/usb`)**, which libusb-based
  code uses — but our transport uses **hidraw**, and there is no general
  auto-connecting `hidraw` interface for arbitrary devices on a classic Ubuntu
  desktop. So a strict snap may **fail to open the device**.
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

1. Build the **strict** snap here and test device access on your machine.
2. If hidraw access is blocked (likely), switch `confinement: classic` in
   `snap/snapcraft.yaml`, rebuild, and request classic approval when uploading.
3. A **Launchpad PPA** (apt) remains the simplest full-featured Ubuntu channel
   if the store review is a blocker.
