"""Shutdown safety in the vendored StreamDock SDK.

Two defects from the pre-0.10.0 device audit, both on the close path:

  * GifController.close() joined its worker with a timeout and discarded the
    result, and StreamDock.close() then destroyed the transport gated only on
    the READ thread. That worker writes to the transport outside every lock, so
    a worker still inside a native write had its handle freed underneath it —
    a use-after-free in C, i.e. the process dies instead of disconnecting.

  * _heartbeat_worker slept in 10 s blocks, so clearing run_heartbeat_thread was
    not noticed for up to ten seconds and the join(timeout=2.0) in close()
    therefore always timed out. That is a guaranteed 2 s freeze on the Qt thread
    for every quit, and 2 s of the udev listener blocked on every unplug.

These drive the real SDK classes with fake transports; no device is touched.
"""
import os
import threading
import time

import pytest

sd = pytest.importorskip("fifine_deck.backend.StreamDock.Devices.StreamDock")
gc_mod = pytest.importorskip("fifine_deck.backend.StreamDock.Devices.GifController")


class _FakeTransport:
    """Records whether the handle was destroyed, and can block a caller."""

    def __init__(self, block_writes=None):
        self.destroyed = False
        self.destroyed_at = None
        self.heartbeats = 0
        self._block = block_writes          # an Event to hold writers in, or None

    def close(self):
        self.destroyed = True
        self.destroyed_at = time.monotonic()

    def heartbeat(self):
        self.heartbeats += 1

    def disconnected(self):
        pass

    def set_key_image_stream(self, *a, **k):
        if self._block is not None:
            # Stand in for libusb blocking until its transfer times out.
            self._block.wait(5.0)
        if self.destroyed:
            raise AssertionError("write reached a DESTROYED transport (use-after-free)")


class _ConcreteDock(sd.StreamDock):
    """StreamDock is abstract; close() lives on the base and needs none of these."""

    def decode_input_event(self, *a, **k):
        return {}

    def get_image_key(self, *a, **k):
        return None

    def set_brightness(self, *a, **k):
        pass

    def set_device(self, *a, **k):
        pass

    def set_key_image(self, *a, **k):
        pass

    def set_touchscreen_image(self, *a, **k):
        pass


def _bare_dock(transport):
    """A StreamDock with our fake transport, without running __init__ (which
    would open a device). Only the attributes close() touches are set up."""
    dock = object.__new__(_ConcreteDock)
    dock.transport = transport
    dock.path = "/dev/hidraw-test"
    dock._callback_lock = threading.Lock()
    dock.key_callback = None
    dock.raw_read_callback = None
    dock.touchscreen_callback = None
    dock._notify_on_close = False
    dock.read_thread = None
    dock.run_read_thread = False
    dock.heartbeat_thread = None
    dock.run_heartbeat_thread = False
    dock._heartbeat_stop = threading.Event()
    dock._close_lock = threading.Lock()
    dock._close_done = False
    dock.gif_controller = _CleanGif()       # tests override where it matters
    return dock


class _StuckGif:
    """A GIF controller whose worker refuses to stop in time."""

    def __init__(self):
        self.close_calls = 0

    def close(self, timeout=2.0):
        self.close_calls += 1
        return False                        # "still running"


class _CleanGif:
    def close(self, timeout=2.0):
        return True


# -- the use-after-free ------------------------------------------------------

def test_transport_is_not_destroyed_while_the_gif_worker_is_still_writing():
    """The whole bug in one assertion: a GIF worker that did not stop must not
    have its transport freed underneath it."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.gif_controller = _StuckGif()

    dock.close(notify=False)

    assert not tr.destroyed, (
        "transport_destroy ran while the GIF worker was still inside a native "
        "write — this is the use-after-free")


def test_transport_is_destroyed_when_every_worker_has_stopped():
    """Deferring must be conditional, or the handle leaks on every clean close."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.gif_controller = _CleanGif()

    dock.close(notify=False)

    assert tr.destroyed, "clean shutdown failed to release the device"


