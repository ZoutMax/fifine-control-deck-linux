"""Runtime controller tests driven by a mock device (no hardware needed).

A MockDevice implements the small surface the controller uses, so we can test
the connect->render path, page/profile/folder navigation, brightness, key-press
dispatch and the press flash without a real Stream Dock.
"""
import time

import pytest

# The controller imports the vendored transport SDK; skip cleanly if it can't
# load here (the offscreen smoke test covers importability in CI).
controller = pytest.importorskip("fifine_deck.controller")

from fifine_deck.controller import DeckController
from fifine_deck.model import Action, DeckConfig, Folder, Page, Profile


class MockDevice:
    """Records the calls the controller makes, so tests can assert on them."""
    KEY_COUNT = 15
    KEY_PIXEL_WIDTH = 100
    KEY_PIXEL_HEIGHT = 100
    firmware_version = "MOCK"

    def __init__(self):
        self.opened = False
        self.closed = False
        self.brightness = None
        self.callback = None
        self.key_images = {}     # index -> last image/gif set
        self.refreshes = 0
        self.gif_loop = False

        class _T:
            def sleep(self_):
                self_.slept = True
        self.transport = _T()

    def open(self):
        self.opened = True
        return True

    def init(self):
        pass

    def set_key_callback(self, cb):
        self.callback = cb

    def set_key_image_pil(self, index, img):
        self.key_images[index] = img

    def set_key_gif(self, index, path):
        self.key_images[index] = ("gif", path)

    def clear_key_gif(self, index):
        self.key_images.pop(index, None)

    def set_brightness(self, pct):
        self.brightness = pct

    def refresh(self):
        self.refreshes += 1

    def clearAllIcon(self):
        self.key_images.clear()

    def close(self):
        self.closed = True

    def start_gif_loop(self):
        self.gif_loop = True

    def stop_gif_loop(self):
        self.gif_loop = False


def _connected():
    """A controller wired to a freshly-connected mock device."""
    c = DeckController(DeckConfig())
    dev = MockDevice()
    assert c._setup_device(dev) is True
    return c, dev


def test_setup_device_connects_renders_and_sets_brightness():
    c, dev = _connected()
    try:
        assert c.connected and dev.opened
        assert dev.callback is not None                 # key callback wired
        assert dev.brightness == c.config.brightness     # apply_brightness ran
        assert len(dev.key_images) == dev.KEY_COUNT       # every key rendered
    finally:
        c.stop()


def test_page_navigation_wraps():
    c, dev = _connected()
    try:
        c.config.active_profile().pages.append(Page(name="P2"))
        assert c.page_index == 0
        c.next_page(); assert c.page_index == 1
        c.next_page(); assert c.page_index == 0    # wraps (2 pages)
        c.prev_page(); assert c.page_index == 1
        c.goto_page(0); assert c.page_index == 0
    finally:
        c.stop()


def test_profile_switch_and_rotate():
    c, dev = _connected()
    try:
        p2 = Profile(name="P2")
        c.config.profiles.append(p2)
        first = c.config.active_profile_id
        c.next_profile(); assert c.config.active_profile_id == p2.id
        c.prev_profile(); assert c.config.active_profile_id == first
        c.switch_profile(p2.id); assert c.config.active_profile_id == p2.id
    finally:
        c.stop()


def test_folder_navigation():
    c, dev = _connected()
    try:
        fld = Folder(name="Apps")
        c.config.active_profile().pages[0].key(1).folder = fld
        assert c.at_root()
        c.enter_folder(fld)
        assert not c.at_root() and c.container() is fld
        c.go_back()
        assert c.at_root()
    finally:
        c.stop()


def test_brightness_clamped():
    c, dev = _connected()
    try:
        c.set_brightness(50); assert c.config.brightness == 50 and dev.brightness == 50
        c.set_brightness(500); assert c.config.brightness == 100    # clamped high
        c.adjust_brightness(-40); assert c.config.brightness == 60
    finally:
        c.stop()


def test_key_press_dispatches_action():
    from StreamDock.InputTypes import ButtonKey, EventType, InputEvent
    c, dev = _connected()
    try:
        c.config.active_profile().pages.append(Page(name="P2"))
        c.config.active_profile().pages[0].key(1).action = Action("next_page", {})
        ev = InputEvent(event_type=EventType.BUTTON, key=ButtonKey(1), state=1)
        c._key_callback(dev, ev)
        # the action runs on the worker thread — poll for the effect
        for _ in range(100):
            if c.page_index == 1:
                break
            time.sleep(0.01)
        assert c.page_index == 1
    finally:
        c.stop()


