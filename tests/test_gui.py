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
    # BOTH halves of the identity, separately: the old `icon or label`
    # disjunction was blind to an icon-only regression (audit finding).
    assert kc.icon == mw.assets.library_ref("volume_up")
    assert kc.label == "Volume"


def test_selecting_a_key_tracks_the_selection(win):
    w, cfg, c = win
    w._on_key_selected(4)
    assert w.selected_index == 4


def test_external_page_change_clears_a_stale_selection(win):
    """A key action can switch page on the device. The GUI must drop its
    selection AND detach the editor, or a later edit would land on the
    wrong page's key (the docstring's hazard, now asserted directly)."""
    w, cfg, c = win
    w._on_key_selected(2)
    old_kc = c.page().key(2)
    w.editor.label_edit.setText("before switch")
    assert old_kc.label == "before switch"
    cfg.active_profile().pages.append(mw.Page(name="P2"))
    c.page_index = 1                      # the page REALLY changes
    c.on_page_changed()
    QApplication.processEvents()
    assert w.selected_index is None
    # the load-bearing half: an edit after the switch must go nowhere
    w.editor.label_edit.setText("after switch")
    assert old_kc.label == "before switch"


def test_same_page_rerender_keeps_the_selection(win):
    """A device reconnect re-renders the SAME page; wiping the selection and
    editor mid-edit (or mid-dialog) discarded the user's in-flight work
    (audit finding). Only a real page change may clear."""
    w, cfg, c = win
    w._on_key_selected(2)
    c.on_page_changed()                   # same page object, same index
    QApplication.processEvents()
    assert w.selected_index == 2
    assert w.editor._kc is c.page().key(2)


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


# -- folders survive an action-type change -----------------------------------

def test_changing_a_folder_keys_action_keeps_the_folder(win):
    """It used to drop kc.folder — every nested page and key — the moment the
    type changed, with no confirmation and an autosave 600ms later. There is no
    undo, and re-selecting open_folder mints a NEW empty folder rather than
    restoring the old one."""
    w, cfg, c = win
    kc = cfg.active_profile().pages[0].key(1)
    kc.action = mw.Action("open_folder", {})
    w._ensure_folder(kc)
    folder = kc.folder
    assert folder is not None
    folder.pages.append(mw.Page(name="Macros"))          # user builds it out

    kc.action = mw.Action("volume", {"cmd": "mute"})     # a stray scroll does this
    w._ensure_folder(kc)

    assert kc.folder is folder, "the folder and its pages were destroyed"
    assert [p.name for p in kc.folder.pages] == ["Main", "Macros"]


def test_restoring_the_folder_action_restores_the_same_folder(win):
    w, cfg, c = win
    kc = cfg.active_profile().pages[0].key(1)
    kc.action = mw.Action("open_folder", {})
    w._ensure_folder(kc)
    folder = kc.folder

    kc.action = mw.Action("none", {})
    w._ensure_folder(kc)
    kc.action = mw.Action("open_folder", {})
    w._ensure_folder(kc)

    assert kc.folder is folder, "a new empty folder was minted"


def test_a_dormant_folder_survives_a_save_and_reload(win, tmp_path):
    """Keeping the folder in memory is only half of it — KeyConfig must still
    serialize it while the action isn't open_folder."""
    w, cfg, c = win
    kc = cfg.active_profile().pages[0].key(1)
    kc.action = mw.Action("open_folder", {})
    w._ensure_folder(kc)
    kc.folder.pages.append(mw.Page(name="Macros"))
    kc.action = mw.Action("none", {})                    # dormant
    w._ensure_folder(kc)

    path = tmp_path / "c.json"
    cfg.save(str(path))
    from fifine_deck.model import DeckConfig
    reloaded = DeckConfig.load(str(path)).active_profile().pages[0].key(1)
    assert reloaded.folder is not None
    assert [p.name for p in reloaded.folder.pages] == ["Main", "Macros"]