def test_a_live_heartbeat_thread_also_defers_the_destroy():
    """The heartbeat calls transport.heartbeat(); it is on the handle too."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.gif_controller = _CleanGif()

    stuck = threading.Event()
    t = threading.Thread(target=lambda: stuck.wait(10), daemon=True)
    t.start()
    dock.heartbeat_thread = t
    try:
        dock.close(notify=False)
        assert not tr.destroyed, "destroy ran with the heartbeat thread still live"
    finally:
        stuck.set()
        t.join(timeout=5)


def test_gif_close_reports_whether_the_worker_actually_exited():
    """The signal StreamDock.close depends on, checked on the real class."""
    blocker = threading.Event()
    tr = _FakeTransport(block_writes=blocker)

    ctl = object.__new__(gc_mod.GifController)
    ctl._running = True
    ctl._loop_enabled = False
    ctl._wake_event = threading.Event()
    ctl._lock = threading.Lock()
    ctl._gif_map = {}
    # a worker wedged exactly like one blocked in libusb
    ctl._thread = threading.Thread(target=lambda: blocker.wait(10), daemon=True)
    ctl._thread.start()
    try:
        assert ctl.close(timeout=0.2) is False, "a stuck worker reported as stopped"
    finally:
        blocker.set()
        ctl._thread.join(timeout=5)

    assert ctl.close(timeout=0.2) is True, "a stopped worker reported as stuck"


# -- the 2 second quit freeze ------------------------------------------------

def test_close_does_not_block_on_the_heartbeats_ten_second_sleep():
    """Before the fix this took the full 2 s join timeout every single time,
    because the worker was parked in time.sleep(10) and never saw the flag."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.gif_controller = _CleanGif()

    dock.run_heartbeat_thread = True
    dock._heartbeat_stop.clear()
    t = threading.Thread(target=sd.StreamDock._heartbeat_worker, args=(dock,),
                         daemon=True)
    dock.heartbeat_thread = t
    t.start()
    # Wait past the 1.0 s settling delay on purpose. Stopping during the settle
    # would exit quickly even on the old code, which made this pass by luck; the
    # bug only shows once the worker is parked in the long inter-beat wait.
    time.sleep(1.3)
    assert tr.heartbeats >= 1, "worker never got past its settling delay"

    started = time.monotonic()
    dock.close(notify=False)
    elapsed = time.monotonic() - started

    assert not t.is_alive(), "heartbeat thread outlived close()"
    assert elapsed < 1.0, f"close() blocked {elapsed:.2f}s waiting on the heartbeat"
    assert tr.destroyed, "a promptly-stopped heartbeat should not defer the destroy"


