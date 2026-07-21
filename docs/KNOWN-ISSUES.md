# Known issues

Open defects carried forward from the pre-0.10.0 audits. Everything here was
found by reading the code and, where noted, reproducing the behaviour. None of
it is fixed yet.

Line numbers are **as of v0.10.0** (commit `aacf2eb`) and have shifted in files
touched since. Treat them as a pointer, not an address.

Nothing in this file is a regression introduced by 0.10.0 or 0.10.1. These are
long-standing behaviours that predate both.

---

## Device layer and the vendored SDK

**Status:** issues 2 and 3 are fixed and verified on hardware. The rest are open.

The items below live in or around `fifine_deck/backend/StreamDock/`, which
is the vendored MiraboxSpace SDK — code we ship but did not write. Fixing them
means patching a third party's threading, so each one needs real replug-cycle
and quit-timing testing against physical hardware before it can be trusted. That
is why they were held back from 0.10.0 rather than rushed in on release day.

### 1. A replug completing inside ~0.6 s wedges the deck permanently

`DeviceManager.py:295-301`, `DeviceManager.py:174`, `controller.py:179`

On a udev `remove`, `_remove_missing_devices` retries only 3 times over 0.6 s and
reconciles by **path string**. If the device is back before those retries finish,
enumeration still finds `/dev/hidraw0`, so nothing is removed. The following
`add` then hits the `_device_exists(path)` guard, finds the stale dead device
object still in `streamdocks`, and returns without announcing anything.

`DeckController._on_removed` never fires, so `self.device` keeps pointing at a
StreamDock whose transport handle refers to a torn-down node. Every later write
is silently swallowed, the status bar still reads `● connected fw=…`, and no key
works. The 60 s idle rescan does the same path comparison and also finds nothing
to fix, so there is no recovery short of restarting the app or unplugging for
more than a second.

Reachable from a wobbly USB-C connector or a hub that re-enumerates.

### 2. `transport_destroy` can free a handle while the GIF worker is writing through it — **FIXED**

> Fixed after 0.10.1. `GifController.close()` now returns whether its worker
> actually exited, and `StreamDock.close()` defers `transport.close()` unless
> **every** thread that can be inside a native call on the handle — reader, GIF
> worker and heartbeat — has stopped. Deferring leaks one handle and its threads
> for the process lifetime, which is strictly better than a use-after-free that
> kills the process, and it now says which threads held it back.
> Covered by `tests/test_sdk_shutdown.py`.

The original problem, for reference:


`StreamDock.py:266-275`, `GifController.py:236-237`, `GifController.py:473-481`,
`LibUSBHIDAPI.py:1231-1240`, `LibUSBHIDAPI.py:628-633`

The transport destroy is gated on `read_thread_alive` only. The GIF worker's join
is not: `GifController.close()` joins with `timeout=2.0` and ignores the result,
while that worker writes to the device outside every lock. Every transport method
is an unsynchronised check-then-use of `self._handle`.

Pull the cable while an animated key is playing: the in-flight
`set_key_image_stream` blocks in libusb waiting for its transfer timeout, the
join gives up after 2 s, and the handle is freed underneath it. That is a
use-after-free in C, so the process dies instead of disconnecting cleanly.

**This is the one to fix first.** It is the only item here that can take the
whole process down.

### 3. Every quit blocks ~2 s in the heartbeat join — **FIXED**

> Fixed after 0.10.1. The worker now waits on a `threading.Event` instead of
> `time.sleep`, so a stop request wakes it at once; `close()` sets that event,
> checks the join result, and treats a still-live heartbeat as a reason to defer
> the transport destroy (see issue 2). The unbounded `join()` when restarting the
> heartbeat is bounded too.
>
> Measured on real hardware, phase by phase, after the fix:
> `gif_controller.close()` 0.00 s, **heartbeat join 0.00 s (was a guaranteed
> 2.0 s)**, read thread join 0.09 s. End to end, `--quit` went from exceeding its
> 10 s deadline and reporting failure, to 6.34 s and reporting success.
>
> **~2 s of the remaining shutdown is elsewhere** — see issue 9 below, which the
> same measurement turned up.

The original problem, for reference:


`StreamDock.py:599-606`, `StreamDock.py:228-236`, `controller.py:248`,
`main_window.py:876`, `LibUSBHIDAPI.py:886-890`

`_heartbeat_worker` sleeps 10 s between beats, and `close()` joins it with
`timeout=2.0` while ignoring whether the join succeeded. The thread is inside
`time.sleep(10)` essentially always, so the join always times out.