def test_the_explicit_clear_button_does_delete_the_folder(win):
    """Dormancy is for implicit action-type changes only. Pressing Clear key
    is stated intent to wipe the key: keeping the folder would render a blank
    key that silently resurrects old pages when a folder action is later
    dropped onto it."""
    w, cfg, c = win
    kc = cfg.active_profile().pages[0].key(1)
    kc.action = mw.Action("open_folder", {})
    w._ensure_folder(kc)
    assert kc.folder is not None

    kc.label = "L"; kc.icon = "lib:star"
    kc.bg_color = "#111111"; kc.text_color = "#eeeeee"
    w.editor.set_key(kc, 1)
    w.editor.clear_btn.click()            # the REAL button, not the method

    default = mw.KeyConfig()
    assert kc.folder is None
    assert kc.action.type == "none"
    assert kc.label == default.label
    assert kc.icon == default.icon
    assert kc.bg_color == default.bg_color
    assert kc.text_color == default.text_color


def test_hover_scrolling_the_step_delay_spinbox_cannot_change_it(qapp):
    """The multi-step editor pins its scroll area to a fixed height, so
    hover-scrolling past the per-step delay spinbox is routine — and an
    unfocused spinbox eats the wheel and rewrites the timing."""
    from PyQt6.QtCore import QPoint, QPointF, Qt as QtCore_Qt
    from PyQt6.QtGui import QWheelEvent
    from fifine_deck.gui.widgets import ActionParamsWidget

    w = ActionParamsWidget()
    w.set_action(mw.Action("multi", {"steps": [
        {"action": {"type": "text", "params": {"text": "x"}}, "delay": 1.0},
    ]}))
    row = w._multi_editor._rows[0]
    assert row.delay.value() == 1.0
    assert not row.delay.hasFocus()

    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                     QtCore_Qt.MouseButton.NoButton, QtCore_Qt.KeyboardModifier.NoModifier,
                     QtCore_Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(row.delay, ev)

    assert row.delay.value() == 1.0, "hover-scroll changed the step delay"


def test_hover_scrolling_a_combo_cannot_change_it(qapp):
    """Qt lets an unfocused QComboBox eat wheel events and change value, so
    scrolling the editor panel silently rewrote whatever combo was under the
    cursor — which on the action combo destroyed the key's folder."""
    from PyQt6.QtCore import QPoint, QPointF, Qt as QtCore_Qt
    from PyQt6.QtGui import QWheelEvent
    from fifine_deck.gui.widgets import ActionParamsWidget

    w = ActionParamsWidget()
    w.set_action(mw.Action("open_folder", {}))
    combo = w.type_combo
    before = combo.currentIndex()
    assert not combo.hasFocus()

    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                     QtCore_Qt.MouseButton.NoButton, QtCore_Qt.KeyboardModifier.NoModifier,
                     QtCore_Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(combo, ev)

    assert combo.currentIndex() == before, "hover-scroll changed the action type"


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


# ---------------------------------------------------------------------------
# Icon library: picking an icon must stick (user-reported, 0.6.0)
# ---------------------------------------------------------------------------
def _editor_with_key(action_type, params, icon_name):
    from fifine_deck.gui.widgets import ActionEditor
    from fifine_deck.model import KeyConfig, Action
    from fifine_deck import assets
    kc = KeyConfig()
    kc.action = Action(action_type, dict(params))
    kc.icon = assets.library_ref(icon_name)
    ed = ActionEditor()
    ed.set_key(kc, 1)
    return ed, kc


@pytest.mark.parametrize("atype,params", [
    ("launch_app", {"command": "obs"}),
    ("volume", {"cmd": "up"}),      # optional-field type: the one that hid bug 2
])
def test_library_icon_choice_is_not_overwritten(qapp, atype, params):
    """Picking an icon in the Library dialog set icon_edit, which re-ran the
    'icon follows the action' logic and instantly restored the action's
    default icon — so choosing any library icon appeared to do nothing."""
    from fifine_deck import assets
    ed, kc = _editor_with_key(atype, params, "home")
    ed.icon_edit.setText(assets.library_ref("star"))     # what _pick_library does
    assert ed.icon_edit.text() == assets.library_ref("star")
    assert kc.icon == assets.library_ref("star")