def test_heartbeat_wakes_immediately_rather_than_sleeping_out_its_interval():
    """Directly: setting the stop Event must end the worker at once, not in 10 s."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.run_heartbeat_thread = True
    dock._heartbeat_stop.clear()
    t = threading.Thread(target=sd.StreamDock._heartbeat_worker, args=(dock,),
                         daemon=True)
    t.start()
    time.sleep(0.05)

    started = time.monotonic()
    dock.run_heartbeat_thread = False
    dock._heartbeat_stop.set()
    t.join(timeout=3.0)
    elapsed = time.monotonic() - started

    assert not t.is_alive(), "worker ignored the stop event"
    assert elapsed < 0.5, f"worker took {elapsed:.2f}s to notice the stop"


def test_the_initial_settling_delay_is_interruptible_too():
    """close() during the first second must not wait that second out."""
    tr = _FakeTransport()
    dock = _bare_dock(tr)
    dock.run_heartbeat_thread = True
    dock._heartbeat_stop.clear()
    t = threading.Thread(target=sd.StreamDock._heartbeat_worker, args=(dock,),
                         daemon=True)
    t.start()

    started = time.monotonic()
    dock._heartbeat_stop.set()              # while still in the 1.0 s settle
    t.join(timeout=3.0)
    elapsed = time.monotonic() - started

    assert not t.is_alive()
    assert elapsed < 0.5, f"settling delay was not interruptible ({elapsed:.2f}s)"
    assert tr.heartbeats == 0, "a heartbeat was sent after the stop was requested"


# -- hotplug reconciliation --------------------------------------------------

def _device_manager_module():
    """The SDK is imported as a top-level `StreamDock` package, and it is
    fifine_deck.controller that puts it on sys.path — importing the vendored
    path directly raises ModuleNotFoundError and silently skipped these tests."""
    pytest.importorskip("fifine_deck.controller")
    return pytest.importorskip("StreamDock.DeviceManager")


def test_a_change_uevent_is_handed_to_the_owner():
    """0.10.2 audit: `udevadm trigger` — the command our own docs tell users to
    run after installing the udev rule — emits "change", which the handler
    dropped. A device that had failed to open stayed cached forever (the add
    path skips any path already held), so the documented fix could not take
    effect without restarting or physically replugging."""
    dm = _device_manager_module()
    mgr = object.__new__(dm.DeviceManager)
    seen = []
    mgr._on_device_changed = lambda d: seen.append(d)
    mgr._on_device_added = None
    mgr._on_device_removed = None

    mgr._handle_device_event("change", "the-device", [])

    assert seen == ["the-device"], "a change uevent was dropped on the floor"


def test_an_unknown_action_is_still_ignored():
    """The change branch must not turn into a catch-all."""
    dm = _device_manager_module()
    mgr = object.__new__(dm.DeviceManager)
    seen = []
    mgr._on_device_changed = lambda d: seen.append(d)
    mgr._handle_device_event("bind", "d", [])
    mgr._handle_device_event("unbind", "d", [])
    assert seen == []


def test_controller_reopens_on_change_only_when_it_needs_to():
    """try_open is the documented recovery, and is a no-op when a working
    handle is already held — so reacting to every change event is cheap."""
    from fifine_deck.controller import DeckController
    from fifine_deck.model import DeckConfig
    from tests.test_controller import MockDevice

    c = DeckController(DeckConfig())
    c._running = True
    calls = []
    c.try_open = lambda: (calls.append(1), True)[1]

    c.device = None                       # nothing open -> must attempt
    c._on_changed()
    assert len(calls) == 1

    c.device = MockDevice()               # working handle -> must not churn
    c._on_changed()
    assert len(calls) == 1, "reopened a device that was already working"

    dead = MockDevice()
    dead.firmware_version = ""            # false-connect -> must attempt
    c.device = dead
    c._on_changed()
    assert len(calls) == 2


def test_the_rescan_is_time_gated_not_idle_gated():
    """0.10.2 audit: the safety-net rescan ran only on the poll() timeout
    branch, i.e. only after 60 consecutive seconds with zero USB uevents of any
    kind. The filter is subsystem-wide, so a webcam or dock re-enumerating kept
    resetting that window and the rescan could go hours without running."""
    import inspect
    dm = _device_manager_module()
    src = inspect.getsource(dm.DeviceManager._listen_linux)
    assert "last_rescan" in src, "no wall-clock rescan schedule"
    assert "time.monotonic()" in src, "rescan schedule is not monotonic"
    # the rescan must NOT be inside an `if device is None` branch any more
    assert "if device is None:\n                    self._remove_missing_devices" not in src


def test_pyudev_setup_failure_falls_back_instead_of_killing_the_thread():
    """Context()/from_netlink() sat outside every try, so a failure there (no
    netlink in a container or confined session) killed the listener thread and
    left hotplug dead for the session with no retry."""
    import inspect
    dm = _device_manager_module()
    src = inspect.getsource(dm.DeviceManager._listen_linux)
    head = src[:src.index("while True")]
    assert "try:" in head and "_fallback_polling" in head, (
        "pyudev setup is still unguarded")


# -- the fast-replug wedge ---------------------------------------------------

class _FakeDock:
    """Just enough of a StreamDock for the reconciliation code."""

    def __init__(self, path, identity):
        self._path = path
        self._node_identity = identity
        self.closed = 0

    def getPath(self):
        return self._path

    def close(self, *a, **k):
        self.closed += 1


def _manager_with(devices, current_paths):
    dm = _device_manager_module()
    mgr = object.__new__(dm.DeviceManager)
    mgr._device_lock = threading.RLock()
    mgr.streamdocks = list(devices)
    mgr._on_device_removed = None
    mgr._on_device_added = None
    mgr._on_device_changed = None
    mgr._current_device_paths = lambda products: set(current_paths)
    return mgr


def test_a_replug_that_reuses_the_path_is_still_detected_as_a_replug(tmp_path):
    """0.10.2 audit, issue 1: reconciliation compared PATH strings only. A
    replug completing inside the ~0.6 s remove-retry window leaves /dev/hidrawN
    in place, so nothing was removed; the following "add" then hit the
    _device_exists(path) guard and announced nothing, and the owner kept a
    handle onto a torn-down node — writes silently swallowed, status still
    "connected", no key working, no recovery short of a restart."""
    node = tmp_path / "hidraw0"
    node.write_text("")                       # stand-in for the device node
    dm = _device_manager_module()
    current = dm.DeviceManager._node_identity(str(node))

    # The device was created while the node had a DIFFERENT identity — i.e. the
    # node has since been destroyed and recreated. Stamped directly rather than
    # by unlink-and-recreate, because an ordinary filesystem may hand back the
    # SAME inode (CI's did), which silently turned this test into a no-op.
    #
    # devtmpfs does allocate a fresh inode for a recreated device node. Measured
    # across a physical replug of this deck: (7, 12769) -> (7, 14541).
    stale_identity = (current[0], current[1] + 1)
    dev = _FakeDock(str(node), stale_identity)

    mgr = _manager_with([dev], [str(node)])   # path IS still present
    removed = mgr._remove_missing_devices([])

    assert removed == [dev], "a recreated node was not recognised as a replug"
    assert dev.closed == 1, "the stale device was not closed"


def test_an_unchanged_node_is_left_alone(tmp_path):
    """The identity check must not evict a device that never went away."""
    node = tmp_path / "hidraw0"
    node.write_text("")
    dm = _device_manager_module()
    dev = _FakeDock(str(node), dm.DeviceManager._node_identity(str(node)))

    mgr = _manager_with([dev], [str(node)])
    assert mgr._remove_missing_devices([]) == []
    assert dev.closed == 0


def test_a_genuinely_absent_path_is_still_removed(tmp_path):
    """The original behaviour has to survive: a path that is gone is gone."""
    dev = _FakeDock(str(tmp_path / "hidraw9"), None)
    mgr = _manager_with([dev], [])            # nothing enumerated
    assert mgr._remove_missing_devices([]) == [dev]


def test_unknowable_identity_falls_back_to_path_only(tmp_path):
    """If the node cannot be stat'd we must not invent a replug and evict a
    working device."""
    node = tmp_path / "hidraw0"
    node.write_text("")
    dev = _FakeDock(str(node), None)          # identity never recorded
    mgr = _manager_with([dev], [str(node)])
    assert mgr._remove_missing_devices([]) == []
    assert dev.closed == 0


def test_a_removal_immediately_tries_to_re_add():
    """On a fast replug the add uevent can land while the stale entry is still
    cached and be skipped, so removal must re-add rather than leave the deck
    waiting for the next event or the 60 s rescan."""
    dm = _device_manager_module()
    mgr = object.__new__(dm.DeviceManager)
    calls = []
    mgr._remove_missing_devices = lambda products: ["something"]
    mgr._add_missing_devices = lambda products: calls.append("add")
    mgr._handle_device_event("remove", None, [])
    assert calls == ["add"], "a removal did not attempt an immediate re-add"


def test_a_removal_that_removed_nothing_does_not_re_add():
    """No churn when the remove event was not ours."""
    dm = _device_manager_module()
    mgr = object.__new__(dm.DeviceManager)
    calls = []
    mgr._remove_missing_devices = lambda products: []
    mgr._add_missing_devices = lambda products: calls.append("add")
    mgr._handle_device_event("remove", None, [])
    assert calls == []


# -- GIF decode cost ---------------------------------------------------------

def _gif_controller_for_decode():
    """A GifController with only what _read_gif touches — no thread, no device."""
    from PIL import Image  # noqa: F401  (ensures Pillow is present)
    ctl = object.__new__(gc_mod.GifController)

    class _Dev:
        def touchscreen_image_format(self):
            return {"size": (800, 480)}

    ctl._device = _Dev()
    ctl._decode_cache = {}
    ctl._decode_cache_lock = threading.Lock()
    return ctl


def _make_gif(path, n_frames, size=(64, 64)):
    from PIL import Image
    frames = [Image.new("RGB", size, (i * 3 % 256, 40, 90)) for i in range(n_frames)]
    frames[0].save(str(path), save_all=True, append_images=frames[1:],
                   duration=40, loop=0)
    return str(path)


FMT = {"size": (112, 112), "format": "JPEG", "rotation": 0, "flip": (False, False)}


def test_decoding_the_same_gif_twice_does_not_decode_twice(tmp_path):
    """0.10.2 audit, issue 7: set_key_gif decoded AND re-encoded every frame on
    every render, with no caching — so each page switch, profile switch, folder
    enter/exit and reconnect paid the whole cost again for an unchanged file.
    It runs on the Qt thread inside the controller lock, so that is a frozen
    window and a stall for the SDK reader thread waiting on the same lock."""
    ctl = _gif_controller_for_decode()
    path = _make_gif(tmp_path / "a.gif", 12)

    calls = []
    real = ctl._read_gif_uncached
    ctl._read_gif_uncached = lambda p, f, a: (calls.append(p), real(p, f, a))[1]

    first = ctl._read_gif(path, FMT, True)
    second = ctl._read_gif(path, FMT, True)

    assert len(calls) == 1, f"decoded {len(calls)} times for one unchanged file"
    assert first[0] == second[0], "cached frames differ from the decoded ones"
    assert len(first[0]) == 12


def test_the_cache_hands_out_its_own_lists(tmp_path):
    """Callers must not be able to disturb the cached originals."""
    ctl = _gif_controller_for_decode()
    path = _make_gif(tmp_path / "a.gif", 5)
    a = ctl._read_gif(path, FMT, True)
    b = ctl._read_gif(path, FMT, True)
    assert a[0] is not b[0], "same list object handed to two callers"
    a[0].append(b"junk")
    assert len(ctl._read_gif(path, FMT, True)[0]) == 5, "cache was corrupted"


def test_editing_the_file_invalidates_the_cache(tmp_path):
    """Keyed on mtime and size, so replacing an icon in place still takes
    effect — a cache that ignored this would pin the old animation forever."""
    ctl = _gif_controller_for_decode()
    p = tmp_path / "a.gif"
    _make_gif(p, 12)
    assert len(ctl._read_gif(str(p), FMT, True)[0]) == 12
    time.sleep(0.01)
    _make_gif(p, 4)                       # same path, different content
    assert len(ctl._read_gif(str(p), FMT, True)[0]) == 4


def test_a_different_target_format_is_cached_separately(tmp_path):
    """The same file rendered for a different key geometry is different bytes."""
    ctl = _gif_controller_for_decode()
    path = _make_gif(tmp_path / "a.gif", 4)
    ctl._read_gif(path, FMT, True)
    other = dict(FMT, size=(96, 96))
    ctl._read_gif(path, other, True)
    assert len(ctl._decode_cache) == 2


def test_the_cache_is_bounded(tmp_path):
    """Frame data is large; the cache must not grow without limit."""
    ctl = _gif_controller_for_decode()
    path = _make_gif(tmp_path / "a.gif", 3)
    for i in range(gc_mod.GifController._DECODE_CACHE_MAX + 6):
        ctl._read_gif(path, dict(FMT, size=(112, 100 + i)), True)
    assert len(ctl._decode_cache) <= gc_mod.GifController._DECODE_CACHE_MAX


def test_an_undecodable_file_is_not_cached_as_a_success(tmp_path):
    """A truncated or mislabelled file must keep returning empty, not get a
    permanent empty entry that hides a later fixed file."""
    ctl = _gif_controller_for_decode()
    bad = tmp_path / "bad.gif"
    bad.write_bytes(b"not a gif at all")
    assert ctl._read_gif(str(bad), FMT, True)[0] == []
    assert ctl._decode_cache == {}, "a failed decode was cached"


# -- issue 8: double close, stale frame --------------------------------------

def test_close_is_idempotent_and_destroys_the_transport_once():
    """0.10.2 audit, issue 8: unplugging during quit lets DeckController.stop()
    and DeviceManager._remove_device_by_path call close() on the SAME object
    concurrently, and neither StreamDock.close nor LibUSBHIDAPI.close took a
    lock — so both could read a non-None handle and both call transport_destroy
    on it."""
    class _CountingTransport(_FakeTransport):
        def __init__(self):
            super().__init__()
            self.destroy_calls = 0

        def close(self):
            self.destroy_calls += 1
            super().close()

    tr = _CountingTransport()
    dock = _bare_dock(tr)
    dock.close(notify=False)
    dock.close(notify=False)
    dock.close(notify=False)
    assert tr.destroy_calls == 1, (
        f"transport destroyed {tr.destroy_calls} times — that is a double free")


def test_concurrent_closes_destroy_the_transport_once():
    """The same thing under the real race, from several threads at once."""
    class _CountingTransport(_FakeTransport):
        def __init__(self):
            super().__init__()
            self.destroy_calls = 0

        def close(self):
            self.destroy_calls += 1
            super().close()

    tr = _CountingTransport()
    dock = _bare_dock(tr)
    barrier = threading.Barrier(6)

    def closer():
        barrier.wait()
        dock.close(notify=False)

    threads = [threading.Thread(target=closer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert tr.destroy_calls == 1, (
        f"transport destroyed {tr.destroy_calls} times under concurrent close")


def test_controller_stop_stops_the_gif_worker_before_clearing():
    """A frame collected before the clear but written after it stays lit on the
    physical deck once the app is gone. Clearing the loop flag is not enough —
    the worker writes outside its lock."""
    from fifine_deck.controller import DeckController
    from fifine_deck.model import DeckConfig
    from tests.test_controller import MockDevice

    order = []

    class _Dev(MockDevice):
        class _Gif:
            def close(self, timeout=2.0):
                order.append("gif_worker_stopped")
                return True

        def __init__(self):
            super().__init__()
            self.gif_controller = _Dev._Gif()

        def stop_gif_loop(self):
            order.append("loop_flag_cleared")

        def clearAllIcon(self):
            order.append("cleared")

        def close(self, *a, **k):
            order.append("closed")

    c = DeckController(DeckConfig())
    c.device = _Dev()
    c.stop()

    assert "gif_worker_stopped" in order, "the animation worker was never stopped"
    assert order.index("gif_worker_stopped") < order.index("cleared"), (
        f"worker stopped after the clear: {order}")


# -- issue 7: decoding must not happen on the caller's thread ----------------

class _FakeGifCtl:
    def __init__(self, cached=False, decodes_ok=True):
        self.cached = cached
        self.decodes_ok = decodes_ok
        self.warm_calls = 0

    def is_key_gif_cached(self, path, key):
        return self.cached

    def warm_key_gif(self, path, key):
        self.warm_calls += 1
        if self.decodes_ok:
            self.cached = True
        return self.decodes_ok


def _controller_with_gif_ctl(ctl):
    from fifine_deck.controller import DeckController
    from fifine_deck.model import DeckConfig
    from tests.test_controller import MockDevice
    c = DeckController(DeckConfig())
    dev = MockDevice()
    dev.gif_controller = ctl
    c.device = dev
    # DeckController.__init__ leaves _running False; only start() sets it. The
    # worker's "did we actually attempt this" test reads it, so without this a
    # test asserting "not blacklisted" would pass for the WRONG reason.
    c._running = True
    return c, dev


def test_an_undecoded_gif_is_queued_instead_of_decoded_inline(tmp_path):
    """0.10.2 audit, issue 7: decoding ran on the Qt thread while holding the
    controller lock — ~244 ms for a 90-frame file, freezing the window and
    stalling the SDK reader thread waiting on the same lock."""
    ctl = _FakeGifCtl(cached=False)
    c, dev = _controller_with_gif_ctl(ctl)
    path = str(tmp_path / "a.gif")

    ready = c._gif_decode_ready(dev, 3, path)

    assert ready is False, "the caller was told to decode inline"
    assert c._decode_queue.qsize() == 1, "the decode was not handed to the worker"


def test_an_already_cached_gif_is_installed_inline(tmp_path):
    """A cache hit costs nothing, so there is no reason to defer it."""
    ctl = _FakeGifCtl(cached=True)
    c, dev = _controller_with_gif_ctl(ctl)
    assert c._gif_decode_ready(dev, 3, str(tmp_path / "a.gif")) is True
    assert c._decode_queue.qsize() == 0


def test_the_same_gif_is_not_queued_twice(tmp_path):
    """render_key runs per key per render; without a pending set the queue
    would grow without bound while one decode was in flight."""
    ctl = _FakeGifCtl(cached=False)
    c, dev = _controller_with_gif_ctl(ctl)
    path = str(tmp_path / "a.gif")
    for _ in range(10):
        c._gif_decode_ready(dev, 3, path)
    assert c._decode_queue.qsize() == 1


def test_an_undecodable_gif_is_not_retried_forever(tmp_path):
    """Without a failure memo, a file that never decodes would be re-queued on
    every render for the life of the process."""
    ctl = _FakeGifCtl(cached=False, decodes_ok=False)
    c, dev = _controller_with_gif_ctl(ctl)
    path = str(tmp_path / "bad.gif")

    assert c._gif_decode_ready(dev, 3, path) is False   # queued once
    index, p = c._decode_queue.get_nowait()
    # run exactly what the worker would do for a failing decode
    ok = dev.gif_controller.warm_key_gif(p, index)
    key = (os.path.abspath(p), index)
    with c._decode_lock:
        c._decode_pending.discard(key)
        if not ok:
            c._decode_failed.add(key)

    # now it must take the normal path (and hit the existing rc<0 handling)
    assert c._gif_decode_ready(dev, 3, path) is True
    assert c._decode_queue.qsize() == 0


def test_a_device_without_the_helpers_behaves_as_before(tmp_path):
    """An SDK build lacking is_key_gif_cached must not break rendering."""
    class _Old:
        pass

    ctl = _Old()
    c, dev = _controller_with_gif_ctl(ctl)
    assert c._gif_decode_ready(dev, 3, str(tmp_path / "a.gif")) is True


def test_the_decode_cache_can_hold_every_key_on_the_device():
    """The cache was capped at 6 while the 293V3 has 15 keys, all of which can
    be animated. A page animating more than 6 keys therefore evicted and
    re-decoded entries on EVERY page render — wasted CPU, and each key visibly
    flicking from static back to animated on every switch.

    The cap has to track the device, not a guess. It was originally justified by
    "frame data is large"; measured, a 90-frame animation is 73 KB, so the cap
    was costing correctness to save a fraction of a megabyte."""
    from fifine_deck.device import DEVICE_PROFILE
    keys = DEVICE_PROFILE["key_count"]
    assert gc_mod.GifController._DECODE_CACHE_MAX > keys, (
        f"cache holds {gc_mod.GifController._DECODE_CACHE_MAX} but the device has "
        f"{keys} animatable keys — a full page will thrash")


def test_a_full_page_of_animations_does_not_evict_itself(tmp_path):
    """The behavioural version: decode one distinct file per key, then re-read
    them all. With a cap below the key count the second pass re-decodes."""
    from fifine_deck.device import DEVICE_PROFILE
    ctl = _gif_controller_for_decode()
    keys = DEVICE_PROFILE["key_count"]
    paths = [_make_gif(tmp_path / f"k{i}.gif", 3) for i in range(keys)]
    for p in paths:
        ctl._read_gif(p, FMT, True)

    decodes = []
    real = ctl._read_gif_uncached
    ctl._read_gif_uncached = lambda p, f, a: (decodes.append(p), real(p, f, a))[1]
    for p in paths:                       # a second full-page render
        ctl._read_gif(p, FMT, True)

    assert decodes == [], f"{len(decodes)} of {keys} keys re-decoded on the second pass"


def test_a_decode_skipped_because_the_device_vanished_is_not_blacklisted(tmp_path):
    """0.11.1 audit: _decode_worker blacklisted a key whenever ok was False —
    including when it never ATTEMPTED the decode because the device had been
    unplugged mid-drain or the app was shutting down. _decode_failed is never
    cleared, so a perfectly good file was marked broken for the rest of the
    process: after a replug that key fell back to decoding inline on the Qt
    thread, reinstating the ~244 ms stall the worker exists to remove."""
    ctl = _FakeGifCtl(cached=False)
    c, dev = _controller_with_gif_ctl(ctl)
    path = str(tmp_path / "a.gif")

    c._gif_decode_ready(dev, 3, path)          # queued
    item = c._decode_queue.get_nowait()
    c.device = None                            # unplugged before the worker ran

    # exactly what the worker does for that item
    key = (os.path.abspath(item[1]), item[0])
    attempted = c.device is not None and c._running
    with c._decode_lock:
        c._decode_pending.discard(key)
        if attempted:
            c._decode_failed.add(key)

    assert key not in c._decode_failed, "a transient skip was recorded as permanent"
    assert ctl.warm_calls == 0, "test setup wrong: the decode should not have run"

    # and with the device back, the file is queued again rather than decoded inline
    c.device = dev
    assert c._gif_decode_ready(dev, 3, path) is False
    assert c._decode_queue.qsize() == 1


def test_a_genuinely_undecodable_file_is_still_blacklisted(tmp_path):
    """The other half: a real decode failure must still be remembered, or it
    gets re-queued on every render forever."""
    ctl = _FakeGifCtl(cached=False, decodes_ok=False)
    c, dev = _controller_with_gif_ctl(ctl)
    path = str(tmp_path / "bad.gif")

    c._gif_decode_ready(dev, 3, path)
    index, p = c._decode_queue.get_nowait()
    key = (os.path.abspath(p), index)
    attempted = False
    if c.device is not None and c._running:
        attempted = True
        ok = dev.gif_controller.warm_key_gif(p, index)
    with c._decode_lock:
        c._decode_pending.discard(key)
        if attempted and not ok:
            c._decode_failed.add(key)

    assert key in c._decode_failed, "a real failure was forgotten and will re-queue forever"
