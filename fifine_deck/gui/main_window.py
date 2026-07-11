"""Main window: key grid + editor + profile/page controls + tray."""
from __future__ import annotations

import functools
import json
import os

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QSlider, QInputDialog, QMessageBox, QDockWidget,
    QSystemTrayIcon, QMenu, QStatusBar, QScrollArea, QFileDialog,
)

from .. import rendering, assets
from ..device import DEVICE_PROFILE
from ..model import DeckConfig, Profile, Page, KeyConfig, Action
from ..actions import default_icon_for
from ..controller import DeckController
from .widgets import KeyButton, ActionEditor, ActionCatalog, KnobEditor, ReorderDialog


class _Bridge(QObject):
    """Marshals controller callbacks (background threads) onto the GUI thread."""
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    keyEvent = pyqtSignal(int, bool)
    pageChanged = pyqtSignal()


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

        self.bridge = _Bridge()
        self.bridge.connected.connect(self._on_connected)
        self.bridge.disconnected.connect(self._on_disconnected)
        self.bridge.keyEvent.connect(self._on_key_event)
        self.bridge.pageChanged.connect(self._on_external_page_change)
        controller.on_connect = lambda dev: self.bridge.connected.emit()
        controller.on_disconnect = lambda: self.bridge.disconnected.emit()
        controller.on_key_event = lambda i, p: self.bridge.keyEvent.emit(i, p)
        controller.on_page_changed = lambda: self.bridge.pageChanged.emit()

        self._close_notified = False
        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._reload_profiles()
        self._rebuild_grid()

    def _build_menu(self):
        m = self.menuBar().addMenu("&App")
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
        # Start on login (hidden) toggle
        import os as _os
        from ..app import AUTOSTART_FILE, set_autostart
        self.autostart_act = QAction("Start on login (hidden)", self, checkable=True)
        self.autostart_act.setChecked(_os.path.exists(AUTOSTART_FILE))
        self.autostart_act.toggled.connect(lambda on: set_autostart(on))
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

    # -- config export / import -------------------------------------------
    def _export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export configuration", os.path.expanduser("~/fifine-deck-config.json"),
            "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w") as f:
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
            imported = DeckConfig.from_dict(data)
            if not imported.profiles:
                raise ValueError("no profiles in file")
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Import failed",
                                f"Not a valid configuration file:\n{e}")
            return
        if QMessageBox.question(
                self, "Import configuration",
                "Replace your current profiles, pages and settings with the "
                "imported ones?") != QMessageBox.StandardButton.Yes:
            return
        # Mutate the existing config object in place so the controller keeps its
        # reference.
        self.config.brightness = imported.brightness
        self.config.glow = imported.glow
        self.config.profiles = imported.profiles
        self.config.active_profile_id = imported.active_profile_id
        self.controller.page_index = 0
        self.glow_act.setChecked(self.config.glow)
        self.bright.setValue(self.config.brightness)
        self._reload_profiles()
        self._rebuild_grid()
        self.editor.clear()
        self.controller.apply_brightness()
        self.controller.render_page()
        self.config.save()
        self.statusBar().showMessage("Configuration imported", 4000)

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
        self.profile_combo = QComboBox()
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
        self.page_combo = QComboBox()
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

        # key grid, centered on a "device" panel
        self.grid_host = QWidget()
        self.grid_host.setObjectName("deckPanel")
        self.grid_host.setStyleSheet(
            "#deckPanel{background:#0d0d0d;border:1px solid #333;border-radius:18px;}")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12)
        self.grid.setContentsMargins(24, 24, 24, 24)
        center = QHBoxLayout()
        center.addStretch()
        center.addWidget(self.grid_host)
        center.addStretch()
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
        self._save_timer.timeout.connect(lambda: self.config.save())

    def _tray_host_present(self) -> bool:
        """Reliable tray check: is a StatusNotifier host actually on the bus?
        Qt's isSystemTrayAvailable() gives false positives on some Wayland
        compositors, which would trap the window on close-to-tray."""
        try:
            from PyQt6.QtDBus import QDBusConnection
            bus = QDBusConnection.sessionBus()
            iface = bus.interface()
            for name in ("org.kde.StatusNotifierWatcher",
                         "org.freedesktop.StatusNotifierWatcher"):
                reply = iface.isServiceRegistered(name)
                if reply.value():
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
            self.grid.addWidget(btn, r, c)
            self.buttons[idx] = btn
        self._refresh_all_previews()

    def _refresh_all_previews(self):
        page = self._page()
        for idx, btn in self.buttons.items():
            btn.update_preview(page.keys.get(idx, KeyConfig()))

    # -- model helpers -----------------------------------------------------
    def _profile(self) -> Profile:
        return self.config.active_profile()

    def _page(self) -> Page:
        pages = self._profile().pages
        i = min(self.controller.page_index, len(pages) - 1)
        return pages[i]

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
        for n, pg in enumerate(self._profile().pages):
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
            self.controller.page_index = 0
            self._reload_pages()
            self._refresh_all_previews()
            self.controller.render_page()
            self._queue_save()

    def _on_page_selected(self, i):
        if i < 0:
            return
        self.controller.page_index = i
        self._refresh_all_previews()
        self.controller.render_page()
        self.editor.clear()

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, "New profile", "Name:")
        if ok and name:
            p = Profile(name=name)
            self.config.profiles.append(p)
            self.config.active_profile_id = p.id
            self.controller.page_index = 0
            self._reload_profiles()
            self._refresh_all_previews()
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
            self.controller.page_index = 0
            self._reload_profiles()
            self._refresh_all_previews()
            self.controller.render_page()
            self._queue_save()

    def _add_page(self):
        prof = self._profile()
        prof.pages.append(Page(name="Page"))
        # jump to and show the new page
        self.controller.page_index = len(prof.pages) - 1
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _del_page(self):
        prof = self._profile()
        if len(prof.pages) <= 1:
            QMessageBox.information(self, "Delete page", "At least one page is required.")
            return
        del prof.pages[self.controller.page_index]
        self.controller.page_index = 0
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _reorder_pages(self):
        prof = self._profile()
        if len(prof.pages) < 2:
            return
        labels = [self.page_combo.itemText(i) for i in range(self.page_combo.count())]
        dlg = ReorderDialog("Reorder pages", labels, self)
        if not dlg.exec():
            return
        order = dlg.order()
        if order == list(range(len(prof.pages))):
            return
        current_id = prof.pages[self.controller.page_index].id
        prof.pages = [prof.pages[i] for i in order]
        self.controller.page_index = next(
            (i for i, p in enumerate(prof.pages) if p.id == current_id), 0)
        self._reload_pages()
        self._refresh_all_previews()
        self.controller.render_page()
        self._queue_save()

    def _reorder_profiles(self):
        if len(self.config.profiles) < 2:
            return
        labels = [p.name for p in self.config.profiles]
        dlg = ReorderDialog("Reorder profiles", labels, self)
        if not dlg.exec():
            return
        order = dlg.order()
        if order == list(range(len(self.config.profiles))):
            return
        self.config.profiles = [self.config.profiles[i] for i in order]
        self._reload_profiles()   # active profile tracked by id, stays selected
        self._queue_save()

    # -- editing -----------------------------------------------------------
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
                self.controller.device.refresh()
            except Exception:
                pass
        self._on_key_selected(dst)   # follow the key to its new slot
        self._queue_save()

    def _on_action_dropped(self, index: int, atype: str):
        kc = self._page().key(index)
        kc.action = Action(atype, {})
        icon_name, label = default_icon_for(kc.action)
        if icon_name and not kc.icon:
            kc.icon = assets.library_path(icon_name)
        if label and not kc.label:
            kc.label = label
        self.buttons[index].update_preview(kc)
        if self.controller.connected:
            self.controller.render_key(index)
            try:
                self.controller.device.refresh()
            except Exception:
                pass
        self._on_key_selected(index)
        self._queue_save()

    def _build_knobs(self):
        n = DEVICE_PROFILE.get("dial_count", 0)
        if n <= 0:
            return
        host = QWidget()
        hb = QHBoxLayout(host)
        page = self._page()
        for k in range(1, n + 1):
            ed = KnobEditor(k, page.knob(k))
            ed.changed.connect(self._queue_save)
            hb.addWidget(ed)
        hb.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        dock = QDockWidget("Knobs", self)
        dock.setWidget(scroll)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    def _on_editor_changed(self):
        if self.selected_index is None:
            return
        idx = self.selected_index
        self.buttons[idx].update_preview(self._page().key(idx))
        if self.controller.connected:
            self.controller.render_key(idx)
            try:
                self.controller.device.refresh()
            except Exception:
                pass
        self._queue_save()

    def _on_brightness(self, v):
        self.controller.set_brightness(v)
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

    def _on_external_page_change(self):
        # A key/knob action switched the page or profile on the controller.
        # Resync the profile/page combos and previews so GUI edits/deletes act
        # on the page actually shown on the device (not a stale selection).
        self._reload_profiles()          # also reloads pages from page_index
        self._refresh_all_previews()
        self.editor.clear()
        self.selected_index = None

    def _set_status(self):
        from ..actions import environment_summary
        state = "● connected" if self.controller.connected else "○ no device"
        fw = ""
        if self.controller.connected and self.controller.device:
            fw = f"  fw={self.controller.device.firmware_version}"
        self.statusBar().showMessage(f"{state}{fw}   |   {environment_summary()}")

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
                "• Quit completely: App → Quit  (Ctrl+Q).")

    def _quit(self):
        self.config.save()
        self.controller.stop()
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()


def config_brightness(cfg: DeckConfig) -> int:
    return int(cfg.brightness)