def test_icon_still_follows_a_changed_action(qapp):
    """The auto-icon feature itself must survive the fix: switching the
    volume sub-command still swaps the (unmodified) library icon."""
    from fifine_deck import assets
    ed, kc = _editor_with_key("volume", {"cmd": "up"}, "volume_up")
    combo = ed.params._params["cmd"]
    combo.setCurrentIndex(combo.findText("mute"))
    assert kc.action.params["cmd"] == "mute"
    assert kc.icon == assets.library_ref("mute")


def test_custom_file_icon_is_never_auto_replaced(qapp, tmp_path):
    from fifine_deck.gui.widgets import ActionEditor
    from fifine_deck.model import KeyConfig, Action
    custom = str(tmp_path / "mine.png")
    kc = KeyConfig()
    kc.action = Action("volume", {"cmd": "up"})
    kc.icon = custom
    ed = ActionEditor()
    ed.set_key(kc, 1)
    combo = ed.params._params["cmd"]
    combo.setCurrentIndex(combo.findText("mute"))
    assert kc.icon == custom


@pytest.mark.parametrize("atype,params", [
    ("launch_app", {"command": "obs"}),
    ("volume", {"cmd": "up"}),
])
def test_editing_label_does_not_touch_a_chosen_icon(qapp, atype, params):
    from fifine_deck import assets
    ed, kc = _editor_with_key(atype, params, "star")
    ed.label_edit.setText("Streaming")
    assert kc.icon == assets.library_ref("star")
    assert kc.label == "Streaming"


@pytest.mark.parametrize("atype,params", [
    ("launch_app", {"command": "obs"}),
    ("run_command", {"command": "ls"}),
    ("open_url", {"url": "https://x.dev"}),
    ("hotkey", {"keys": "ctrl+c"}),
    ("text", {"text": "hi"}),
    ("media", {"cmd": "play-pause"}),
    ("volume", {"cmd": "up"}),          # has an optional "step" field
    ("brightness", {"mode": "up"}),     # has an optional "value" field
    ("close_app", {"target": "obs"}),
    ("goto_page", {"page": "2"}),
    ("next_page", {}),
    ("sleep_screen", {}),
])
def test_first_library_pick_sticks_for_every_action_type(qapp, atype, params):
    """The FIRST pick after selecting a key is the case that broke: the editor
    materializes every field of an action type, so a stored action with
    optional fields omitted looked 'changed' on the first edit and the icon
    was replaced by the action default. Cover every type, first pick."""
    from fifine_deck.gui.widgets import ActionEditor
    from fifine_deck.model import KeyConfig, Action
    from fifine_deck import assets
    kc = KeyConfig()
    kc.action = Action(atype, dict(params))
    kc.icon = assets.library_ref("home")
    ed = ActionEditor()
    ed.set_key(kc, 1)
    ed.icon_edit.setText(assets.library_ref("star"))     # first pick
    assert kc.icon == assets.library_ref("star"), f"{atype}: first pick reverted"
    # action untouched — type AND every original param (the old type-only
    # assertion was blind to param rewrites; audit finding)
    assert kc.action.type == atype
    for k, v in params.items():
        assert kc.action.params.get(k) == v


def test_consecutive_library_picks_all_stick(qapp):
    from fifine_deck.gui.widgets import ActionEditor
    from fifine_deck.model import KeyConfig, Action
    from fifine_deck import assets
    kc = KeyConfig()
    kc.action = Action("volume", {"cmd": "up"})
    kc.icon = assets.library_ref("volume_up")
    ed = ActionEditor()
    ed.set_key(kc, 1)
    for name in ("star", "folder", "web", "lock"):
        ed.icon_edit.setText(assets.library_ref(name))
        assert kc.icon == assets.library_ref(name), f"pick {name} reverted"


