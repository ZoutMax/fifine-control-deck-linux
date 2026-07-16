"""MainWindow: thread marshalling, editing, and the snap device-access hint.

Runs against real Qt widgets on the offscreen platform (same as the CI smoke
test). The GUI had no unit coverage at all, yet it owns two things that have
already broken in the field: the boundary where device events cross from the
SDK's reader thread onto the Qt thread, and the snap access hint.
"""
import threading

import pytest

pytest.importorskip("PyQt6")
controller_mod = pytest.importorskip("fifine_deck.controller")

from PyQt6.QtWidgets import QApplication, QMessageBox     # noqa: E402

from fifine_deck import actions                            # noqa: E402
from fifine_deck.controller import DeckController          # noqa: E402
from fifine_deck.gui import main_window as mw              # noqa: E402
from fifine_deck.model import DeckConfig                   # noqa: E402
from tests.test_controller import MockDevice               # noqa: E402


class _AutoBox(QMessageBox):
    """A QMessageBox that never blocks: exec() returns immediately, and it can
    report a button (matched by text) as clicked and tick its checkbox."""

    instances: list = []
    infos: list = []
    warns: list = []
    questions: list = []
    click_text = None
    tick = False
    answer = QMessageBox.StandardButton.No     # reply to question()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _AutoBox.instances.append(self)

    def exec(self):
        cb = self.checkBox()
        if _AutoBox.tick and cb is not None:
            cb.setChecked(True)
        return 0

    def clickedButton(self):
        if _AutoBox.click_text:
            for b in self.buttons():
                if _AutoBox.click_text in b.text():
                    return b
        return None

    # The static helpers build and exec their own modal, which would block the
    # test run forever — every one the GUI uses must be stubbed here.
    @staticmethod
    def information(parent, title, text, *a, **k):
        _AutoBox.infos.append(text)
        return QMessageBox.StandardButton.Ok

    @staticmethod
    def warning(parent, title, text, *a, **k):
        _AutoBox.warns.append(text)
        return QMessageBox.StandardButton.Ok

    @staticmethod
    def critical(parent, title, text, *a, **k):
        _AutoBox.warns.append(text)
        return QMessageBox.StandardButton.Ok

    @staticmethod
    def question(parent, title, text, *a, **k):
        _AutoBox.questions.append(text)
        return _AutoBox.answer


@pytest.fixture
def win(qapp, monkeypatch):
    """A real MainWindow with a real controller (no device opened)."""
    _AutoBox.instances = []
    _AutoBox.infos = []
    _AutoBox.warns = []
    _AutoBox.questions = []
    _AutoBox.click_text = None
    _AutoBox.tick = False
    _AutoBox.answer = QMessageBox.StandardButton.No
    monkeypatch.setattr(mw, "QMessageBox", _AutoBox)

    cfg = DeckConfig()
    c = DeckController(cfg)
    w = mw.MainWindow(cfg, c)
    yield w, cfg, c
    w.close()
    c.stop()
    QApplication.processEvents()


# -- construction ------------------------------------------------------------

def test_window_builds_a_button_for_every_key(win):
    w, cfg, c = win
    from fifine_deck.device import DEVICE_PROFILE
    assert set(w.buttons) == set(range(1, DEVICE_PROFILE["key_count"] + 1))


# -- the thread boundary -----------------------------------------------------

def test_device_key_events_are_queued_onto_the_qt_thread(win):
    """The SDK delivers key events on its own reader thread. Touching a widget
    from there is undefined behaviour, so the controller callback only emits a
    signal, which Qt queues onto the GUI thread.

    Asserting it has NOT run before processEvents() is the whole point: if the
    bridge were ever replaced with a direct call, the flash would happen inline
    on the reader thread and this test would fail.
    """
    w, cfg, c = win
    flashes = []
    w.buttons[1].flash = lambda pressed: flashes.append(pressed)

    t = threading.Thread(target=lambda: c.on_key_event(1, True))
    t.start()
    t.join()

    assert flashes == []                 # queued, not executed on the emitter
    QApplication.processEvents()
    assert flashes == [True]             # delivered on the Qt thread


