"""Main window: key grid + editor + profile/page controls + tray."""
from __future__ import annotations

import json
import logging
import os

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QSlider, QInputDialog, QMessageBox, QDockWidget,
    QSystemTrayIcon, QMenu, QStatusBar, QScrollArea, QFileDialog, QCheckBox,
)

from .. import rendering, assets
from ..device import DEVICE_PROFILE
from ..model import (DeckConfig, Profile, Page, KeyConfig, Action, Folder,
                     _next_backup_path, _page_loss_summary)
from ..actions import default_icon_for
from ..controller import DeckController
from .widgets import (KeyButton, ActionEditor, ActionCatalog, KnobEditor,
                      ReorderDialog, _NoWheelWhenUnfocused, _protect_wheel)

log = logging.getLogger(__name__)


class _Bridge(QObject):
    """Marshals controller callbacks (background threads) onto the GUI thread."""
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    keyEvent = pyqtSignal(int, bool)
    pageChanged = pyqtSignal()
    brightnessChanged = pyqtSignal(int)
    monitorImage = pyqtSignal(int, object, str)   # (key index, PIL image, page id)


class MainWindow(QMainWindow):
    def __init__(self, config: DeckConfig, controller: DeckController):
        super().__init__()
        self.config = config
        self.controller = controller
        self.buttons: dict[int, KeyButton] = {}
        self.selected_index: int | None = None

        self.setWindowTitle("fifine Control Deck")
        self.resize(1000, 620)

        # Let action editors offer a profile dropdown for the "switch profile" action.
        from . import widgets as _widgets
        _widgets.PROFILES_PROVIDER = lambda: self.config.profiles
        # Let grid previews render monitor keys from the controller's live
        # sampler (last reading + history) instead of a static placeholder.
        _widgets.MONITOR_PREVIEW_PROVIDER = self._monitor_preview
        # Stamp key-drags with the page they started on, so a drop landing
        # after a mid-drag page switch can be rejected.
        _widgets.CURRENT_PAGE_ID_PROVIDER = lambda: self._page().id

        self.bridge = _Bridge()
        self.bridge.connected.connect(self._on_connected)
        self.bridge.disconnected.connect(self._on_disconnected)
        self.bridge.keyEvent.connect(self._on_key_event)
        self.bridge.pageChanged.connect(self._on_external_page_change)
        self.bridge.brightnessChanged.connect(self._on_brightness_changed)
        self.bridge.monitorImage.connect(self._on_monitor_image)
        controller.on_connect = lambda dev: self.bridge.connected.emit()
        controller.on_disconnect = lambda: self.bridge.disconnected.emit()
        controller.on_key_event = lambda i, p: self.bridge.keyEvent.emit(i, p)
        controller.on_page_changed = lambda: self.bridge.pageChanged.emit()
        # Queued through the bridge like every other controller callback: a
        # deck brightness key runs this on the SDK's reader thread.
        controller.on_brightness_changed = lambda v: self.bridge.brightnessChanged.emit(v)
        controller.on_monitor_image = \
            lambda i, img, page_id="": self.bridge.monitorImage.emit(i, img, page_id)

        self._close_notified = False
        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._reload_profiles()
        self._rebuild_grid()
        self._last_page_key = self._current_page_key()

    def _build_menu(self):
        m = self.menuBar().addMenu("&Options")
        hide_act = QAction("Hide to background", self)
        hide_act.setShortcut("Ctrl+W")
        hide_act.triggered.connect(self.close)
        show_min = QAction("Show / Raise window", self)
        show_min.triggered.connect(self.show_and_raise)
        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self._quit)
        m.addAction(hide_act)
        m.addAction(show_min)
        m.addSeparator()
        export_act = QAction("Export config…", self)
        export_act.triggered.connect(self._export_config)
        import_act = QAction("Import config…", self)
        import_act.triggered.connect(self._import_config)
        m.addAction(export_act)
        m.addAction(import_act)
        m.addSeparator()
        # Start on login (hidden): the autostart .desktop file's existence
        # is the state.
        import os as _os
        from ..app import autostart_file
        self.autostart_act = QAction("Start on login (hidden)", self, checkable=True)
        self.autostart_act.setChecked(_os.path.exists(autostart_file()))
        self.autostart_act.toggled.connect(self._set_autostart)
        m.addAction(self.autostart_act)
        # Glow-on-press toggle
        self.glow_act = QAction("Flash key on press", self, checkable=True)
        self.glow_act.setChecked(bool(self.config.glow))
        self.glow_act.toggled.connect(self._set_glow)
        m.addAction(self.glow_act)
        m.addSeparator()
        m.addAction(quit_act)

    def _set_glow(self, on: bool):
        self.config.glow = bool(on)
        self._queue_save()

    def _set_autostart(self, on: bool):
        from ..app import set_autostart
        set_autostart(on)

    def apply_autostart(self, enable: bool) -> bool:
        """Force the autostart entry to `enable` and resync the menu item.

        setChecked() alone was not enough for the delegated CLI path: it emits
        toggled — and therefore writes or removes the .desktop file — only when
        the value CHANGES, and that value is a snapshot taken once at window
        construction and never refreshed. So if the file had been removed behind
        the running GUI's back, the action still read True, --enable-autostart
        changed nothing, and the CLI printed success anyway.

        Apply first, then make the menu reflect what is actually on disk.
        Returns whether the file ended up in the requested state.
        """
        from ..app import autostart_file, set_autostart
        set_autostart(enable)
        on_disk = os.path.exists(autostart_file())
        self.autostart_act.blockSignals(True)     # or this re-enters _set_autostart
        try:
            self.autostart_act.setChecked(on_disk)
        finally:
            self.autostart_act.blockSignals(False)
        return on_disk == enable

    # -- config export / import -------------------------------------------
    def _config_has_cleartext_password(self) -> bool:
        """True if any action anywhere carries a literal password.

        Walks profiles, pages, folders (recursively) and knobs, including hold
        actions and multi-action steps, because a secret in any one of them ends
        up in the exported file just the same.
        """
        def step_has(step) -> bool:
            """A multi-action step, which is NOT a bare action dict.

            _StepRow.value() writes {"action": {...}, "delay": N}, and the
            executor reads it back as step.get("action", step) — see the "multi"
            branch of actions.execute. Reading step["params"] directly, as this
            did first, looks one level too shallow and therefore never saw a
            password nested in a Multi-action: the export warning silently did
            not fire for exactly the case it was written for. Mirror the
            executor's unwrapping so the two cannot drift apart.
            """
            if not isinstance(step, dict):
                return False
            inner = step.get("action", step)
            if not isinstance(inner, dict):
                return False
            params = inner.get("params") or {}
            if params.get("password"):
                return True
            # a step may itself be a multi-action
            return any(step_has(s) for s in (params.get("steps") or []))

        def action_has(a) -> bool:
            if a is None:
                return False
            if a.params.get("password"):
                return True
            return any(step_has(s) for s in (a.params.get("steps") or []))

        def scan_container(cont) -> bool:
            for page in getattr(cont, "pages", []):
                for kc in page.keys.values():
                    if action_has(kc.action) or action_has(kc.hold_action):
                        return True
                    if kc.folder is not None and scan_container(kc.folder):
                        return True
                # knobs hang off the Page, not the Profile
                for kn in page.knobs.values():
                    if any(action_has(getattr(kn, slot, None))
                           for slot in ("press", "left", "right")):
                        return True
            return False

        return any(scan_container(prof) for prof in self.config.profiles)

    def _export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export configuration", os.path.expanduser("~/fifine-deck-config.json"),
            "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        # A password key falls back to storing its secret in the config in
        # cleartext when no keyring is available, and to_dict copies params
        # verbatim — so the export carries it. 0600 protects other local users
        # of THIS machine, but the whole point of an export is to move it
        # somewhere else, where the mode does not follow it. Warn before
        # writing, not after.
        if self._config_has_cleartext_password():
            if QMessageBox.question(
                    self, "Export contains a password",
                    "One or more keys store a password in this configuration "
                    "in plain text, because no keyring was available when it "
                    "was set.\n\nThe exported file will contain that password "
                    "readable by anyone who opens it. It is written private to "
                    "you, but copying it to another machine, a backup or cloud "
                    "storage carries the password with it.\n\nExport anyway?") \
                    != QMessageBox.StandardButton.Yes:
                return
        try:
            # 0600 like DeckConfig.save: the config can hold a plaintext
            # password (the no-keyring fallback), and an export written with
            # the default umask would hand that secret to every local user.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)      # O_CREAT's mode is skipped on overwrite
            with os.fdopen(fd, "w") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        self.statusBar().showMessage(f"Exported configuration to {path}", 4000)

    def _import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import configuration", os.path.expanduser("~"), "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            if not DeckConfig.looks_like_config(data):
                raise ValueError("this file does not look like a deck configuration")
            imported = DeckConfig.from_dict(data)
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Import failed",
                                f"Not a valid configuration file:\n{e}")
            return
        from ..model import CONFIG_PATH
        backup = _next_backup_path(CONFIG_PATH + ".bak")
        if QMessageBox.question(
                self, "Import configuration",
                "Replace your current profiles, pages and settings with the "
                "imported ones?\n\nYour current configuration is backed up to\n"
                f"{backup}") != QMessageBox.StandardButton.Yes:
            return
        # Back up the current config before replacing it, and treat a failed
        # backup as a failed import. The dialog above PROMISES the backup, so
        # swallowing the error and replacing anyway is the one outcome the user
        # explicitly did not agree to: no backup, no warning, no way back.
        try:
            self.config.save(backup)
        except Exception as e:
            QMessageBox.warning(
                self, "Import cancelled",
                f"Your current configuration could not be backed up:\n{e}\n\n"
                f"Nothing was changed.")
            log.error("import aborted: backup to %s failed: %s", backup, e)
            return
        # Mutate the existing config object in place so the controller keeps its
        # reference.
        self.config.brightness = imported.brightness
        self.config.glow = imported.glow
        self.config.snap_hint_dismissed = imported.snap_hint_dismissed
        self.config.profiles = imported.profiles
        self.config.active_profile_id = imported.active_profile_id
        # reset_nav, not page_index=0: if we were inside a folder, _container
        # still points at a Folder from the profiles we just discarded. It is
        # unreachable from self.config, so every later edit would preview fine,
        # report success, and be dropped on the next save.
        self.controller.reset_nav()
        self.glow_act.setChecked(self.config.glow)
        self.bright.setValue(self.config.brightness)
        self._reload_profiles()
        self._rebuild_grid()
        self._deselect()
        self.controller.apply_brightness()
        self.controller.render_page()
        # Guarded like _autosave is. A bare save() here meant that on failure
        # the grid already showed the imported config, nothing had reached the
        # disk, this status line never ran, and the user saw only a traceback
        # on stderr — with in-memory and on-disk state silently diverged.
        try:
            self.config.save()
        except Exception as e:
            log.error("saving the imported config failed: %s", e)
            QMessageBox.warning(
                self, "Import not saved",
                f"The imported configuration is loaded, but could not be "
                f"written to disk:\n{e}\n\nYour previous configuration is at\n"
                f"{backup}")
            return
        self.statusBar().showMessage(f"Configuration imported (backup: {backup})", 6000)

    def show_and_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # top bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Profile:"))
        # Combos change value on hover-scroll unless filtered; switching the
        # profile or page moves what the physical deck is showing.
        self._nowheel = _NoWheelWhenUnfocused(self)
        self.profile_combo = _protect_wheel(QComboBox(), self._nowheel)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        bar.addWidget(self.profile_combo)
        for text, slot, tip in [("+", self._add_profile, "Add profile"),
                                ("Rename", self._rename_profile, "Rename profile"),
                                ("⇅", self._reorder_profiles, "Reorder profiles"),
                                ("–", self._del_profile, "Delete profile")]:
            b = QPushButton(text)
            b.setFixedWidth(70 if len(text) > 1 else 32)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)

        bar.addSpacing(20)
        bar.addWidget(QLabel("Page:"))
        self.page_combo = _protect_wheel(QComboBox(), self._nowheel)
        self.page_combo.currentIndexChanged.connect(self._on_page_selected)
        bar.addWidget(self.page_combo)
        for text, slot, tip in [("+", self._add_page, "Add page"),
                                ("⇅", self._reorder_pages, "Reorder pages"),
                                ("–", self._del_page, "Delete page")]:
            b = QPushButton(text)
            b.setFixedWidth(32)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)

        bar.addStretch()
        bar.addWidget(QLabel("Brightness"))
        self.bright = QSlider(Qt.Orientation.Horizontal)
        self.bright.setFixedWidth(150)
        self.bright.setRange(0, 100)
        self.bright.setValue(config_brightness(self.config))
        self.bright.valueChanged.connect(self._on_brightness)
        bar.addWidget(self.bright)
        root.addLayout(bar)

        # breadcrumb / folder navigation row
        crumb = QHBoxLayout()
        self.back_btn = QPushButton("⬅ Back")
        self.back_btn.setToolTip("Exit this folder")
        self.back_btn.clicked.connect(self._folder_back)
        self.back_btn.setVisible(False)
        crumb.addWidget(self.back_btn)
        self.breadcrumb = QLabel("")
        self.breadcrumb.setStyleSheet("color:#9a9a9a;")
        crumb.addWidget(self.breadcrumb)
        crumb.addStretch()
        root.addLayout(crumb)

        # key grid, centered on a "device" panel
        self.grid_host = QWidget()
        self.grid_host.setObjectName("deckPanel")
        self.grid_host.setStyleSheet(
            "#deckPanel{background:#0d0d0d;border:1px solid #333;border-radius:18px;}")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12)
        self.grid.setContentsMargins(24, 24, 24, 24)
        center = QHBoxLayout()
        # Align, don't stretch: given the whole central area the panel grows to
        # fill it and QGridLayout hands the slack to the rows, so the key rows
        # drift apart into big empty bands. Alignment keeps the panel at its
        # size hint (keys packed at `spacing`) and centres it instead.
        center.addWidget(self.grid_host, 0, Qt.AlignmentFlag.AlignCenter)
        wrap = QWidget()
        wrap.setLayout(center)
        root.addWidget(wrap, 1)

        self.setCentralWidget(central)

        # actions catalog dock (left) — drag onto a key
        self.catalog = ActionCatalog()
        cat_dock = QDockWidget("Actions", self)
        cat_dock.setWidget(self.catalog)
        cat_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                             QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        cat_dock.setMinimumWidth(180)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, cat_dock)

        # editor dock (right)
        self.editor = ActionEditor()
        self.editor.changed.connect(self._on_editor_changed)
        dock = QDockWidget("Key settings", self)
        dock.setWidget(self.editor)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        dock.setMinimumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        # knob editors (only for devices with dials)
        self._build_knobs()

        self.setStatusBar(QStatusBar())
        self._set_status()

        # save periodically + on change
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._autosave)

    def _tray_host_present(self) -> bool:
        """Reliable tray check: is a StatusNotifier host actually on the bus?
        Qt's isSystemTrayAvailable() gives false positives on some Wayland
        compositors, which would trap the window on close-to-tray."""
        try:
            from PyQt6.QtDBus import QDBusConnection
            bus = QDBusConnection.sessionBus()
            iface = bus.interface()
            if iface is not None:
                for name in ("org.kde.StatusNotifierWatcher",
                             "org.freedesktop.StatusNotifierWatcher"):
                    if iface.isServiceRegistered(name).value():
                        return True
        except Exception:
            pass
        return False

    def _build_tray(self):
        icon = self._app_icon()
        self.setWindowIcon(icon)
        # Only create a tray icon when a real host exists; otherwise closing
        # would hide the window with no way to restore it.
        self.tray = None
        # Opt-in: only build a tray when explicitly requested AND a real
        # StatusNotifier host is on the bus (avoids the close-to-tray trap and
        # D-Bus warnings on compositors without a tray host).
        import os as _os
        if _os.environ.get("FIFINE_TRAY") != "1" or not self._tray_host_present():
            return
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show = QAction("Show / Hide", self)
        show.triggered.connect(self._toggle_visible)
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._toggle_visible()
            if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip("fifine Control Deck")
        self.tray.show()

    def _app_icon(self) -> QIcon:
        from .. import assets
        if assets.app_icon_path():
            return QIcon(assets.app_icon_path())
        img = rendering.render_key(64, "fC", "", "#1551ff", "#ffffff")
        return QIcon(QPixmap.fromImage(rendering.pil_to_qimage(img)))

    # -- grid --------------------------------------------------------------
    def _rebuild_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.buttons = {}
        cols = DEVICE_PROFILE["cols"]
        count = DEVICE_PROFILE["key_count"]
        for idx in range(1, count + 1):
            r, c = divmod(idx - 1, cols)
            btn = KeyButton(idx)
            btn.selected.connect(self._on_key_selected)
            btn.actionDropped.connect(self._on_action_dropped)
            btn.keyMoved.connect(self._on_key_moved)
            btn.openFolder.connect(self._on_open_folder)
            self.grid.addWidget(btn, r, c)
            self.buttons[idx] = btn
        self._refresh_all_previews()
        self._update_breadcrumb()

    def _refresh_all_previews(self):
        page = self._page()
        for idx, btn in self.buttons.items():
            btn.update_preview(page.keys.get(idx, KeyConfig()))

    # -- model helpers -----------------------------------------------------
    def _profile(self) -> Profile:
        return self.config.active_profile()

    def _container(self):
        """Current page-holder: the active profile, or a folder if navigated in."""
        return self.controller.container()

    def _page(self) -> Page:
        return self.controller.page()

    # -- profile / page combo handling ------------------------------------
    def _reload_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p in self.config.profiles:
            self.profile_combo.addItem(p.name, p.id)
        i = self.profile_combo.findData(self.config.active_profile_id)
        self.profile_combo.setCurrentIndex(max(0, i))
        self.profile_combo.blockSignals(False)
        self._reload_pages()

    def _reload_pages(self):
        self.page_combo.blockSignals(True)
        self.page_combo.clear()
        for n, pg in enumerate(self._container().pages):
            # Number by position so entries are always unique and sequential
            # (stored names can be stale/duplicated after add/delete).
            label = f"Page {n + 1}"
            if pg.name and not pg.name.lower().startswith(("page", "main")):
                label = f"{n + 1}: {pg.name}"   # show custom names too
            self.page_combo.addItem(label, pg.id)
        self.page_combo.setCurrentIndex(min(self.controller.page_index,
                                            self.page_combo.count() - 1))
        self.page_combo.blockSignals(False)

    def _on_profile_selected(self, i):
        pid = self.profile_combo.itemData(i)
        if pid:
            self.config.active_profile_id = pid
            self.controller.reset_nav()      # exit any folder to the profile root
            self._reload_pages()
            self._reload_knobs()
            self._refresh_all_previews()
            self._update_breadcrumb()
            self.controller.render_page()
            self._queue_save()

    def _on_page_selected(self, i):
        if i < 0:
            return
        self.controller.page_index = i
        self._reload_knobs()
        self._refresh_all_previews()
        self.controller.render_page()
        self._deselect()

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, "New profile", "Name:")
        if ok and name:
            p = Profile(name=name)
            self.config.profiles.append(p)
            self.config.active_profile_id = p.id
            # reset_nav, not page_index=0: if we were inside a folder, the
            # controller still holds that folder from the OLD profile, so every
            # read (previews, page list, edits) would go to the old profile
            # while the combo shows the new one. Then render so the deck shows
            # the profile we just switched to. Same as _del_profile.
            self.controller.reset_nav()
            self._reload_profiles()
            self._refresh_all_previews()
            self.controller.render_page()
            self._queue_save()

    def _rename_profile(self):
        p = self._profile()
        name, ok = QInputDialog.getText(self, "Rename profile", "Name:", text=p.name)
        if ok and name:
            p.name = name
            self._reload_profiles()
            self._queue_save()

    def _del_profile(self):
        if len(self.config.profiles) <= 1:
            QMessageBox.information(self, "Delete profile", "At least one profile is required.")
            return
        p = self._profile()
        if QMessageBox.question(self, "Delete profile", f"Delete '{p.name}'?") \
                == QMessageBox.StandardButton.Yes:
            self.config.profiles.remove(p)
            self.config.active_profile_id = self.config.profiles[0].id
            self.controller.reset_nav()
            self._reload_profiles()
            self._refresh_all_previews()
            self.controller.render_page()
            self._queue_save()

    def _add_page(self):
        cont = self._container()
        cont.pages.append(Page(name="Page"))
        # jump to and show the new page
        self.controller.page_index = len(cont.pages) - 1
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _del_page(self):
        cont = self._container()
        if len(cont.pages) <= 1:
            QMessageBox.information(self, "Delete page", "At least one page is required.")
            return
        page = cont.pages[self.controller.page_index]
        # Ask before destroying work. The "–" button is a 32 px square right
        # beside "+" and "⇅", one misclick takes every key on the page plus any
        # folder tree hanging off it at unlimited depth, and the autosave 600 ms
        # later makes it permanent. _del_profile next door already asks.
        #
        # An untouched page is not work, so deleting one keeps costing nothing.
        loss = _page_loss_summary(page)
        if loss and QMessageBox.question(
                self, "Delete page",
                f"Delete '{page.name}'?\n\nThis removes {loss}.\n"
                f"It cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        # Under the controller's lock: controller.page() clamps page_index
        # against len(pages) and then indexes, so between the del and the
        # reassignment below a reader (the SDK reader thread dispatching a key
        # press, the monitor thread) could clamp against the old length and
        # index the shortened list. The lock page() takes is an RLock on the
        # controller, and this is the one mutation of pages that ran outside it.
        with self.controller._lock:
            del cont.pages[self.controller.page_index]
            self.controller.page_index = 0
        # The editor/knob panels may be bound to the page we just deleted.
        # The async page-change resync can't be relied on to clear them:
        # deleting the page at index 0 keeps (container, page_index) equal,
        # so it sees "no change" — and edits would then flow into the deleted
        # Page's objects and silently vanish on restart (0.8.1 audit).
        self._deselect()
        self._last_page_key = None       # force the next resync to treat this as a change
        self._reload_knobs()
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _reorder_pages(self):
        cont = self._container()
        if len(cont.pages) < 2:
            return
        labels = [self.page_combo.itemText(i) for i in range(self.page_combo.count())]
        dlg = ReorderDialog("Reorder pages", labels, self)
        try:
            if not dlg.exec():
                return
            order = dlg.order()
        finally:
            # Parented to the long-lived MainWindow, so without this every
            # click retains a hidden dialog and its populated list widget for
            # the process lifetime — the same leak already fixed explicitly for
            # IconLibraryDialog and QColorDialog. Cancel leaked one too.
            dlg.deleteLater()
        if order == list(range(len(cont.pages))):
            return
        current_id = cont.pages[self.controller.page_index].id
        cont.pages = [cont.pages[i] for i in order]
        self.controller.page_index = next(
            (i for i, p in enumerate(cont.pages) if p.id == current_id), 0)
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _reorder_profiles(self):
        if len(self.config.profiles) < 2:
            return
        labels = [p.name for p in self.config.profiles]
        dlg = ReorderDialog("Reorder profiles", labels, self)
        try:
            if not dlg.exec():
                return
            order = dlg.order()
        finally:
            dlg.deleteLater()               # see _reorder_pages
        if order == list(range(len(self.config.profiles))):
            return
        self.config.profiles = [self.config.profiles[i] for i in order]
        self._reload_profiles()   # active profile tracked by id, stays selected
        self._queue_save()

    # -- editing -----------------------------------------------------------
    def _deselect(self):
        """Drop the key selection everywhere it is visible.

        The editor and the grid are two halves of one piece of state, and only
        the editor half was ever reset: every caller cleared the panel but left
        the key button drawn with its blue :checked border, so after a page or
        profile switch the grid claimed a key was selected while the panel said
        "No key selected" — the same contradiction 0.10.0 fixes inside the
        panel. Two of the four callers also left selected_index pointing at the
        old key. One helper so a new caller cannot get half of it right.
        """
        for b in self.buttons.values():
            b.setChecked(False)
        self.editor.clear()
        self.selected_index = None

    def _on_key_selected(self, index: int):
        self.selected_index = index
        for i, b in self.buttons.items():
            b.setChecked(i == index)
        kc = self._page().key(index)
        self.editor.set_key(kc, index)

    def _on_key_moved(self, src: int, dst: int):
        """Swap two keys' configs (drag one key onto another to rearrange)."""
        if src == dst:
            return
        page = self._page()
        a = page.keys.get(src, KeyConfig())
        b = page.keys.get(dst, KeyConfig())
        page.keys[src] = b
        page.keys[dst] = a
        for i in (src, dst):
            self.buttons[i].update_preview(page.keys.get(i, KeyConfig()))
            if self.controller.connected:
                self.controller.render_key(i)
        if self.controller.connected:
            try:
                self.controller.refresh()
            except Exception:
                pass
        self._on_key_selected(dst)   # follow the key to its new slot
        self._queue_save()

    def _on_action_dropped(self, index: int, atype: str):
        kc = self._page().key(index)
        # Capture the OLD action's defaults first: an icon/label that still
        # matches them was auto-assigned by a previous drop and should follow
        # the new action; anything else was the user's choice and stays.
        # (The old guard was `not kc.icon`, so a second drop onto a key kept
        # the first action's identity forever.)
        old_icon_name, old_label = default_icon_for(kc.action)
        old_auto_icon = assets.library_ref(old_icon_name)
        kc.action = Action(atype, {})
        if atype == "switch_profile" and self.config.profiles:
            # Materialize the target the editor will display: leaving it
            # empty made the dropped key a silent no-op until some unrelated
            # edit happened to write the combo's selection back.
            kc.action.params["profile_id"] = self.config.profiles[0].id
        icon_name, label = default_icon_for(kc.action)
        if kc.icon in ("", old_auto_icon):
            kc.icon = assets.library_ref(icon_name)
        if kc.label in ("", old_label):
            kc.label = label
        self._ensure_folder(kc)
        self.buttons[index].update_preview(kc)
        if self.controller.connected:
            self.controller.render_key(index)
            self.controller.refresh()
        self._on_key_selected(index)
        self._queue_save()

    # -- folders -----------------------------------------------------------
    def _ensure_folder(self, kc: KeyConfig):
        """Create folder content (a page with a Back key) for a folder key.

        A folder is never discarded when the action type changes: it just goes
        dormant, ignored while the action isn't open_folder, and comes back
        intact if the key is made a folder again. This used to drop kc.folder —
        every nested page and key — the instant the type changed, with no
        confirmation and an autosave 600ms later. There is no undo, and a
        folder is not recoverable by re-selecting open_folder: that mints a new
        empty one. KeyConfig.to_dict/from_dict keep `folder` regardless of
        action type, so a dormant folder survives a restart.
        """
        if kc.action.type == "open_folder" and kc.folder is None:
            page = Page(name="Main")
            last = DEVICE_PROFILE["key_count"]
            back = page.key(last)
            back.label = "Back"
            back.icon = assets.library_ref("prev_page")
            back.bg_color = "#26262c"
            back.action = Action("folder_back", {})
            kc.folder = Folder(name=kc.label or "Folder", pages=[page])

    def _on_open_folder(self, index: int):
        """Double-clicked a key: if it's a folder, navigate into it."""
        kc = self._page().keys.get(index)
        if kc and kc.action.type == "open_folder":
            created = kc.folder is None
            self._ensure_folder(kc)
            if created:
                # _ensure_folder just materialized real model content (a page
                # with a Back key) — persist it like every other model change,
                # or it exists only until the app closes.
                self._queue_save()
            self.controller.enter_folder(kc.folder)
            # (controller fires on_page_changed -> _on_external_page_change resync)

    def _folder_back(self):
        self.controller.go_back()

    def _folder_path(self):
        """Folder objects from the profile root down to the current container."""
        folders = [c for c, _ in self.controller._nav if c is not None]
        if not self.controller.at_root():
            folders.append(self.controller.container())
        return folders

    def _update_breadcrumb(self):
        inside = not self.controller.at_root()
        self.back_btn.setVisible(inside)
        if inside:
            crumbs = [self.controller.profile().name] + \
                     [f.name for f in self._folder_path()]
            self.breadcrumb.setText("  ▸  ".join(crumbs))
        else:
            self.breadcrumb.setText("")

    def _build_knobs(self):
        n = DEVICE_PROFILE.get("dial_count", 0)
        if n <= 0:
            return
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._knob_scroll = scroll
        self._reload_knobs()
        dock = QDockWidget("Knobs", self)
        dock.setWidget(scroll)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    def _reload_knobs(self):
        """(Re)bind the knob editors to the CURRENT page's KnobConfigs. They
        were built once against the startup page, so after any page or
        profile switch they kept showing — and writing into — the old page's
        knobs."""
        if getattr(self, "_knob_scroll", None) is None:
            return
        host = QWidget()
        hb = QHBoxLayout(host)
        page = self._page()
        for k in range(1, DEVICE_PROFILE.get("dial_count", 0) + 1):
            ed = KnobEditor(k, page.knob(k))
            ed.changed.connect(self._queue_save)
            hb.addWidget(ed)
        hb.addStretch()
        self._knob_scroll.setWidget(host)   # replaces + deletes the old host

    def _on_editor_changed(self):
        if self.selected_index is None:
            return
        idx = self.selected_index
        self._ensure_folder(self._page().key(idx))
        self.buttons[idx].update_preview(self._page().key(idx))
        if self.controller.connected:
            self.controller.render_key(idx)
            self.controller.refresh()
        self._queue_save()

    def _on_brightness(self, v):
        self.controller.set_brightness(v)
        self._queue_save()

    def _on_brightness_changed(self, v: int):
        """Brightness changed from the deck: follow it with the slider.

        blockSignals, or setValue re-enters _on_brightness and pushes the value
        straight back at the device. Queue a save here too — the deck path had
        none, so a brightness set from the deck was lost on restart.
        """
        if self.bright.value() == v:
            return
        self.bright.blockSignals(True)
        try:
            self.bright.setValue(v)
        finally:
            self.bright.blockSignals(False)
        self._queue_save()

    # -- controller callbacks (GUI thread via bridge) ---------------------
    def _on_connected(self):
        self._set_status()
        self.bright.setValue(self.config.brightness)
        self.controller.render_page()

    def _on_disconnected(self):
        self._set_status()

    def _on_key_event(self, index: int, pressed: bool):
        b = self.buttons.get(index)
        if b:
            b.flash(pressed)

    def _monitor_preview(self, kc, size: int):
        """Render a monitor key's grid preview from the controller's sampler.
        Reads the last cached reading (dict lookups only — safe against the
        monitor thread), so previews are live even with no device connected."""
        from .. import monitors
        spec = monitors.MonitorSpec.from_params(kc.action.params)
        sampler = self.controller._sampler
        return monitors.render_monitor(size, spec, sampler.last(spec),
                                       sampler.history(spec),
                                       kc.bg_color, kc.text_color)

    def _on_monitor_image(self, index: int, img, page_id: str = ""):
        """Live monitor frame from the controller thread (via the bridge)."""
        btn = self.buttons.get(index)
        kc = self._page().keys.get(index)
        # The frame may be stale — queued before a page switch or an edit and
        # delivered after. Check both the key's intent and the page it was
        # rendered for.
        if btn is None or kc is None or kc.action.type != "monitor":
            return
        if page_id and page_id != self._page().id:
            return
        btn.setIcon(QIcon(QPixmap.fromImage(rendering.pil_to_qimage(img))))

    def _current_page_key(self) -> tuple:
        """Identity of the page the GUI is showing (container + page)."""
        return (id(self.controller.container()), self.controller.page_index)

    def _on_external_page_change(self):
        # The controller re-rendered. Only treat it as a page/profile CHANGE
        # when the visible page actually changed: a device reconnect or a
        # same-page re-render must not wipe the user's selection mid-edit
        # (or mid-dialog — the Library/File picker's result was silently
        # discarded when a hotplug landed while it was open).
        changed = self._current_page_key() != getattr(self, "_last_page_key", None)
        self._last_page_key = self._current_page_key()
        self._reload_profiles()          # also reloads pages from page_index
        if changed:
            self._reload_knobs()
        self._refresh_all_previews()
        self._update_breadcrumb()
        if changed:
            self._deselect()
            # A profile switched FROM THE DECK mutates config.active_profile_id
            # and nothing armed the debounced save, so the choice was lost on
            # anything but a clean quit. Same gap the brightness path already
            # closed for the deck's brightness keys. The page index is not
            # persisted at all by design, so only a profile change is worth a
            # write.
            if self.config.active_profile_id != getattr(
                    self, "_last_saved_profile_id", self.config.active_profile_id):
                self._queue_save()
        self._last_saved_profile_id = self.config.active_profile_id

    def _set_status(self):
        from ..actions import environment_summary
        state = "● connected" if self.controller.connected else "○ no device"
        fw = ""
        if self.controller.connected and self.controller.device:
            fw = f"  fw={self.controller.device.firmware_version}"
        # A deck that is plugged in but cannot be opened is a different problem
        # from one that is absent, and both used to read "○ no device". Say
        # which, when the controller knows.
        reason = ""
        if not self.controller.connected and getattr(self.controller, "last_error", ""):
            state = "⚠ deck not usable"
            reason = f"  {self.controller.last_error}"
        self.statusBar().showMessage(
            f"{state}{fw}{reason}   |   {environment_summary()}")

    def maybe_show_snap_hint(self):
        """Under a snap, if the deck isn't actually usable, guide the user to fix
        device access. For the classic snap this offers a one-click button that
        installs the udev rule via pkexec and reconnects; otherwise it shows text
        guidance. "Not usable" includes a connected-but-empty-firmware handle (the
        libusb false-connect a locked-out snap gets before the rule is present)."""
        from ..actions import (snap_usb_hint, can_install_udev_rule,
                                install_udev_rule_pkexec)
        hint = snap_usb_hint()
        # "Working" means connected AND we actually read the firmware. A
        # locked-out snap can enumerate the deck over libusb (connected, keys)
        # while hidraw I/O is denied, leaving firmware empty — treat that as not
        # working, or the fix (and the Enable-device-access button) would never
        # appear for the very user who needs it.
        dev = self.controller.device
        working = bool(self.controller.connected
                       and dev is not None and dev.firmware_version)
        if not hint or working or self.config.snap_hint_dismissed:
            return
        log.info("device not usable under snap (fw empty?) — showing access hint")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("No device detected")
        box.setText("The fifine Control Deck was not detected.")
        box.setInformativeText(hint)
        dont_show = QCheckBox("Don't show this again")
        box.setCheckBox(dont_show)
        fix_btn = None
        if can_install_udev_rule():
            fix_btn = box.addButton("Enable device access",
                                    QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if dont_show.isChecked():
            self.config.snap_hint_dismissed = True
            self._queue_save()
        if fix_btn is not None and box.clickedButton() is fix_btn:
            ok, msg = install_udev_rule_pkexec()
            if ok and self.controller.try_open():
                QMessageBox.information(self, "Device access",
                                        "Connected — your deck is ready.")
            elif ok:
                QMessageBox.information(self, "Device access",
                    "Rule installed, but the deck still isn't detected — try "
                    "unplugging and replugging it.")
            else:
                QMessageBox.warning(self, "Device access", msg)
            self._set_status()

    # -- misc --------------------------------------------------------------
    def _queue_save(self):
        self._save_timer.start()

    def _toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, e):
        # Closing never quits: the deck keeps working in the background.
        # A tray (if present) or relaunching the command reopens the window.
        self.hide()
        e.ignore()
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage("fifine Control Deck",
                                  "Still running in the tray. Right-click to quit.",
                                  self._app_icon(), 3000)
        elif not self._close_notified:
            self._close_notified = True
            QMessageBox.information(
                self, "Running in the background",
                "fifine Control Deck keeps running so your keys stay active.\n\n"
                "• Re-open this window: launch “fifine Control Deck” again "
                "(or run 'fifine-control-deck').\n"
                "• Quit completely: Options → Quit  (Ctrl+Q).")

    def _autosave(self):
        """Persist the config; a failure (disk full, permissions) must be
        VISIBLE — silently dropping the user's edits is the worst outcome —
        and must not crash the timer."""
        try:
            self.config.save()
        except Exception as e:
            log.error("autosave failed: %s", e)
            self.statusBar().showMessage(
                f"⚠ Could not save configuration: {e}", 10000)

    def _quit(self):
        try:
            self.config.save()
        except Exception as e:
            # Still quit cleanly: a failing save must not leave the app
            # running with the controller half-stopped (Ctrl+Q previously
            # aborted here, stopping nothing).
            log.error("save on quit failed: %s", e)
        # Take the UI down BEFORE the teardown, so quit looks immediate.
        #
        # controller.stop() blocks ~2.0 s, and profiling attributes essentially
        # all of it to transport_destroy inside the prebuilt libtransport.so —
        # a vendored binary we cannot patch (every other phase measures 0.00 s;
        # the read-thread join is 0.09 s). Running that on the Qt thread meant
        # the window sat there frozen for two seconds after the user asked to
        # quit, which reads as a hang.
        #
        # The teardown still runs to completion, synchronously, before
        # QApplication.quit() — it must, because it clears the key LCDs and
        # releases the device. Only the window goes first.
        from PyQt6.QtWidgets import QApplication
        self.hide()
        if self.tray is not None:
            self.tray.hide()
        QApplication.processEvents()      # actually paint the disappearance
        self.controller.stop()
        QApplication.quit()


def config_brightness(cfg: DeckConfig) -> int:
    return int(cfg.brightness)