# ===========================================================================
# Editor-audit regressions (pre-0.6.2 round: 33 confirmed findings)
# ===========================================================================
from fifine_deck import assets as _assets                   # noqa: E402
from fifine_deck.model import Action, KeyConfig, Page       # noqa: E402


def _pick_from_real_library_dialog(monkeypatch, editor, avoid=""):
    """Drive editor._pick_library() through a REAL IconLibraryDialog, clicking
    a real tile button (audit: the old tests only simulated the picker with
    icon_edit.setText, so the dialog path itself was never executed)."""
    from PyQt6.QtWidgets import QToolButton
    from fifine_deck.gui import widgets as wid
    picked = {}

    class Driver(wid.IconLibraryDialog):
        def exec(self):
            for b in self.findChildren(QToolButton):
                b.click()                      # real tile: sets chosen + accept
                if self.chosen != avoid:
                    break
            picked["icon"] = self.chosen
            return 1

    monkeypatch.setattr(wid, "IconLibraryDialog", Driver)
    editor._pick_library()
    return picked["icon"]


def test_real_dialog_pick_survives_typing_the_command(win, monkeypatch):
    """THE user-reported bug, third form (critical audit finding): pick an
    icon in the Library dialog, then type the command — the natural order.
    Asserted end-to-end: real dialog, model, and the bytes on the device."""
    from fifine_deck import rendering as R
    w, cfg, c = win
    dev = MockDevice()
    assert c._setup_device(dev)
    w._on_action_dropped(1, "launch_app")
    w._on_key_selected(1)
    kc = c.page().key(1)
    icon = _pick_from_real_library_dialog(monkeypatch, w.editor, avoid=kc.icon)
    assert kc.icon == icon
    w.editor.params._params["command"].setText("gimp")
    assert kc.icon == icon, "typing the command reverted the picked icon"
    assert kc.action.params["command"] == "gimp"
    expected = R.render_key(dev.KEY_PIXEL_WIDTH, kc.label, icon,
                            kc.bg_color, kc.text_color)
    assert dev.key_images[1].tobytes() == expected.tobytes(), \
        "device shows a different icon than the one picked"


@pytest.mark.parametrize("atype,param,val", [
    ("launch_app", "command", "firefox"),
    ("run_command", "command", "ls -la"),
    ("open_url", "url", "https://x.dev"),
    ("hotkey", "keys", "ctrl+alt+t"),
    ("close_app", "target", "obs"),
    ("volume", "step", "7"),
    ("brightness", "value", "15"),
    ("goto_page", "page", "2"),
    ("monitor", "interval", "5"),
])
def test_picked_icon_survives_param_edits_for_every_type(qapp, atype, param, val):
    from fifine_deck.gui.widgets import ActionEditor
    kc = KeyConfig()
    kc.action = Action(atype, {})
    kc.icon = _assets.library_ref("heart") or _assets.library_ref("star")
    picked = kc.icon
    ed = ActionEditor()
    ed.set_key(kc, 1)
    widget = ed.params._params[param]
    if hasattr(widget, "setPlainText"):
        widget.setPlainText(val)
    elif hasattr(widget, "setText"):
        widget.setText(val)
    else:
        widget.setCurrentText(val)
    assert kc.icon == picked, f"{atype}: editing '{param}' reverted the icon"


def test_second_drop_reskins_an_auto_identity(win):
    """An untouched auto icon/label must follow a second dropped action; the
    old `not kc.icon` guard kept the first action's identity forever."""
    w, cfg, c = win
    w._on_action_dropped(2, "volume")
    kc = c.page().key(2)
    assert (kc.icon, kc.label) == (_assets.library_ref("volume_up"), "Volume")
    w._on_action_dropped(2, "media")
    assert kc.action.type == "media"
    assert kc.icon == _assets.library_ref("play")
    assert kc.label == "Media"


