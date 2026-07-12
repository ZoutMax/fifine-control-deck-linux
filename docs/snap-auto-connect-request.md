# Snap Store auto-connect request (draft)

The snap uses the `raw-usb` and `hardware-observe` interfaces to talk to the
device. These are **not** auto-connected by default, so after `snap install`
users must run:

```bash
sudo snap connect fifine-control-deck:raw-usb
sudo snap connect fifine-control-deck:hardware-observe
```

To make the snap work out-of-the-box, request auto-connection from Canonical.

## Where to post

<https://forum.snapcraft.io/c/store-requests/19> → **+ New Topic**
(log in with the Ubuntu One / SSO account first; new forum accounts need manual
approval before they can post).

## Draft to paste

**Title:**

```
Auto-connect request: raw-usb + hardware-observe for fifine-control-deck
```

**Body:**

```
Requesting auto-connection of the raw-usb and hardware-observe plugs for the
fifine-control-deck snap (publisher: zoutmax, strict confinement).

- What it is: a native Linux control app for the fifine Control Deck — a
  Mirabox / Hotspot "Stream Dock" style USB macro keypad with per-key LCDs
  (USB 3142:0060). It draws per-key icons/labels and reads key presses directly
  over USB.
- Why auto-connect: the app's sole purpose is to communicate with the USB
  keypad. Without raw-usb auto-connected the device is inert until the user
  manually runs `snap connect` — a poor first-run experience. hardware-observe
  is used to enumerate / identify the device.
- Source: https://github.com/ZoutMax/fifine-control-deck-linux

Thanks!
```

## Status

- [ ] Forum account approved
- [ ] Request posted
- [ ] Auto-connection granted by Canonical
