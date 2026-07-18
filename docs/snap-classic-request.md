# Snap Store classic confinement request

Classic confinement needs Canonical approval, and that approval process is
driven by a forum topic — nothing happens without one. Post it at:

<https://forum.snapcraft.io/c/store-requests/19> → **+ New Topic**
(log in with the Ubuntu One / SSO account; brand-new forum accounts need
manual approval before their first post appears.)

## Title

```
Classic confinement request: fifine-control-deck
```

## Body

```
I'm requesting classic confinement for the fifine-control-deck snap
(publisher: zoutmax).

What it is: a native Linux control application for the FiFine AmpliGame D6 /
"Stream Dock" family of USB macro keypads with per-key LCD screens
(USB 3142:0060). It renders per-key icons on the device and executes
user-configured actions on key presses. Free and open source (GPL):
https://github.com/ZoutMax/fifine-control-deck-linux

Why classic: the device is driven over /dev/hidraw with a vendor HID
protocol (via hidapi's hidraw backend). Strict confinement cannot grant
hidraw access for this device class — the raw-usb interface does not cover
hidraw nodes, and there is no other interface that does. We shipped a strict
build first (revisions up to 0.5.6) and it cannot open the device on any
system; classic is the only route that makes the snap functional. This
matches the established precedent for hardware-control apps in the store.

Additionally, the app's purpose is to run user-defined commands and
hotkeys on the host (a macro keypad), which is inherently un-confinable —
the same category as other device-control/automation snaps granted classic.

The classic build is ready and auto-builds from GitHub; we will release it
as soon as classic is granted. Thanks!
```

## Status

- [x] Forum account approved (zoutmax)
- [x] Request posted 2026-07-18: https://forum.snapcraft.io/t/classic-confinement-request-fifine-control-deck/52368
- [ ] Reviewer follow-ups answered
- [ ] Classic granted → upload/release 0.7.0 classic build
