"""
Runtime controller: connects a FifineDeck to a DeckConfig and the action engine.

- Renders the active page onto the physical keys.
- Dispatches key presses to bound actions.
- Implements the ActionContext (page/profile/brightness operations).
- Handles hotplug so unplug/replug re-applies the current page.

GUI-agnostic: optional callbacks (on_connect / on_disconnect / on_key_event /
on_page_changed) let a GUI observe state without this module importing Qt.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import os
import queue

from . import actions, monitors, rendering
from .device import DEVICE_PROFILE, FifineDeck, register
from .model import DeckConfig, Profile, Page, KeyConfig

from StreamDock.DeviceManager import DeviceManager
from StreamDock.InputTypes import EventType

# A key held this long (seconds) counts as a long press. Keys WITHOUT a hold
# action are dispatched instantly on press-down, exactly as before 0.8.0 —
# the threshold only ever delays keys that define a second action.
HOLD_THRESHOLD = 0.5


class _PendingHold:
    """One in-flight press on a key that has a hold action. The `fired` flag
    is the race arbiter between the hold timer and the release event: whoever
    flips it under the lock owns the dispatch."""
    __slots__ = ("kc", "timer", "fired", "lock")

    def __init__(self, kc):
        self.kc = kc
        self.timer = None
        self.fired = False
        self.lock = threading.Lock()

log = logging.getLogger(__name__)


class DeckController:
    def __init__(self, config: DeckConfig):
        self.config = config
        self.manager: Optional[DeviceManager] = None
        self.device: Optional[FifineDeck] = None
        self.page_index = 0
        self._lock = threading.RLock()
        # Serializes the whole open path. try_open (GUI reconnect) racing the
        # hotplug listener's _on_added could otherwise open the SAME hidraw
        # node twice — two live transports, every keypress dispatched twice,
        # and the loser's handle leaking as a zombie reader. Separate from
        # _lock: opening a device is slow and _lock guards hot paths.
        self._open_lock = threading.Lock()
        self._listen_thread: Optional[threading.Thread] = None
        self._running = False
        self._gif_keys: set[int] = set()   # logical keys currently animated
        self._nav: list = []               # folder navigation stack
        self._container = None             # current Folder, or None at profile root

        # Actions run on a dedicated worker thread so a slow action (e.g. a
        # multi-action with delays, or a blocking command) never stalls the
        # SDK reader thread that delivers key events.
        self._action_queue: queue.Queue = queue.Queue()
        self._action_thread = threading.Thread(target=self._action_worker, daemon=True)
        self._action_thread.start()

        # Monitor keys are sampled + repainted on their own thread, so a slow
        # metric read (a stalling disk, a sick GPU driver) can never delay
        # actions or the SDK reader.
        self._sampler = monitors.Sampler()
        self._monitor_state: dict[int, tuple] = {}  # key index -> (last_t, signature)
        self._holds: dict[int, _PendingHold] = {}   # key index -> in-flight long-press
        self._monitor_stop = threading.Event()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        # observer callbacks (optional)
        self.on_connect: Optional[Callable[[FifineDeck], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_key_event: Optional[Callable[[int, bool], None]] = None
        self.on_page_changed: Optional[Callable[[], None]] = None
        self.on_monitor_image: Optional[Callable[[int, object, str], None]] = None
        # Fired when brightness is changed from the DECK (a brightness key), so
        # the GUI slider can follow. Without it the slider kept its stale value
        # and the next one-step nudge slammed the device back to it.
        self.on_brightness_changed: Optional[Callable[[int], None]] = None
        # Why the last open attempt failed, in words meant for the user, or ""
        # when there is nothing to explain. A deck that is plugged in but
        # unopenable (no udev rule) and a deck that is simply absent are very
        # different problems, and the status bar showed "○ no device" for both.
        self.last_error: str = ""
        # Consecutive key writes the device did not accept; see _note_write_result.
        self._write_failures: int = 0
        # Off-thread GIF decoding; see _gif_decode_ready / _decode_worker.
        self._decode_queue: "queue.Queue" = queue.Queue()
        self._decode_lock = threading.Lock()
        self._decode_pending: set = set()
        self._decode_failed: set = set()
        self._decode_thread: Optional[threading.Thread] = None

        register()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Enumerate + open the first device, then listen for hotplug."""
        self._running = True
        self.manager = DeviceManager()
        found = self.manager.enumerate()
        opened = False
        for dev in found:
            if isinstance(dev, FifineDeck):
                if self._setup_device(dev):
                    opened = True
                    break
        if self._decode_thread is None or not self._decode_thread.is_alive():
            self._decode_thread = threading.Thread(
                target=self._decode_worker, daemon=True, name="gif-decode")
            self._decode_thread.start()
        self._listen_thread = threading.Thread(target=self._listen, daemon=True)
        self._listen_thread.start()
        return opened

    def try_open(self) -> bool:
        """Re-enumerate and open the deck if not already connected.

        Safe to call at runtime — e.g. right after installing the udev rule,
        where `udevadm trigger` fires a 'change' (not 'add') event that the
        hotplug listener ignores. Does not spawn another listener thread.

        If we already hold a *functional* handle (firmware read) we keep it. A
        non-functional handle — the libusb false-connect with empty firmware
        that a locked-out snap gets before the rule is installed — is dropped
        and the device re-opened fresh, so keys work immediately without a
        relaunch.
        """
        with self._open_lock:
            if self.device is not None and self.device.firmware_version:
                return True
            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
                with self._lock:
                    self.device = None
            if self.manager is None:
                self.manager = DeviceManager()
            for dev in self.manager.enumerate():
                if isinstance(dev, FifineDeck) and self._setup_device(dev):
                    return True
            return False

    def _listen(self):
        try:
            self.manager.listen(
                on_device_added=self._on_added,
                on_device_removed=self._on_removed,
                on_device_changed=self._on_changed,
                auto_open=False,
            )
        except Exception as e:
            log.error("hotplug listener stopped: %s", e)

    def _on_changed(self, _dev=None):
        """A udev 'change' uevent arrived.

        This is what `udevadm trigger` emits — the command the README and the
        snap hint tell users to run right after installing the udev rule. It is
        NOT an 'add', so the hotplug path ignored it, and a device that had
        already failed to open stayed cached forever: the manager skips any path
        it already holds, so the documented fix could never take effect without
        restarting the app or physically replugging.

        try_open is exactly the recovery for this — it drops a non-functional
        handle and reopens — and it is a no-op when we already have a working
        one, so reacting to every change event is cheap.
        """
        if not self._running:
            return
        if self.device is not None and self.device.firmware_version:
            return                       # already working; nothing to recover
        try:
            if self.try_open():
                log.info("recovered device access after a udev change event")
        except Exception as e:
            log.debug("try_open after a change event failed: %s", e)

    def _on_added(self, dev):
        if not (self._running and isinstance(dev, FifineDeck)):
            return
        with self._open_lock:
            # Re-checked under the lock: a concurrent try_open may have just
            # opened this very hidraw node with its own device object.
            if self.device is None:
                self._setup_device(dev)

    def _cancel_holds(self) -> None:
        """Cancel every in-flight long-press. A lost release (unplug mid-hold,
        reconnect) must not leave stale entries: the duplicate-down guard
        would silently swallow the key's next genuine press, and an armed
        timer would fire the hold action against a gone device."""
        # popitem() atomically, rather than check-then-act: the reader thread
        # pops entries on release and other callers may cancel concurrently,
        # so a `while self._holds: popitem()` could race to an empty dict and
        # raise KeyError. Catching it is the clean end-of-loop signal.
        while True:
            try:
                _, pending = self._holds.popitem()
            except KeyError:
                break
            if pending.timer is not None:
                pending.timer.cancel()
            with pending.lock:
                pending.fired = True    # claim: a timer sliver must not dispatch

    def _on_removed(self, dev):
        if dev is self.device:
            self._cancel_holds()
            with self._lock:
                self.device = None
            if self.on_disconnect:
                self.on_disconnect()

    def _setup_device(self, dev: FifineDeck) -> bool:
        try:
            if not dev.open():
                log.warning("open() failed (permissions? udev rule installed?)")
                # Record WHY, so the GUI can say something better than the
                # "○ no device" it shows for an unplugged deck. A deck that is
                # physically present but unusable because the udev rule is not
                # installed is a completely different problem for the user, and
                # the two were indistinguishable.
                self.last_error = ("The deck is connected but could not be "
                                   "opened — this is almost always the udev "
                                   "rule missing, or you not being in the "
                                   "'plugdev' group.")
                from .actions import snap_usb_hint
                hint = snap_usb_hint()
                if hint:
                    log.warning("%s", hint)
                return False
            dev.init()
            # A handle with no firmware is the libusb "false connect": open()
            # succeeded but the device will not talk, so every key is dead.
            # try_open already rejects this state; _setup_device used to accept
            # it and report "● connected  fw=" with nothing working.
            if not dev.firmware_version:
                log.warning("device opened but returned no firmware — treating "
                            "as not connected (access is probably blocked)")
                self.last_error = ("The deck answered without identifying "
                                   "itself, so it cannot be driven. Check the "
                                   "udev rule, then replug it.")
                try:
                    dev.close()
                except Exception:
                    log.debug("close() after a false-connect failed", exc_info=True)
                return False
            self._cancel_holds()        # a replug starts with a clean slate
            with self._lock:
                self.device = dev
                # Do NOT reset page_index here: a hotplug replug would then
                # snap the user back to page 1 and, via on_page_changed, make
                # the GUI clear its editor and drop an open picker's result.
                # page_index defaults to 0 for the initial connect, and
                # render_page/current_page clamp it, so preserving it is safe.
            self.last_error = ""            # a working handle clears the reason
            dev.set_key_callback(self._key_callback)
            self.apply_brightness()
            self.render_page()
            if self.on_connect:
                self.on_connect(dev)
            log.info("connected: fw=%r keys=%s", dev.firmware_version, dev.KEY_COUNT)
            return True
        except Exception as e:
            log.error("device setup failed: %s", e)
            # open() succeeded if we got past it, and it starts the SDK's reader
            # and heartbeat threads — so bailing out without close() strands an
            # open hidraw fd plus two live threads for the process lifetime. The
            # object is not even collectable: both threads hold bound methods of
            # it, so __del__ never runs. Worse, self.device is only assigned
            # after init(), so a failure before that leaves the leak unreachable
            # AND makes try_open skip its own close (it keys off self.device),
            # letting every later replug pile on another one.
            with self._lock:
                orphan = dev is not self.device
            if orphan:
                try:
                    dev.close()
                except Exception:                     # already half-dead; best effort
                    log.debug("cleanup close() after failed setup also failed",
                              exc_info=True)
            return False

    def stop(self):
        self._running = False
        self._cancel_holds()
        self._monitor_stop.set()
        self._action_queue.put(None)   # unblock + end the action worker
        self._decode_queue.put(None)   # and the gif decode worker
        dev = self.device
        # Drop the callback BEFORE taking the lock: a _key_callback already
        # running is blocked on self._lock, and dev.close() below joins the
        # SDK reader thread. If close() ran while we held the lock, that
        # in-flight callback could never acquire it, the join would time out,
        # and the device would be left half-closed.
        if dev:
            try:
                dev.set_key_callback(None)
            except Exception:
                pass
        with self._lock:
            if dev:
                try:
                    time.sleep(0.05)
                    # Stop animations first so the GIF loop can't repaint the
                    # keys we are about to clear. Clearing the loop FLAG is not
                    # enough: the worker collects its frame batch under its own
                    # lock and writes AFTER releasing it, so one more frame
                    # could land after clearAllIcon() and stay lit on the
                    # physical deck once the app was gone. Stop the worker
                    # itself and wait for it.
                    dev.stop_gif_loop()
                    try:
                        if not dev.gif_controller.close():
                            log.debug("gif worker still running at clear time; "
                                      "a stale frame may survive")
                    except Exception:
                        log.debug("stopping the gif worker failed", exc_info=True)
                    self._gif_keys.clear()
                    dev.clearAllIcon()
                    dev.refresh()
                except Exception:
                    pass
            self.device = None
        # close() joins the reader thread — do it OUTSIDE the lock.
        if dev:
            try:
                dev.close()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self.device is not None

    # -- config helpers ----------------------------------------------------
    def profile(self) -> Profile:
        return self.config.active_profile()

    def container(self):
        """The page-holder currently shown: the active profile at the root, or
        a Folder when navigated into one."""
        return self._container if self._container is not None else self.config.active_profile()

    def at_root(self) -> bool:
        return not self._nav

    def page(self) -> Page:
        # RLock: navigation state (page_index/_container/_nav) is mutated by
        # the action-worker thread and read here on the render path; serialize
        # so a reader never sees a half-updated container/index pair.
        with self._lock:
            pages = self.container().pages
            self.page_index = max(0, min(self.page_index, len(pages) - 1))
            return pages[self.page_index]

    # -- folder navigation -------------------------------------------------
    def enter_folder(self, folder) -> None:
        if folder is None:
            return
        with self._lock:
            self._nav.append((self._container, self.page_index))
            self._container = folder
            self.page_index = 0
        self.render_page()

    def go_back(self) -> None:
        with self._lock:
            if not self._nav:
                return
            container, page_index = self._nav.pop()
            self._container = container
            self.page_index = page_index
        self.render_page()

    def reset_nav(self) -> None:
        """Return to the root of the active profile (used on profile switch)."""
        with self._lock:
            self._nav = []
            self._container = None
            self.page_index = 0

    # -- rendering ---------------------------------------------------------
    def render_key(self, index: int) -> None:
        # Hold the lock for the whole body: all app-initiated device writes and
        # _gif_keys mutations must be serialized across the GUI thread (edits)
        # and the action-worker thread (page/profile switches). RLock keeps
        # render_page -> render_key reentrant.
        with self._lock:
            dev = self.device
            if not dev:
                return
            kc = self.page().keys.get(index, KeyConfig())
            # A monitor readout replaces the key face entirely, icon included.
            is_monitor = kc.action.type == "monitor"
            is_gif = (not is_monitor
                      and kc.icon.lower().endswith(".gif") and os.path.exists(kc.icon))
            try:
                if is_gif and not self._gif_decode_ready(dev, index, kc.icon):
                    # Not decoded yet, and decoding costs ~244 ms for a
                    # 90-frame file. This runs on the Qt thread holding
                    # self._lock, so paying it here freezes the window AND
                    # stalls the SDK reader thread waiting on the same lock.
                    # Hand it to the decode worker and paint the static face
                    # for now; the worker re-renders this key once the decode
                    # is cached, and that second pass costs ~0 ms.
                    is_gif = False
                if is_gif:
                    # The backend returns -1 for a file that exists but can't
                    # be decoded (truncated download, mislabeled format). It
                    # writes nothing to the device in that case — treating it
                    # as animated anyway froze the key forever: marked in
                    # _gif_keys, the static path skipped, never painted again.
                    rc = dev.set_key_gif(index, kc.icon)
                    if isinstance(rc, int) and rc < 0:
                        log.warning("gif %r undecodable; rendering static", kc.icon)
                        is_gif = False
                    else:
                        self._gif_keys.add(index)
                if not is_gif:
                    if index in self._gif_keys:
                        dev.clear_key_gif(index)
                        self._gif_keys.discard(index)
                    if is_monitor:
                        spec = monitors.MonitorSpec.from_params(kc.action.params)
                        img = monitors.render_monitor(
                            dev.KEY_PIXEL_WIDTH, spec, self._sampler.last(spec),
                            self._sampler.history(spec), kc.bg_color, kc.text_color)
                        # force a fresh sample on the next tick so the cached
                        # value we just painted goes live quickly
                        self._monitor_state.pop(index, None)
                    else:
                        img = rendering.render_key(
                            dev.KEY_PIXEL_WIDTH, kc.label, kc.icon, kc.bg_color, kc.text_color)
                    self._note_write_result(dev.set_key_image_pil(index, img), index)
                self._sync_gif_loop()
            except Exception as e:
                log.error("render key %s failed: %s", index, e)

    # -- off-thread GIF decoding -------------------------------------------
    def _gif_decode_ready(self, dev, index: int, path: str) -> bool:
        """True if this GIF can be installed without paying for a decode here.

        False means "not yet" — the decode has been queued for the worker, and
        the caller should render the static face this time round. A file that
        failed to decode returns True so the caller takes its normal path and
        hits the existing rc < 0 handling, rather than being queued forever.
        """
        ctl = getattr(dev, "gif_controller", None)
        if ctl is None or not hasattr(ctl, "is_key_gif_cached"):
            return True                      # older SDK: behave as before
        key = (os.path.abspath(path), index)
        try:
            if ctl.is_key_gif_cached(path, index):
                return True
        except Exception:
            return True                      # never let the check break rendering
        with self._decode_lock:
            if key in self._decode_failed:
                return True                  # let the normal error path report it
            if key in self._decode_pending:
                return False                 # already queued; don't pile up
            self._decode_pending.add(key)
        self._decode_queue.put((index, path))
        return False

    def _decode_worker(self) -> None:
        """Decodes GIFs off the Qt thread, then asks for a re-render.

        Deliberately takes no controller lock while decoding: warm_key_gif
        touches only the decode cache, never the device.
        """
        while True:
            item = self._decode_queue.get()
            if item is None:                 # shutdown sentinel
                return
            index, path = item
            key = (os.path.abspath(path), index)
            ok = False
            attempted = False
            try:
                dev = self.device
                if dev is not None and self._running:
                    attempted = True
                    ok = dev.gif_controller.warm_key_gif(path, index)
            except Exception as e:
                attempted = True       # it ran and genuinely failed
                log.debug("background gif decode of %r failed: %s", path, e)
            finally:
                with self._decode_lock:
                    self._decode_pending.discard(key)
                    if attempted and not ok:
                        # Remember a REAL failure — an undecodable file — or
                        # render_key would queue it again on every render.
                        self._decode_failed.add(key)
                    # Not attempted (device unplugged mid-drain, or shutting
                    # down) is transient and must NOT be blacklisted. Doing so
                    # marked a perfectly good file as broken for the rest of the
                    # process, so after a replug that key fell back to decoding
                    # inline on the Qt thread — reinstating the exact ~244 ms
                    # stall this worker exists to remove, with nothing logged.
            if ok and self._running:
                try:
                    self.render_key(index)   # now a cache hit, ~0 ms
                    self.refresh()
                except Exception as e:
                    log.debug("re-render after gif decode failed: %s", e)

    def _note_write_result(self, result, index: int) -> None:
        """Notice a key write that did not land.

        set_key_image_stream returns None when the transport has no handle —
        it returns EARLY and writes nothing — and a negative int on a C-level
        error. Both were discarded by every caller, which is what made a dead
        handle invisible: the app carried on painting into nothing while the
        status bar said "connected". Log it, once per run of failures, so the
        log is useful without becoming a flood at one line per key per render.
        """
        failed = result is None or (isinstance(result, int) and result < 0)
        if not failed:
            if self._write_failures:
                log.info("device writes are landing again (after %d failed)",
                         self._write_failures)
            self._write_failures = 0
            return
        self._write_failures += 1
        if self._write_failures == 1:
            log.warning("key %s: the device accepted no data (result=%r) — the "
                        "handle is probably dead; further failures will be "
                        "counted, not logged", index, result)

    def _sync_gif_loop(self) -> None:
        dev = self.device
        if not dev:
            return
        try:
            if self._gif_keys:
                dev.start_gif_loop()
            else:
                dev.stop_gif_loop()
        except Exception as e:
            log.error("gif loop sync failed: %s", e)

    def render_page(self) -> None:
        with self._lock:
            # A page render is always a context change (page/profile/folder
            # switch, import, reconnect): drop every monitor gate so the new
            # page's monitor keys sample + paint on the next tick instead of
            # inheriting a stale timestamp/signature from the old page.
            self._monitor_state.clear()
            dev = self.device
            if dev:
                # drop animations from the previous page before re-rendering
                for k in list(self._gif_keys):
                    try:
                        dev.clear_key_gif(k)
                    except Exception:
                        pass
                self._gif_keys.clear()
                for i in range(1, dev.KEY_COUNT + 1):
                    self.render_key(i)
                self._sync_gif_loop()
                try:
                    dev.refresh()
                except Exception as e:
                    log.error("refresh failed: %s", e)
        # Fire even with no device so the GUI resyncs (e.g. editing folders offline).
        if self.on_page_changed:
            self.on_page_changed()

    # -- monitor keys ------------------------------------------------------
    def _monitor_loop(self):
        # 0.5 s scheduler granularity. With no monitor keys on the visible
        # page a tick is a single dict scan — no metric is ever sampled.
        while not self._monitor_stop.wait(0.5):
            try:
                self.monitor_tick()
            except Exception as e:      # a tick must never kill the thread
                log.error("monitor tick failed: %s", e)

    def monitor_tick(self, now: float | None = None) -> None:
        """Sample and repaint the monitor keys of the visible page that are
        due per their refresh interval. Runs on the monitor thread; callable
        directly with a fake clock in tests."""
        now = time.monotonic() if now is None else now
        with self._lock:
            page = self.page()
            dev = self.device
            size = dev.KEY_PIXEL_WIDTH if dev else int(DEVICE_PROFILE["key_size"])
            entries = [(i, kc) for i, kc in list(page.keys.items())
                       if kc.action.type == "monitor"]
        if not entries:
            if self._monitor_state:
                self._monitor_state.clear()
            return
        live = {i for i, _ in entries}
        # Which keys are due? (last_t None = never sampled — an explicit
        # sentinel, because time.monotonic() is small right after boot and a
        # 0.0 sentinel would wrongly look "recently sampled".)
        due = []
        for index, kc in entries:
            spec = monitors.MonitorSpec.from_params(kc.action.params)
            last_t = self._monitor_state.get(index, (None, None))[0]
            if last_t is None or now - last_t >= spec.interval:
                due.append((index, kc, spec))
        # Sample each STREAM once, shared by all its keys. cpu_percent and the
        # net counters are since-last-call deltas with global state — sampling
        # once per key would hand every key after the first a garbage ~0.
        readings: dict[tuple, monitors.Reading] = {}
        for _, _, spec in due:
            if spec.key() not in readings:
                readings[spec.key()] = self._sampler.sample(spec)
        pushed = False
        for index, kc, spec in due:
            try:
                reading = readings[spec.key()]
                # Capture the exact render inputs up front: the GUI mutates
                # KeyConfig objects IN PLACE, so kc's colors and params can
                # change under us at any point of this unlocked section.
                bg, fg = kc.bg_color, kc.text_color
                # Only repaint when something visible changed (graphs always move).
                sig = (spec, bg, fg, reading.text, reading.sub,
                       None if reading.pct is None else int(reading.pct))
                last_sig = self._monitor_state.get(index, (None, None))[1]
                if sig == last_sig and spec.style != "graph":
                    with self._lock:
                        # Refresh the timestamp only if the gate we compared
                        # against is still in place — a clear (render_page) or
                        # pop (render_key after an edit) that landed since must
                        # not be resurrected, or the new context waits a full
                        # interval behind a stale gate.
                        if self._monitor_state.get(index, (None, None))[1] == last_sig:
                            self._monitor_state[index] = (now, sig)
                    continue
                img = monitors.render_monitor(size, spec, reading,
                                              self._sampler.history(spec), bg, fg)
                emit = False
                with self._lock:
                    dev = self.device
                    cur = self.page().keys.get(index)
                    # Re-validate against the CURRENT config. Object identity
                    # alone proves nothing — the GUI edits the same KeyConfig
                    # in place, so a monitor key retuned to another metric/
                    # style/color mid-tick still passes `cur is kc`. Compare
                    # what we actually rendered with what the key wants NOW.
                    valid = (self.page() is page and cur is kc
                             and cur.action.type == "monitor"
                             and monitors.MonitorSpec.from_params(cur.action.params) == spec
                             and cur.bg_color == bg and cur.text_color == fg)
                    if valid:
                        ok = True
                        if dev and index not in self._gif_keys:
                            try:
                                dev.set_key_image_pil(index, img)
                                pushed = True
                            except Exception as e:
                                ok = False
                                log.error("monitor key %s failed: %s", index, e)
                        if ok:
                            # Stamp only AFTER the write succeeded (or no
                            # device to write to): stamping first would let
                            # the unchanged-sig fast path suppress every
                            # retry of a failed write while the value is
                            # stable — a stale key face forever.
                            self._monitor_state[index] = (now, sig)
                            emit = True
                        else:
                            # Failed write: leave no gate, so the very next
                            # tick resamples and retries. The GUI gets no
                            # frame either — preview and device must not
                            # silently diverge.
                            self._monitor_state.pop(index, None)
                if emit and self.on_monitor_image:
                    try:
                        # page id travels with the frame so the GUI can drop
                        # frames queued before a page switch but delivered
                        # after it (they'd repaint the wrong page's preview)
                        self.on_monitor_image(index, img, page.id)
                    except Exception as e:
                        log.error("monitor image callback failed: %s", e)
            except Exception as e:
                # One bad key must not freeze the other monitor keys or
                # starve the final refresh().
                log.error("monitor key %s tick failed: %s", index, e)
        # forget keys that stopped being monitors (cleared / retyped / swapped)
        for stale in set(self._monitor_state) - live:
            self._monitor_state.pop(stale, None)
        if pushed:
            self.refresh()

    # -- input dispatch ----------------------------------------------------
    def _enqueue(self, task):
        """Queue a 0-arg callable to run on the worker thread."""
        self._action_queue.put(task)

    def _dispatch(self, action):
        """Queue an action (or folder navigation) on the worker thread so a slow
        action can't stall the SDK reader thread."""
        if not action or action.type == "none":
            return
        if action.type == "folder_back":
            self._enqueue(self.go_back)
        else:
            self._enqueue(lambda a=action: actions.execute(a, self))

    def _action_worker(self):
        while True:
            task = self._action_queue.get()
            if task is None:
                return
            try:
                task()
            except Exception as e:   # never let the worker die
                log.error("action worker error: %s", e)

    def flash_key(self, index: int, pressed: bool) -> None:
        """Flash a key (brightened) on the device while pressed; restore it on
        release. Skips animated (GIF) keys, which the GIF loop keeps repainting."""
        if not self.config.glow:
            return
        with self._lock:
            dev = self.device
            if not dev or index in self._gif_keys:
                return
            kc = self.page().keys.get(index, KeyConfig())
            if kc.action.type == "monitor":
                # a static flash frame would overpaint the live readout until
                # the next tick; monitor keys don't react to presses anyway
                return
            try:
                img = rendering.render_key(
                    dev.KEY_PIXEL_WIDTH, kc.label, kc.icon, kc.bg_color,
                    kc.text_color, pressed=pressed)
                dev.set_key_image_pil(index, img)
                dev.refresh()
            except Exception as e:
                log.error("flash key %s failed: %s", index, e)

    def _key_callback(self, device, event):
        if event.event_type == EventType.BUTTON:
            index = int(event.key.value)
            pressed = event.state == 1
            if self.on_key_event:
                self.on_key_event(index, pressed)
            self.flash_key(index, pressed)
            if pressed:
                if index in self._holds:
                    return                       # duplicate down (missed release)
                kc = self.page().keys.get(index)
                if not kc:
                    return
                if kc.hold_action.type == "none":
                    # no hold action: fire on press-down, zero added latency
                    self._press_action(kc)
                else:
                    pending = _PendingHold(kc)

                    def _hold_fires(p=pending):
                        with p.lock:
                            if p.fired:
                                return           # release beat us to it
                            p.fired = True
                        self._dispatch(p.kc.hold_action)

                    t = threading.Timer(HOLD_THRESHOLD, _hold_fires)
                    t.daemon = True
                    pending.timer = t
                    self._holds[index] = pending
                    t.start()
            else:
                pending = self._holds.pop(index, None)
                if pending is None:
                    return
                if pending.timer is not None:
                    pending.timer.cancel()
                with pending.lock:
                    hold_fired = pending.fired
                    # claim the dispatch so a timer sliver that survived
                    # cancel() cannot double-fire after us
                    pending.fired = True
                if not hold_fired:
                    self._press_action(pending.kc)
        elif event.event_type in (EventType.KNOB_ROTATE, EventType.KNOB_PRESS):
            self._knob_event(event)

    def _press_action(self, kc: KeyConfig) -> None:
        """Dispatch a key's primary action (folders open, others execute)."""
        if kc.action.type == "open_folder" and kc.folder is not None:
            folder = kc.folder
            self._enqueue(lambda f=folder: self.enter_folder(f))
        else:
            self._dispatch(kc.action)

    def _knob_event(self, event):
        # knob index is device-specific; map knob_1.. to 1..
        try:
            kid = int(str(event.knob_id.value).split("_")[-1])
        except Exception:
            return
        kn = self.page().knobs.get(kid)
        if not kn:
            return
        if event.event_type == EventType.KNOB_PRESS and event.state == 1:
            self._dispatch(kn.press)
        elif event.event_type == EventType.KNOB_ROTATE:
            act = kn.right if getattr(event.direction, "value", "") == "right" else kn.left
            self._dispatch(act)

    # -- ActionContext implementation -------------------------------------
    def switch_profile(self, profile_id: str) -> None:
        if self.config.profile_by_id(profile_id):
            self.config.active_profile_id = profile_id
            self.reset_nav()
            self.render_page()

    def _rotate_profile(self, step: int) -> None:
        """Scene Shift: move to the next/previous profile (wrapping)."""
        profiles = self.config.profiles
        if len(profiles) < 2:
            return
        ids = [p.id for p in profiles]
        try:
            i = ids.index(self.config.active_profile_id)
        except ValueError:
            i = 0
        self.config.active_profile_id = ids[(i + step) % len(ids)]
        self.reset_nav()
        self.render_page()

    def next_profile(self) -> None:
        self._rotate_profile(1)

    def prev_profile(self) -> None:
        self._rotate_profile(-1)

    def sleep_screen(self) -> None:
        with self._lock:
            if self.device:
                try:
                    self.device.transport.sleep()
                except Exception as e:
                    log.error("sleep failed: %s", e)

    def goto_page(self, index: int) -> None:
        # Clamp: a "Go to page #" key configured with 0 (or beyond the last
        # page) must not store a bogus index — with no device attached nothing
        # else re-clamps it and the page combo ends up with no selection.
        with self._lock:
            n = len(self.container().pages)
            self.page_index = max(0, min(index, n - 1))
        self.render_page()

    def next_page(self) -> None:
        # container(), not profile(): inside a folder the visible pages are
        # the FOLDER's — counting the profile's pages made folder pages
        # unreachable (or wrapped wrongly) from the deck's page keys.
        with self._lock:
            n = len(self.container().pages)
            self.page_index = (self.page_index + 1) % n
        self.render_page()

    def prev_page(self) -> None:
        with self._lock:
            n = len(self.container().pages)
            self.page_index = (self.page_index - 1) % n
        self.render_page()

    def refresh(self) -> None:
        """Push pending image changes to the device (thread-safe)."""
        with self._lock:
            if self.device:
                try:
                    self.device.refresh()
                except Exception as e:
                    log.error("refresh failed: %s", e)

    def apply_brightness(self) -> None:
        with self._lock:
            if self.device:
                try:
                    self.device.set_brightness(self.config.brightness)
                except Exception as e:
                    log.error("brightness failed: %s", e)

    def set_brightness(self, percent: int) -> None:
        self.config.brightness = max(0, min(100, int(percent)))
        self.apply_brightness()
        # Tell the GUI, which owns the slider and the save timer. The deck path
        # previously did neither: the slider drifted out of sync, and the value
        # was never queued for save, so a deck-set brightness was lost on
        # restart unless some unrelated edit happened to trigger a write.
        if self.on_brightness_changed:
            self.on_brightness_changed(self.config.brightness)

    def adjust_brightness(self, delta: int) -> None:
        self.set_brightness(self.config.brightness + delta)