def test_connect_and_disconnect_callbacks_survive_a_background_thread(win):
    """on_connect/on_disconnect fire from the hotplug listener thread."""
    w, cfg, c = win
    c.device = MockDevice()
    for cb in (c.on_connect, c.on_disconnect):
        t = threading.Thread(target=lambda cb=cb: cb(c.device) if cb is c.on_connect else cb())
        t.start()
        t.join()
    QApplication.processEvents()         # must not raise


def test_unknown_key_index_is_ignored(win):
    """A stray index from the device must not raise on the GUI thread."""
    w, cfg, c = win
    c.on_key_event(999, True)
    QApplication.processEvents()


# -- editing -----------------------------------------------------------------

def test_dropping_an_action_binds_it_with_a_default_icon(win):
    w, cfg, c = win
    w._on_action_dropped(3, "volume")
    kc = cfg.active_profile().pages[0].key(3)
    assert kc.action.type == "volume"
    assert kc.icon or kc.label        # a dropped action is given an identity


def test_selecting_a_key_tracks_the_selection(win):
    w, cfg, c = win
    w._on_key_selected(4)
    assert w.selected_index == 4


def test_external_page_change_clears_a_stale_selection(win):
    """A key action can switch page on the device. The GUI must drop its
    selection, or a later edit would land on the wrong page's key."""
    w, cfg, c = win
    w._on_key_selected(2)
    assert w.selected_index == 2
    c.on_page_changed()
    QApplication.processEvents()
    assert w.selected_index is None


# -- profile / config switches must reset folder navigation ------------------
#
# container() returns controller._container whenever it is non-None, so a stale
# folder silently redirects EVERY later read: the page list, the previews, the
# breadcrumb, what render_page() pushes to the deck, and where edits land. If
# that folder belongs to a profile that is gone, edits go to an object no longer
# reachable from config and are dropped on the next save — while the GUI reports
# success. _del_profile and _on_profile_selected always called reset_nav();
# these two paths didn't.

def _enter_a_folder(cfg, c):
    """Navigate the controller into a folder of the active profile."""
    from fifine_deck.model import Folder
    folder = Folder(name="Media")
    cfg.active_profile().pages[0].key(1).folder = folder
    c.enter_folder(folder)
    assert c.container() is folder
    return folder


def test_adding_a_profile_leaves_the_folder_of_the_old_one(win, monkeypatch):
    w, cfg, c = win
    old_folder = _enter_a_folder(cfg, c)
    monkeypatch.setattr(mw.QInputDialog, "getText", lambda *a, **k: ("Streaming", True))

    w._add_profile()

    assert c.container() is not old_folder      # not the discarded profile's
    assert c.container() is cfg.active_profile()
    assert c.at_root()
    assert cfg.active_profile().name == "Streaming"


def test_adding_a_profile_renders_the_new_one_to_the_deck(win, monkeypatch):
    """Otherwise the deck keeps showing the previous profile's keys, so it lies
    about what pressing them will do."""
    w, cfg, c = win
    c.device = MockDevice()
    monkeypatch.setattr(mw.QInputDialog, "getText", lambda *a, **k: ("Streaming", True))
    rendered = []
    monkeypatch.setattr(c, "render_page", lambda: rendered.append(True))

    w._add_profile()
    assert rendered, "the deck was never re-rendered for the new profile"


def test_importing_a_config_drops_navigation_into_the_discarded_one(win, monkeypatch, tmp_path):
    w, cfg, c = win
    orphan = _enter_a_folder(cfg, c)

    # A valid config file to import.
    from fifine_deck.model import DeckConfig
    other = DeckConfig()
    other.active_profile().name = "Imported"
    path = tmp_path / "other.json"
    other.save(str(path))

    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName",
                        lambda *a, **k: (str(path), "JSON (*.json)"))
    _AutoBox.answer = QMessageBox.StandardButton.Yes      # confirm the replace

    w._import_config()

    assert c.container() is not orphan
    assert c.container() is cfg.active_profile()
    assert c.at_root()
    assert cfg.active_profile().name == "Imported"


# -- snap device-access hint -------------------------------------------------

def _snap(monkeypatch, hint="udev rule guidance", can_install=False):
    monkeypatch.setattr(actions, "snap_usb_hint", lambda: hint)
    monkeypatch.setattr(actions, "can_install_udev_rule", lambda: can_install)