def test_user_identity_survives_a_second_drop(win):
    w, cfg, c = win
    w._on_action_dropped(2, "volume")
    kc = c.page().key(2)
    kc.icon = "/home/me/custom.png"
    kc.label = "My Key"
    w._on_action_dropped(2, "media")
    assert kc.action.type == "media"
    assert kc.icon == "/home/me/custom.png"
    assert kc.label == "My Key"


def test_cleared_icon_stays_cleared(qapp):
    """Clearing the icon with the × is a choice; the next action edit used to
    resurrect the auto icon (audit finding)."""
    from fifine_deck.gui.widgets import ActionEditor
    kc = KeyConfig()
    kc.action = Action("volume", {"cmd": "up"})
    kc.icon = _assets.library_ref("volume_up")
    ed = ActionEditor()
    ed.set_key(kc, 1)
    ed.icon_edit.setText("")                       # the × button does this
    assert kc.icon == ""
    combo = ed.params._params["cmd"]
    combo.setCurrentIndex(combo.findText("mute"))  # action edit afterwards
    assert kc.icon == "", "auto icon came back after an explicit clear"


def test_auto_icon_clears_when_action_has_no_default(qapp):
    """Switching an auto-skinned key to an action with no default icon
    (monitor) must not leave the previous action's icon behind."""
    from fifine_deck.gui.widgets import ActionEditor
    kc = KeyConfig()
    kc.action = Action("volume", {"cmd": "up"})
    kc.icon = _assets.library_ref("volume_up")      # untouched auto icon
    ed = ActionEditor()
    ed.set_key(kc, 1)
    tc = ed.params.type_combo
    tc.setCurrentIndex(tc.findData("monitor"))
    assert kc.action.type == "monitor"
    assert kc.icon == "", f"stale auto icon left behind: {kc.icon!r}"


def test_selecting_a_password_key_has_no_side_effects(qapp, monkeypatch):
    """set_key baselines the action; for password keys that used to write the
    keyring and could pop the cleartext modal on mere SELECTION."""
    from fifine_deck import secret_store
    from fifine_deck.gui import widgets as wid
    from fifine_deck.gui.widgets import ActionEditor
    calls = []
    monkeypatch.setattr(secret_store, "store",
                        lambda *a: calls.append(a) or True)
    monkeypatch.setattr(secret_store, "new_id", lambda: "sid-new")
    monkeypatch.setattr(wid, "_PLAINTEXT_WARNED", False)
    kc = KeyConfig()
    kc.action = Action("password", {"password": "hunter2"})
    ed = ActionEditor()
    ed.set_key(kc, 1)                               # selection only
    assert calls == [], "selection wrote to the keyring"
    assert wid._PLAINTEXT_WARNED is False
    # …while a real edit still stores:
    ed.params._params["password"].setText("hunter3")
    assert calls, "a real edit no longer reaches the keyring"


def test_unknown_choice_value_round_trips(qapp):
    """A stored combo value this build doesn't know (config from a newer
    version) used to be silently replaced by the first option."""
    from fifine_deck.gui.widgets import ActionEditor
    kc = KeyConfig()
    kc.action = Action("media", {"cmd": "chapter-next"})   # future value
    ed = ActionEditor()
    ed.set_key(kc, 1)
    ed.label_edit.setText("edited")                        # unrelated edit
    assert kc.action.params["cmd"] == "chapter-next"


def test_unknown_action_type_round_trips(qapp):
    """An action type from a newer build must survive unrelated edits, not be
    downgraded to 'none' with its params destroyed."""
    from fifine_deck.gui.widgets import ActionEditor
    kc = KeyConfig()
    kc.action = Action("hologram", {"depth": "3"})         # future type
    ed = ActionEditor()
    ed.set_key(kc, 1)
    ed.label_edit.setText("edited")
    assert kc.action.type == "hologram"
    assert kc.action.params == {"depth": "3"}