Guaranteed: Options → Quit (or Ctrl+Q) freezes the window for two seconds before
it closes, because `DeckController.stop()` runs on the Qt thread. The same 2 s
blocks the udev listener thread on every unplug.

Narrower race: if the heartbeat passes its `if not self._handle` check just as
`close()` frees the handle, `transport_heartbeat` runs on freed memory.

The fix is small and self-contained — replace the sleep with an interruptible
`threading.Event.wait()` — which makes this the best value-for-risk item here.

### 4. The throttled rescan is idle-gated, not time-gated

`DeviceManager.py:225-229`, `DeviceManager.py:303-307`, `DeviceManager.py:208-210`,
`controller.py:141-149`

`monitor.poll(timeout=60)` returns for **any** uevent on the `usb` subsystem, and
the rescan runs only on the timeout branch. So the safety net does not run "once
a minute" as its comment and `docs/PROVENANCE.md` claim — it runs only after 60
consecutive seconds with zero USB uevents of any kind. On a machine with a
webcam, a dock or a phone attached, that window may never arrive.

This matters because the add path can legitimately give up: `_add_missing_devices`
retries only 10 times at 0.2 s. A deck whose hidraw node is not ready inside that
~3 s window is dropped, and the fallback that used to catch it a second later may
now never fire.

The throttle itself is correct; the trigger should be a monotonic
"last full rescan" timestamp rather than "poll returned nothing".

Related, same function: `pyudev.Context()` / `Monitor.from_netlink()` sit outside
the loop's `try`, so a failure there (no netlink access in a container or a
confined session) propagates into `DeckController._listen`, which logs once and
lets the thread die. `_fallback_polling` only covers `ImportError`, so hotplug is
dead for the rest of the session with no retry.

### 5. Fixing udev permissions while the app runs never reconnects

`DeviceManager.py:43-55`, `DeviceManager.py:174`, `DeviceManager.py:292-293`,
`controller.py:187-195`, `main_window.py:816`, `actions.py:456-457`

A device that failed to open stays cached forever. `enumerate()` appends the
device object regardless of whether the open succeeded, `_add_missing_devices`
skips any path already in `streamdocks`, and `_handle_device_event` ignores every
action except `add`/`remove` — while `udevadm trigger`, which the README and our
own snap hint tell the user to run, emits `change`.

So: start the app before installing the udev rule, see "no device", install the
rule and reload udev, and the app never retries. The only in-app reconnect is
`try_open()`, reachable solely from the snap hint, and `snap_usb_hint()` returns
`None` outside a snap — so a .deb, PPA or source user has no reconnect path at
all. The "unplug and replug" line in the docs is what saves this today.

### 6. A permission failure looks identical to "no deck", and a half-open handle reports as connected

`controller.py:190`, `controller.py:125`, `main_window.py:772-776`

The open failure is logged at WARNING to stderr only; the GUI shows the same
`○ no device` as an unplugged deck. Conversely `_setup_device` returns True even
when `dev.firmware_version` is empty — the "libusb false-connect" state that
`try_open` explicitly rejects. On that path the status bar reads `● connected fw=`
with every key dead and no retry anywhere.

### 7. GIF decoding runs on the Qt thread while holding the controller lock

`controller.py:325`, `controller.py:309`, `GifController.py:262-306`

`set_key_gif` decodes and JPEG-encodes **every frame** on the calling thread with
no caching, redone in full on each render, and `render_key`/`render_page` are
called directly from the GUI thread in nine places.

Measured: 90 frames of a 400×400 GIF is 0.25 s of PIL work, scaling linearly with
size and length and multiplying with several animated keys on a page. Every page
switch, profile switch, folder enter/exit and reconnect freezes the window for
that long. Because `_lock` is held throughout, the SDK reader thread also blocks,
so key presses landing during a page switch are dispatched late by the same
amount.

### 8. Lower severity, same area

- **Double close / double free.** Unplugging during quit lets `controller.stop()`
  and `_remove_device_by_path` call `close()` on the same object concurrently;
  neither `StreamDock.close` nor `LibUSBHIDAPI.close` takes a lock, so both can
  read a non-`None` handle and both call `transport_destroy`. Also, `stop()`
  always passes `notify=True`, so a deck removed without the udev event landing
  gets a disconnect packet written to a gone device — which the SDK's own comment
  warns "may terminate the process".