def test_flash_key_renders_pressed_image():
    c, dev = _connected()
    try:
        c.config.active_profile().pages[0].key(1).label = "X"
        dev.key_images.clear(); dev.refreshes = 0
        c.flash_key(1, True)
        assert 1 in dev.key_images and dev.refreshes >= 1
    finally:
        c.stop()


def test_render_page_no_device_still_fires_callback():
    # editing offline (no device) must still notify the GUI to resync
    c = DeckController(DeckConfig())
    try:
        fired = []
        c.on_page_changed = lambda: fired.append(True)
        c.render_page()
        assert fired == [True]
    finally:
        c.stop()


def test_page_keys_navigate_the_folder_when_inside_one():
    """next/prev/goto page must count the CONTAINER's pages: inside a folder
    they used the profile's page count, making folder pages unreachable or
    wrapping wrongly from the deck's page keys (audit finding)."""
    from fifine_deck.model import Folder, Page
    c, dev = _connected()
    try:
        prof = c.config.active_profile()          # 1 page at profile root
        folder = Folder(name="F", pages=[Page(name="F1"), Page(name="F2"),
                                         Page(name="F3")])
        c.enter_folder(folder)
        assert c.page().name == "F1"
        c.next_page()
        assert c.page().name == "F2", "next_page used the profile's page count"
        c.next_page(); c.next_page()
        assert c.page().name == "F1"              # wraps at the FOLDER's count
        c.prev_page()
        assert c.page().name == "F3"
        assert len(prof.pages) == 1               # profile untouched
    finally:
        c.stop()


def test_goto_page_clamps_bogus_indices():
    """'Go to page #' configured with 0 produced page_index -1; with no device
    nothing re-clamped it and the GUI page combo lost its selection."""
    from fifine_deck.model import Page
    c, dev = _connected()
    try:
        c.config.active_profile().pages.append(Page(name="P2"))
        c.goto_page(-1)                           # user typed "0"
        assert c.page_index == 0
        c.goto_page(99)
        assert c.page_index == 1                  # clamped to the last page
    finally:
        c.stop()


# ---------------------------------------------------------------------------
# 0.8.0: press-and-hold key actions (issue #4)
# ---------------------------------------------------------------------------
def _btn(state, key=1):
    from StreamDock.InputTypes import ButtonKey, EventType, InputEvent
    return InputEvent(event_type=EventType.BUTTON, key=ButtonKey(key), state=state)


def _until(cond, timeout=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_key_without_hold_action_fires_on_press_down():
    """Regression guard: plain keys keep firing the moment they go DOWN —
    the hold feature must add zero latency to them."""
    c, dev = _connected()
    try:
        c.config.active_profile().pages[0].key(1).action = \
            Action("brightness", {"mode": "set", "value": "30"})
        c._key_callback(dev, _btn(1))          # down only, never released
        assert _until(lambda: dev.brightness == 30)
    finally:
        c.stop()


def test_short_press_with_hold_action_fires_primary_on_release(monkeypatch):
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.25)
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("brightness", {"mode": "set", "value": "30"})
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))          # down
        time.sleep(0.05)
        assert dev.brightness != 30            # deferred: nothing on down
        c._key_callback(dev, _btn(0))          # quick release
        assert _until(lambda: dev.brightness == 30)
        time.sleep(0.35)                       # well past the threshold
        assert dev.brightness == 30            # hold action never fired
    finally:
        c.stop()


def test_long_hold_fires_hold_action_and_suppresses_primary(monkeypatch):
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.1)
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("brightness", {"mode": "set", "value": "30"})
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))          # down and keep holding
        assert _until(lambda: dev.brightness == 77)
        c._key_callback(dev, _btn(0))          # release after the hold fired
        time.sleep(0.15)
        assert dev.brightness == 77            # primary stayed suppressed
    finally:
        c.stop()


class _FakeTimer:
    """Deterministic stand-in for threading.Timer: never fires on its own,
    exposes the callback so tests can invoke the race sliver by hand."""
    instances: list = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled = interval, fn, False
        self.daemon = False
        _FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def test_timer_sliver_after_release_cannot_double_fire(monkeypatch):
    """The race arbiter: a hold timer that slipped past cancel() must see the
    release's claim on `fired` and do nothing."""
    monkeypatch.setattr(controller.threading, "Timer", _FakeTimer)
    _FakeTimer.instances = []
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("brightness", {"mode": "set", "value": "30"})
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))          # down (fake timer armed)
        assert len(_FakeTimer.instances) == 1
        c._key_callback(dev, _btn(0))          # release -> primary dispatched
        assert _until(lambda: dev.brightness == 30)
        _FakeTimer.instances[0].fn()           # the sliver fires anyway
        time.sleep(0.1)
        assert dev.brightness == 30            # claimed: hold did NOT run
    finally:
        c.stop()