def test_deleted_profile_target_is_preserved(win):
    """A switch_profile key whose target was deleted used to snap to the
    first profile on any unrelated edit."""
    w, cfg, c = win
    kc = c.page().key(5)
    kc.action = Action("switch_profile", {"profile_id": "gone-123"})
    w._on_key_selected(5)
    w.editor.label_edit.setText("edited")
    assert kc.action.params["profile_id"] == "gone-123"


def test_dropped_switch_profile_key_works_immediately(win):
    """The drop used to store empty params, making the key a no-op until an
    unrelated edit materialized the combo's selection."""
    w, cfg, c = win
    w._on_action_dropped(6, "switch_profile")
    kc = c.page().key(6)
    assert kc.action.params.get("profile_id") == cfg.profiles[0].id


def test_import_preserves_snap_hint_dismissed(win, monkeypatch, tmp_path):
    import json as _json
    w, cfg, c = win
    donor = DeckConfig()
    donor.snap_hint_dismissed = True
    path = tmp_path / "donor.json"
    path.write_text(_json.dumps(donor.to_dict()))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    _AutoBox.answer = QMessageBox.StandardButton.Yes
    w._import_config()
    assert cfg.snap_hint_dismissed is True


def test_double_click_created_folder_is_queued_for_save(win):
    w, cfg, c = win
    kc = c.page().key(7)
    kc.action = Action("open_folder", {})
    assert kc.folder is None
    w._on_open_folder(7)
    assert kc.folder is not None
    assert w._save_timer.isActive(), "folder content created but never saved"


def test_undecodable_gif_falls_back_to_a_static_render(tmp_path):
    """A file named .gif that can't be decoded used to freeze the key forever:
    marked animated, never painted (audit finding)."""
    bad = tmp_path / "broken.gif"
    bad.write_bytes(b"this is not a gif at all")

    class GifRejectingDevice(MockDevice):
        def set_key_gif(self, index, path):
            return -1                       # what the backend really returns

    cfg = DeckConfig()
    kc = cfg.active_profile().pages[0].key(1)
    kc.icon = str(bad)
    kc.label = "X"
    c = DeckController(cfg)
    dev = GifRejectingDevice()
    try:
        assert c._setup_device(dev)
        assert 1 in dev.key_images, "key never painted after gif failure"
        assert 1 not in c._gif_keys, "failed gif still registered as animated"
    finally:
        c.stop()


def test_stale_monitor_frame_for_another_page_is_dropped(win):
    from PIL import Image
    w, cfg, c = win
    kc = c.page().key(3)
    kc.action = Action("monitor", {"metric": "cpu"})
    applied = []
    w.buttons[3].setIcon = lambda *a: applied.append(a)     # record repaints
    img = Image.new("RGB", (64, 64))
    w._on_monitor_image(3, img, "some-other-page-id")
    assert applied == [], "frame for another page repainted the preview"
    w._on_monitor_image(3, img, c.page().id)
    assert applied, "frame for the current page was wrongly dropped"


def test_drop_from_a_stale_page_is_rejected(win):
    """The drag payload carries the page it started on; a drop landing after
    a mid-drag page switch must not rearrange the new page (audit finding)."""
    from PyQt6.QtCore import QMimeData
    from fifine_deck.gui.widgets import MIME_KEY

    w, cfg, c = win
    moved = []
    w.buttons[2].keyMoved.connect(lambda s, d: moved.append((s, d)))

    class Ev:
        def __init__(self, mime): self._m = mime
        def mimeData(self): return self._m
        def acceptProposedAction(self): pass
        def ignore(self): pass

    stale = QMimeData()
    stale.setData(MIME_KEY, b"1:not-the-current-page")
    w.buttons[2].dropEvent(Ev(stale))
    assert moved == []

    fresh = QMimeData()
    fresh.setData(MIME_KEY, f"1:{c.page().id}".encode())
    w.buttons[2].dropEvent(Ev(fresh))
    assert moved == [(1, 2)]


