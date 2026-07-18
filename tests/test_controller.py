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