def test_duplicate_down_events_do_not_stack_holds(monkeypatch):
    monkeypatch.setattr(controller.threading, "Timer", _FakeTimer)
    _FakeTimer.instances = []
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))
        c._key_callback(dev, _btn(1))          # device hiccup: second down
        assert len(_FakeTimer.instances) == 1  # one pending hold, not two
        assert len(c._holds) == 1
    finally:
        c.stop()


def test_folder_key_with_hold_action_still_opens_on_short_press(monkeypatch):
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.25)
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("open_folder", {})
        kc.folder = Folder(pages=[Page(name="Inside")])
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))
        c._key_callback(dev, _btn(0))          # quick press
        assert _until(lambda: c.container() is kc.folder)
        assert dev.brightness != 77
    finally:
        c.stop()


def test_folder_key_with_hold_action_holds_without_entering(monkeypatch):
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.1)
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("open_folder", {})
        kc.folder = Folder(pages=[Page(name="Inside")])
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))          # hold it
        assert _until(lambda: dev.brightness == 77)
        c._key_callback(dev, _btn(0))
        time.sleep(0.15)
        assert c.container() is not kc.folder  # folder NOT entered
    finally:
        c.stop()


def test_unplug_mid_hold_cancels_and_replug_press_works(monkeypatch):
    """Audit finding: a lost release (unplug mid-hold) left a stale _holds
    entry — the armed timer fired against a gone device and the key's next
    genuine press after replug was silently swallowed."""
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.1)
    c, dev = _connected()
    try:
        kc = c.config.active_profile().pages[0].key(1)
        kc.action = Action("brightness", {"mode": "set", "value": "30"})
        kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
        c._key_callback(dev, _btn(1))          # down...
        c._on_removed(dev)                     # ...and the deck unplugs
        assert c._holds == {}                  # entry cancelled
        time.sleep(0.2)
        assert c.config.brightness != 77       # timer did not fire the hold
        assert c._setup_device(dev)            # replug
        c._key_callback(dev, _btn(1))          # a fresh short press...
        c._key_callback(dev, _btn(0))
        assert _until(lambda: dev.brightness == 30)   # ...works normally
    finally:
        c.stop()


def test_stop_cancels_inflight_holds(monkeypatch):
    monkeypatch.setattr(controller, "HOLD_THRESHOLD", 0.1)
    c, dev = _connected()
    kc = c.config.active_profile().pages[0].key(1)
    kc.hold_action = Action("brightness", {"mode": "set", "value": "77"})
    c._key_callback(dev, _btn(1))
    c.stop()
    assert c._holds == {}
    time.sleep(0.2)
    assert c.config.brightness != 77


def test_hotplug_add_cannot_double_open_during_try_open(monkeypatch):
    """0.8.1 audit: try_open (GUI reconnect) racing the hotplug listener's
    _on_added opened the same hidraw node twice — two live transports, every
    keypress dispatched twice, and the loser leaking as a zombie reader. The
    open path is serialized now: the loser must wait, see the winner's
    device, and skip."""
    import threading

    monkeypatch.setattr(controller, "FifineDeck", MockDevice)
    c = DeckController(DeckConfig())
    dev_a, dev_b = MockDevice(), MockDevice()
    inside, release = threading.Event(), threading.Event()

    real_open = MockDevice.open

    def slow_open(self):
        inside.set()
        release.wait(2)
        return real_open(self)

    monkeypatch.setattr(MockDevice, "open", slow_open)

    class _Mgr:
        def enumerate(self):
            return [dev_a]

    c.manager = _Mgr()
    c._running = True
    try:
        t_open = threading.Thread(target=c.try_open)
        t_open.start()
        assert inside.wait(2)              # A holds the open lock, mid-open
        t_add = threading.Thread(target=lambda: c._on_added(dev_b))
        t_add.start()
        time.sleep(0.15)
        assert not dev_b.opened            # B is blocked, not double-opening
        release.set()
        t_open.join(2)
        t_add.join(2)
        assert dev_a.opened and c.device is dev_a
        assert not dev_b.opened            # loser skipped: one transport only
    finally:
        release.set()
        c.stop()