def test_autosave_failure_is_visible_and_quit_still_stops(win, monkeypatch):
    w, cfg, c = win
    def boom(path=None):
        raise OSError("disk full")
    monkeypatch.setattr(cfg, "save", boom)
    w._autosave()                                   # must not raise
    assert "Could not save" in w.statusBar().currentMessage()
    stopped = []
    monkeypatch.setattr(c, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: None))
    w._quit()                                       # must not raise either
    assert stopped, "quit aborted before stopping the controller"


# -- autostart toggle (0.7.0) -------------------------------------------------

def test_autostart_denial_reverts_the_toggle(win, monkeypatch):
    """A Background-portal denial must not leave the menu claiming autostart
    is on — the toggle reverts and the status bar says why."""
    w, cfg, c = win
    monkeypatch.setattr("fifine_deck.app.set_autostart", lambda on, config=None: 1)
    # baseline: unchecked, regardless of this machine's real autostart file
    w.autostart_act.blockSignals(True)
    w.autostart_act.setChecked(False)
    w.autostart_act.blockSignals(False)
    w.autostart_act.setChecked(True)              # user toggles on
    assert not w.autostart_act.isChecked()        # denied -> reverted
    assert cfg.autostart_enabled is False
    assert "denied" in w.statusBar().currentMessage().lower()


def test_autostart_grant_under_flatpak_is_persisted(win, monkeypatch):
    """The portal has no query API, so a granted request must be recorded on
    the live config AND queued for saving — that's what re-checks the toggle
    on the next launch. The real set_autostart runs (only the portal call is
    mocked) so this pins the product's persistence, not a stub's."""
    w, cfg, c = win
    monkeypatch.setattr("fifine_deck.app._portal_autostart", lambda enable: True)
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    saves = []
    monkeypatch.setattr(w, "_queue_save", lambda: saves.append(1))
    w.autostart_act.blockSignals(True)
    w.autostart_act.setChecked(False)             # machine-independent baseline
    w.autostart_act.blockSignals(False)
    w.autostart_act.setChecked(True)
    assert cfg.autostart_enabled is True
    assert saves, "granted state was never queued for saving"
    w.autostart_act.setChecked(False)             # the toggle-OFF path too
    assert cfg.autostart_enabled is False
    assert len(saves) == 2


def test_autostart_toggle_restores_flatpak_state_on_next_launch(qapp, monkeypatch):
    """Construction-time state: under Flatpak the toggle must read the
    persisted config flag (the portal cannot be queried). This is the
    'next launch' half of the feature."""
    monkeypatch.setattr(mw, "QMessageBox", _AutoBox)   # dialogs must not block
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    cfg = DeckConfig(autostart_enabled=True)
    c = DeckController(cfg)
    w = mw.MainWindow(cfg, c)
    try:
        assert w.autostart_act.isChecked()
    finally:
        w.close(); c.stop(); QApplication.processEvents()
    cfg2 = DeckConfig(autostart_enabled=False)
    c2 = DeckController(cfg2)
    w2 = mw.MainWindow(cfg2, c2)
    try:
        assert not w2.autostart_act.isChecked()
    finally:
        w2.close(); c2.stop(); QApplication.processEvents()


def test_autostart_toggle_reads_the_entry_file_outside_flatpak(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", _AutoBox)   # dialogs must not block
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from fifine_deck.app import autostart_file
    cfg = DeckConfig()
    c = DeckController(cfg)
    w = mw.MainWindow(cfg, c)
    try:
        assert not w.autostart_act.isChecked()    # no entry file yet
    finally:
        w.close(); c.stop(); QApplication.processEvents()
    import os as _os
    _os.makedirs(_os.path.dirname(autostart_file()), exist_ok=True)
    open(autostart_file(), "w").write("[Desktop Entry]\n")
    cfg2 = DeckConfig()
    c2 = DeckController(cfg2)
    w2 = mw.MainWindow(cfg2, c2)
    try:
        assert w2.autostart_act.isChecked()
    finally:
        w2.close(); c2.stop(); QApplication.processEvents()