def test_no_hint_outside_a_snap(win, monkeypatch):
    _snap(monkeypatch, hint=None)
    w, cfg, c = win
    w.maybe_show_snap_hint()
    assert _AutoBox.instances == []


def test_no_hint_when_the_deck_actually_works(win, monkeypatch):
    _snap(monkeypatch)
    w, cfg, c = win
    c.device = MockDevice()              # firmware_version == "MOCK"
    w.maybe_show_snap_hint()
    assert _AutoBox.instances == []


def test_hint_shown_for_a_false_connected_device(win, monkeypatch):
    """The bug this guards: a snap locked out of hidraw still enumerates the
    deck over libusb, so `connected` is True while firmware is empty. Keying
    the check off `connected` alone hid the hint from the very user who needed
    it."""
    _snap(monkeypatch)
    w, cfg, c = win
    dev = MockDevice()
    dev.firmware_version = ""
    c.device = dev
    assert c.connected is True           # …yet not usable
    w.maybe_show_snap_hint()
    assert len(_AutoBox.instances) == 1


def test_hint_shown_when_no_device_at_all(win, monkeypatch):
    _snap(monkeypatch)
    w, cfg, c = win
    w.maybe_show_snap_hint()
    assert len(_AutoBox.instances) == 1


def test_dismissed_hint_is_never_shown_again(win, monkeypatch):
    _snap(monkeypatch)
    w, cfg, c = win
    cfg.snap_hint_dismissed = True
    w.maybe_show_snap_hint()
    assert _AutoBox.instances == []


def test_ticking_dont_show_again_persists(win, monkeypatch):
    _snap(monkeypatch)
    w, cfg, c = win
    _AutoBox.tick = True
    w.maybe_show_snap_hint()
    assert cfg.snap_hint_dismissed is True


def test_no_enable_button_when_the_installer_is_unavailable(win, monkeypatch):
    """Strict snap / non-snap: offering a button that cannot work is worse
    than plain guidance."""
    _snap(monkeypatch, can_install=False)
    w, cfg, c = win
    w.maybe_show_snap_hint()
    box = _AutoBox.instances[0]
    assert not any("Enable device access" in b.text() for b in box.buttons())


def test_enable_button_offered_in_the_classic_snap(win, monkeypatch):
    _snap(monkeypatch, can_install=True)
    w, cfg, c = win
    w.maybe_show_snap_hint()
    box = _AutoBox.instances[0]
    assert any("Enable device access" in b.text() for b in box.buttons())


def test_enable_button_installs_the_rule_then_reconnects(win, monkeypatch):
    """The one-click flow: pkexec the helper, then re-open the device so the
    keys work immediately without a relaunch."""
    calls = []
    _snap(monkeypatch, can_install=True)
    monkeypatch.setattr(actions, "install_udev_rule_pkexec",
                        lambda: (calls.append("pkexec"), (True, "installed"))[1])
    w, cfg, c = win
    monkeypatch.setattr(c, "try_open", lambda: (calls.append("try_open"), True)[1])
    _AutoBox.click_text = "Enable device access"

    w.maybe_show_snap_hint()

    assert calls == ["pkexec", "try_open"]
    assert any("ready" in t for t in _AutoBox.infos)


def test_cancelled_auth_does_not_claim_success(win, monkeypatch):
    calls = []
    _snap(monkeypatch, can_install=True)
    monkeypatch.setattr(actions, "install_udev_rule_pkexec",
                        lambda: (False, "Authentication was cancelled."))
    w, cfg, c = win
    monkeypatch.setattr(c, "try_open", lambda: calls.append("try_open") or True)
    _AutoBox.click_text = "Enable device access"

    w.maybe_show_snap_hint()

    assert calls == []                                  # no pointless reconnect
    assert not any("ready" in t for t in _AutoBox.infos)
    assert _AutoBox.warns == ["Authentication was cancelled."]


def test_ok_without_clicking_enable_installs_nothing(win, monkeypatch):
    calls = []
    _snap(monkeypatch, can_install=True)
    monkeypatch.setattr(actions, "install_udev_rule_pkexec",
                        lambda: calls.append("pkexec") or (True, ""))
    w, cfg, c = win
    _AutoBox.click_text = None            # user just dismissed the dialog
    w.maybe_show_snap_hint()
    assert calls == []