- **A stale frame can survive quit.** `stop()` calls `stop_gif_loop()` then
  `clearAllIcon()`, but the GIF worker collects its batch under the lock and
  writes after releasing it, so one frame can be painted after the clear and
  stay lit on the physical deck.
- **Write results are never checked.** `set_key_image_stream` returns a
  `TransportResult` that `set_key_image_pil` passes back and `render_key`
  discards. Nothing distinguishes "written" from "swallowed by a dead handle",
  which is what makes issues 1 and 6 invisible to the user.

### 9. ~2 s of every shutdown is spent inside `controller.stop()`

`controller.py` (`stop()`), and what it calls: `stop_gif_loop()`, `clearAllIcon()`

Found by profiling the fix for issue 3, so unlike the rest of this file it is a
**measurement, not a reading**. With the SDK's thread joins now effectively free,
the phase breakdown of a real shutdown on hardware is:

| phase | time |
|---|---|
| `gif_controller.close()` | 0.00 s |
| heartbeat join | 0.00 s |
| read thread join | 0.09 s |
| **rest of `controller.stop()`** | **2.05 s** |

That 2 s is the dominant remaining cost, and it runs on the Qt thread from
`MainWindow._quit`, so it is a visible freeze on every quit. The likely candidate
is `clearAllIcon()` writing all 15 key images over USB one at a time, plus the
monitor sampler shutdown, but that has **not** been confirmed — the profile
above only narrows it to "inside `stop()`". Profile it further before changing
anything.

Worth noting that the end-to-end `--quit` figure (6.34 s) is larger than the sum
here, so the IPC round trip and Qt teardown carry cost of their own that has not
been attributed yet.

---

## Application layer

### Typing does nothing, silently, when no keystroke tool is installed

`actions.py:296-298`

`_type_text` returns silently when no keystroke tool is present, while the
parallel `_send_hotkey` path logs `no keystroke tool (install xdotool / ydotool /
wtype)`. A "Type text" or "Type password" key on a box without any of those does
nothing at all, with no log line and no GUI feedback.

### A locked keyring types an empty password with no indication

`actions.py:382-386`, `secret_store.py:60-62`

`secret_store.get` returns `None` both for "keyring missing" and "keyring
locked", and the warning only fires for a raised error, not a `None` return. With
the login keyring locked, pressing the key types an empty string and the user is
never told the secret was unavailable.

### Config export writes a plaintext password with no warning

`widgets.py:542`, `main_window.py:123-142`

When no keyring is available, the password is stored in `params["password"]` and
`Action.to_dict` copies params verbatim, so **Options → Export config** writes the
secret in the clear. The export is `0600`, which protects other local users, but
the whole point of an export is to move it to another machine or a backup, where
that mode does not follow it. No warning is shown at export time.

### `--enable-autostart` can report success without changing anything

`app.py:354-363`, `main_window.py:103`

The flags delegate to a running instance and print success as soon as the bytes
are written, without confirming. The receiving side calls
`autostart_act.setChecked(...)`, which emits `toggled` — and therefore writes or
removes the `.desktop` — only if the state actually changes; that state is a
snapshot taken once at window construction and never refreshed. If the file was
removed behind the GUI's back, `--enable-autostart` prints
`Signalled the running instance to update autostart.`, exits 0, and autostart
stays off.

---

## Packaging

### The vendored transport `.so` is unstripped

`lintian` reports `unstripped-binary-or-object` on
`fifine_deck/backend/StreamDock/Transport/TransportDLL/libtransport.so` for every
build. It is a prebuilt third-party binary, so stripping it is a decision about
someone else's artifact rather than a straightforward fix. Not an upload blocker.

### Launchpad references outliving the mirror

The Launchpad git mirror was removed from `release.sh` after v0.10.0 (SSH still
authenticates but the repository read fails, and the mirror sat 16 commits
behind). `README.md`, `docs/index.html`, `docs/SNAP.md`, `docs/PROVENANCE.md` and
`docs/PPA.md` still mention Launchpad. Most of those refer to the **PPA**, which
is live and correct and is fed by `dput`, not by the git mirror — so they were
left alone deliberately. They need a pass once it is settled whether the
Launchpad repository is dead or merely moved.

---

## Verification status

Items 1-8 were each read out of the source and reasoned through, and several
were checked against the live process's open file descriptors and threads. They
have **not** been reproduced against physical hardware, because doing so means
deliberately provoking disconnects and use-after-free on a real device. Confirm
each on hardware before writing a fix, and treat the severity ordering here as a
starting point rather than a measurement.
