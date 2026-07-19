"""Reusable widgets: key button, action editor, action catalog, knob editor."""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QMimeData, QPoint, QObject, QEvent
from PyQt6.QtGui import QPixmap, QColor, QIcon, QDrag
from PyQt6.QtWidgets import (
    QToolButton, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QComboBox, QPushButton, QColorDialog, QFileDialog,
    QDoubleSpinBox, QDialog, QScrollArea, QGridLayout, QListWidget,
    QListWidgetItem, QAbstractItemView, QFrame, QApplication, QMessageBox,
)

from typing import Callable

from .. import rendering, assets
from ..actions import ACTION_TYPES, ACTION_CATALOG
from ..model import KeyConfig, KnobConfig, Action

log = logging.getLogger(__name__)


class _NoWheelWhenUnfocused(QObject):
    """Event filter that stops a combo box changing value on hover-scroll.

    Qt lets a QComboBox consume wheel events without ever being focused, so
    scrolling a panel silently rewrites whatever combo happens to be under the
    cursor. On the action combo that is destructive — it changes what a key
    does, and used to take the key's whole folder with it.
    """

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Wheel and not obj.hasFocus():
            ev.ignore()
            return True                 # swallow it; let the panel scroll
        return False


def _protect_wheel(widget, filt: QObject):
    """Make a wheel-sensitive editor (combo, spinbox) only respond to the
    wheel once deliberately focused."""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.installEventFilter(filt)
    return widget

# Injected by the main window so action editors can offer a profile dropdown for
# the "switch profile" action (avoids a widgets -> main_window import cycle).
PROFILES_PROVIDER: Callable[[], object] | None = None

# Injected by the main window: renders a monitor key's preview from the
# controller's sampler (last reading + history), so grid previews show real
# values even with no device connected and never regress to the placeholder
# on rebuilds. Returns a PIL image, or None to fall back to the placeholder.
MONITOR_PREVIEW_PROVIDER: Callable[[KeyConfig, int], object] | None = None

# Injected by the main window: the id of the page currently shown in the
# grid. Stamped into key-drag payloads so a drop that lands after the page
# changed underneath the drag (an action can switch pages mid-drag) is
# rejected instead of rearranging the wrong page's keys.
CURRENT_PAGE_ID_PROVIDER: Callable[[], str] | None = None

# One cleartext-fallback warning per app run (see _warn_plaintext_once).
_PLAINTEXT_WARNED = False

MIME_ACTION = "application/x-fifine-action"
MIME_KEY = "application/x-fifine-key"    # dragging a key to rearrange it


# ---------------------------------------------------------------------------
# Icon library picker
# ---------------------------------------------------------------------------
class IconLibraryDialog(QDialog):
    """Grid of built-in icons grouped by category; returns the chosen path."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Icon library")
        self.resize(520, 460)
        self.chosen = ""
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        vbox = QVBoxLayout(host)
        items = assets.load_library()
        cats: dict[str, list] = {}
        for it in items:
            cats.setdefault(it["category"], []).append(it)
        for cat in sorted(cats):
            lbl = QLabel(cat)
            lbl.setStyleSheet("font-weight:bold;color:#9a9a9a;margin-top:8px;")
            vbox.addWidget(lbl)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setSpacing(6)
            for i, it in enumerate(cats[cat]):
                b = QToolButton()
                b.setFixedSize(74, 90)
                b.setIconSize(QSize(56, 56))
                b.setIcon(QIcon(it["file"]))
                b.setText(it["label"])
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                b.setStyleSheet("QToolButton{border:1px solid #333;border-radius:8px;"
                                "background:#1f1f1f;font-size:10px;}"
                                "QToolButton:hover{border-color:#409eff;}")
                b.clicked.connect(lambda _, n=it["name"]: self._pick(assets.library_ref(n)))
                grid.addWidget(b, i // 6, i % 6)
            vbox.addWidget(grid_host)
        vbox.addStretch()
        scroll.setWidget(host)
        root.addWidget(scroll)

    def _pick(self, path):
        self.chosen = path
        self.accept()


class ReorderDialog(QDialog):
    """Drag the rows to reorder them. `order()` returns the new arrangement as
    a list of the original indices."""
    def __init__(self, title: str, labels: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(320, 360)
        v = QVBoxLayout(self)
        hint = QLabel("Drag items to reorder, then OK.")
        hint.setStyleSheet("color:#9a9a9a;")
        v.addWidget(hint)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for i, lbl in enumerate(labels):
            it = QListWidgetItem(lbl)
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.list.addItem(it)
        v.addWidget(self.list)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        ok = QPushButton("OK"); ok.clicked.connect(self.accept)
        row.addWidget(cancel); row.addWidget(ok)
        v.addLayout(row)

    def order(self) -> list[int]:
        return [it.data(Qt.ItemDataRole.UserRole)
                for r in range(self.list.count())
                if (it := self.list.item(r)) is not None]


# ---------------------------------------------------------------------------
# Key button (grid cell) — selectable + accepts dropped actions
# ---------------------------------------------------------------------------
class KeyButton(QToolButton):
    selected = pyqtSignal(int)
    actionDropped = pyqtSignal(int, str)   # (index, action_type)
    keyMoved = pyqtSignal(int, int)        # (source_index, target_index) swap
    openFolder = pyqtSignal(int)           # double-click to enter a folder key

    def __init__(self, index: int, size: int = 96):
        super().__init__()
        self.index = index
        self._size = size
        self.setCheckable(True)
        self.setFixedSize(size + 12, size + 12)
        self.setIconSize(QSize(size, size))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setAcceptDrops(True)
        self._base_qss = (
            "QToolButton{border:2px solid #333;border-radius:10px;background:#0b0b12;}"
            "QToolButton:checked{border:2px solid #1551ff;}"
            "QToolButton:hover{border-color:#409eff;}")
        self.setStyleSheet(self._base_qss)
        self.clicked.connect(lambda: self.selected.emit(self.index))
        self._press_pos: QPoint | None = None

    def update_preview(self, kc: KeyConfig):
        if kc.action.type == "monitor":
            # Preferably the controller's live values (works offline too);
            # placeholder only when no provider is wired (bare widget tests).
            provider = globals().get("MONITOR_PREVIEW_PROVIDER")
            img = provider(kc, self._size) if provider else None
            if img is None:
                from .. import monitors
                spec = monitors.MonitorSpec.from_params(kc.action.params)
                img = monitors.render_monitor(self._size, spec,
                                              monitors.placeholder(spec),
                                              [], kc.bg_color, kc.text_color)
            self.setIcon(QIcon(QPixmap.fromImage(rendering.pil_to_qimage(img))))
            return
        icon = kc.icon
        if icon.lower().endswith(".gif"):
            # show first frame for the preview
            pix = QPixmap(icon)
            if not pix.isNull():
                self.setIcon(QIcon(pix.scaled(self._size, self._size,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)))
                return
        img = rendering.render_key(self._size, kc.label, icon,
                                   kc.bg_color, kc.text_color)
        self.setIcon(QIcon(QPixmap.fromImage(rendering.pil_to_qimage(img))))

    def flash(self, on: bool):
        self.setStyleSheet(
            "QToolButton{border:2px solid #00ff88;border-radius:10px;background:#0b0b12;}"
            if on else self._base_qss)

    # -- start a drag to rearrange this key -------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.openFolder.emit(self.index)
        super().mouseDoubleClickEvent(e)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.MouseButton.LeftButton) or self._press_pos is None:
            return super().mouseMoveEvent(e)
        if (e.position().toPoint() - self._press_pos).manhattanLength() \
                < QApplication.startDragDistance():
            return super().mouseMoveEvent(e)
        drag = QDrag(self)
        mime = QMimeData()
        provider = globals().get("CURRENT_PAGE_ID_PROVIDER")
        page_id = provider() if provider else ""
        # index + the page the drag STARTED on — QDrag.exec runs a nested
        # event loop, so an action can switch pages before the drop lands.
        mime.setData(MIME_KEY, f"{self.index}:{page_id}".encode())
        drag.setMimeData(mime)
        pm = self.icon().pixmap(QSize(self._size, self._size))
        if not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)
        self.setDown(False)

    # -- drop targets: a catalog action, or another key being rearranged --
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_ACTION) or md.hasFormat(MIME_KEY):
            e.acceptProposedAction()
            self.setStyleSheet(
                "QToolButton{border:2px dashed #409eff;border-radius:10px;background:#12203a;}")

    def dragLeaveEvent(self, e):
        self.setStyleSheet(self._base_qss)

    def dropEvent(self, e):
        self.setStyleSheet(self._base_qss)
        md = e.mimeData()
        if md.hasFormat(MIME_KEY):
            payload = bytes(md.data(MIME_KEY)).decode()
            src_s, _, page_id = payload.partition(":")
            src = int(src_s)
            provider = globals().get("CURRENT_PAGE_ID_PROVIDER")
            cur_page = provider() if provider else ""
            if page_id and cur_page and page_id != cur_page:
                # The page changed mid-drag; applying the swap here would
                # rearrange the wrong page's keys.
                e.ignore()
                return
            if src != self.index:
                self.keyMoved.emit(src, self.index)
            e.acceptProposedAction()
        elif md.hasFormat(MIME_ACTION):
            atype = bytes(md.data(MIME_ACTION)).decode()
            self.actionDropped.emit(self.index, atype)
            e.acceptProposedAction()


# ---------------------------------------------------------------------------
# Colour picker button
# ---------------------------------------------------------------------------
class ColorButton(QPushButton):
    changed = pyqtSignal(str)

    def __init__(self, color: str):
        super().__init__()
        self.setFixedWidth(60)
        self._color = color
        self._apply()
        self.clicked.connect(self._pick)

    def _apply(self):
        self.setStyleSheet(f"background:{self._color};border:1px solid #555;")
        self.setText(self._color)

    def _pick(self):
        # Force Qt's own dialog: the native color chooser ignores the app's
        # dark stylesheet (white window, unreadable with themed text).
        c = QColorDialog.getColor(
            QColor(self._color), self, "Choose color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if c.isValid():
            self._color = c.name()
            self._apply()
            self.changed.emit(self._color)

    def color(self):
        return self._color

    def set_color(self, c):
        self._color = c
        self._apply()


# ---------------------------------------------------------------------------
# Reusable action editor (type combo + dynamic params) for one Action
# ---------------------------------------------------------------------------
class ActionParamsWidget(QWidget):
    changed = pyqtSignal()

    def __init__(self, exclude=None):
        super().__init__()
        self._building = False
        self._exclude = set(exclude or [])
        self._params: dict[str, QWidget] = {}
        self._multi_editor = None
        self._orig_action = Action()      # last action given to set_action
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # Owned by self so it outlives the combos it filters.
        self._nowheel = _NoWheelWhenUnfocused(self)
        self.type_combo = _protect_wheel(QComboBox(), self._nowheel)
        for key, meta in ACTION_TYPES.items():
            if key in self._exclude:
                continue
            self.type_combo.addItem(meta["label"], key)
        self.type_combo.currentIndexChanged.connect(self._on_type)
        form = QFormLayout()
        form.addRow("Action", self.type_combo)
        self._layout.addLayout(form)
        self._params_box = QVBoxLayout()
        self._layout.addLayout(self._params_box)

    def set_action(self, action: Action):
        self._building = True
        # Keep the stored action verbatim: an action type this build does not
        # know (config written by a newer version) must round-trip untouched,
        # not be downgraded to the combo's first entry on the next edit.
        self._orig_action = Action(action.type, dict(action.params))
        i = self.type_combo.findData(action.type)
        if i < 0:
            # Either a type this editor normally excludes (e.g. a monitor
            # bound to a knob/step before the exclusion existed) or a type
            # from a newer build. Show it rather than falling back to index
            # 0 — that fallback silently rewrote the stored action.
            label = ACTION_TYPES.get(action.type, {}).get("label", action.type)
            self.type_combo.addItem(label, action.type)
            i = self.type_combo.findData(action.type)
        self.type_combo.setCurrentIndex(max(0, i))
        self._build(action.type, action.params)
        self._building = False

    def get_action(self, peek: bool = False) -> Action:
        """The action as the widgets show it. `peek` guarantees NO side
        effects (no keyring writes, no warning dialogs) — use it for
        baselines/comparisons; only real edits may use the default."""
        atype = self.type_combo.currentData()
        if atype not in ACTION_TYPES and self._orig_action.type == atype:
            # Unknown-to-this-build type: no param widgets were built, so
            # collecting would return {} and destroy the stored params.
            return Action(self._orig_action.type, dict(self._orig_action.params))
        return Action(atype, self._collect(peek))

    def _on_type(self):
        if self._building:
            return
        self._build(self.type_combo.currentData(), {})
        self.changed.emit()

    def _build(self, atype, values):
        while self._params_box.count():
            item = self._params_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._params = {}
        self._multi_editor = None
        if atype == "multi":
            self._multi_editor = MultiStepsEditor()
            self._multi_editor.set_steps(values.get("steps", []))
            self._multi_editor.changed.connect(self._emit)
            self._params_box.addWidget(self._multi_editor)
            return
        spec = ACTION_TYPES.get(atype, {}).get("params", [])
        if not spec:
            return
        form = QFormLayout()
        for key, kind, label in spec:
            if kind == "multiline":
                w = QPlainTextEdit(); w.setPlainText(str(values.get(key, "")))
                w.setFixedHeight(56); w.textChanged.connect(self._emit)
            elif kind == "password":
                from .. import secret_store
                sid = str(values.get("secret_id", ""))
                initial = secret_store.get(sid) if sid else values.get(key, "")
                w = QLineEdit(str(initial or ""))
                w.setEchoMode(QLineEdit.EchoMode.Password)
                w.setProperty("kind", "password")
                w.setProperty("secret_id", sid)
                # We hold a secret_id but couldn't read the secret back — the
                # keyring is locked or the entry is gone. The empty field is
                # then a display artefact, NOT the user clearing the password,
                # and _collect_password must not read it as one.
                unreadable = bool(sid) and not initial
                w.setProperty("secret_unreadable", unreadable)
                if unreadable:
                    # Make the invisible state visible: a password IS bound,
                    # we just can't display it. Without this cue the empty
                    # field reads as "no password".
                    w.setPlaceholderText("(saved — keyring locked)")
                w.textChanged.connect(self._emit)
            elif kind == "profiles":
                w = _protect_wheel(QComboBox(), self._nowheel)
                provider = globals().get("PROFILES_PROVIDER")
                cur = str(values.get(key, ""))
                if provider:
                    for prof in provider():
                        w.addItem(prof.name, prof.id)
                    j = w.findData(cur)
                    if j >= 0:
                        w.setCurrentIndex(j)
                    elif cur:
                        # The stored target no longer exists (profile deleted).
                        # Keep it visible instead of snapping to the first
                        # profile — that snap silently rebound the key on the
                        # next unrelated edit.
                        w.addItem(f"(missing profile {cur})", cur)
                        w.setCurrentIndex(w.count() - 1)
                w.currentIndexChanged.connect(self._emit)
                w.setProperty("kind", "profiles")
            elif kind.startswith("choice:"):
                w = _protect_wheel(QComboBox(), self._nowheel)
                for opt in kind.split(":", 1)[1].split(","):
                    w.addItem(opt)
                cur = str(values.get(key, ""))
                j = w.findText(cur)
                if j >= 0:
                    w.setCurrentIndex(j)
                elif cur:
                    # A stored value this build's list doesn't know (config
                    # from a newer version): show it verbatim so it round-
                    # trips instead of being replaced by the first option on
                    # the next unrelated edit.
                    w.addItem(cur)
                    w.setCurrentIndex(w.count() - 1)
                w.currentIndexChanged.connect(self._emit)
            else:
                w = QLineEdit(str(values.get(key, ""))); w.textChanged.connect(self._emit)
            self._params[key] = w
            form.addRow(label, w)
        holder = QWidget(); holder.setLayout(form)
        self._params_box.addWidget(holder)

    def _collect(self, peek: bool = False):
        if self._multi_editor is not None:
            return {"steps": self._multi_editor.get_steps(peek)}
        out = {}
        for k, w in self._params.items():
            if isinstance(w, QPlainTextEdit):
                out[k] = w.toPlainText()
            elif isinstance(w, QComboBox):
                # profiles combo stores the profile id in item data
                if w.property("kind") == "profiles":
                    out[k] = w.currentData() or ""
                else:
                    out[k] = w.currentText()
            elif isinstance(w, QLineEdit):
                if w.property("kind") == "password":
                    self._collect_password(w, out, peek)
                else:
                    out[k] = w.text()
        return out

    def _collect_password(self, w, out, peek=False):
        """Store the password in the OS keyring and put only its id in the
        config; fall back to plaintext if no keyring backend is available.

        With `peek` this is a pure read: it reproduces what a real collect
        WOULD store without touching the keyring or popping the cleartext
        warning. Merely selecting a password key takes this path (the editor
        baselines the action on selection) and selection must never have
        side effects."""
        from .. import secret_store
        text = w.text()
        sid = w.property("secret_id") or ""
        if peek:
            if text and sid:
                out["secret_id"] = sid
            elif text:
                out["password"] = text
            elif sid and w.property("secret_unreadable"):
                out["secret_id"] = sid
            return
        if not text:
            # An empty field means "no password" only if we were able to show
            # the current one. If the keyring was locked when this editor was
            # built we never had it to display, so dropping secret_id here
            # would destroy a working binding — permanently, and silently —
            # just because the user edited the label next to it.
            if sid and w.property("secret_unreadable"):
                out["secret_id"] = sid
            return
        if not sid:
            sid = secret_store.new_id()
        if secret_store.store(sid, text):
            w.setProperty("secret_id", sid)
            w.setProperty("secret_unreadable", False)
            out["secret_id"] = sid
        else:
            # No keyring, or it refused: the value can only be kept in
            # config.json, in the clear. Warn once — the user chose a password
            # action expecting it to be stored securely, and silently doing
            # otherwise is exactly the kind of thing they'd want to know.
            out["password"] = text
            self._warn_plaintext_once()

    def _warn_plaintext_once(self):
        # Process-wide, not per-instance: a new editor is built on every key
        # selection (and one per step in a multi-action), so an instance flag
        # would re-pop the modal on each click — forever, for the exact users
        # the warning is aimed at.
        global _PLAINTEXT_WARNED
        if _PLAINTEXT_WARNED:
            return
        _PLAINTEXT_WARNED = True
        log.warning("no usable keyring — password stored in config.json in cleartext")
        QMessageBox.warning(
            self, "Password not stored securely",
            "No usable keyring was found, so this password will be saved in "
            "your configuration file in cleartext (readable by anything running "
            "as you).\n\nInstalling a keyring service — e.g. gnome-keyring, or "
            "KWallet with kwalletmanager — lets it be stored securely instead.")

    def _emit(self, *_):
        if not self._building:
            self.changed.emit()


# ---------------------------------------------------------------------------
# Multi-action editor: an ordered list of sub-action steps with per-step delay
# ---------------------------------------------------------------------------
# Not valid as a step: nesting/navigation, and monitor (a live display, not an
# executable action — as a step it would be a silent no-op).
_STEP_EXCLUDE = {"multi", "open_folder", "folder_back", "monitor"}


class _StepRow(QFrame):
    changed = pyqtSignal()
    removed = pyqtSignal()

    def __init__(self, step: dict | None = None):
        super().__init__()
        self.setStyleSheet("QFrame{background:#1f1f1f;border:1px solid #333;border-radius:6px;}")
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Do:"))
        top.addStretch()
        rm = QPushButton("✕")
        rm.setFixedWidth(26)
        rm.setToolTip("Remove step")
        rm.clicked.connect(self.removed.emit)
        top.addWidget(rm)
        v.addLayout(top)
        self.apw = ActionParamsWidget(exclude=_STEP_EXCLUDE)
        self.apw.changed.connect(self.changed.emit)
        v.addWidget(self.apw)
        drow = QHBoxLayout()
        drow.addWidget(QLabel("then wait (s):"))
        # Inside a fixed-height scroll area: hover-scroll past it is routine,
        # and an unfocused spinbox would eat the wheel and rewrite the delay.
        self.delay = _protect_wheel(QDoubleSpinBox(), self.apw._nowheel)
        self.delay.setRange(0.0, 30.0)
        self.delay.setSingleStep(0.1)
        self.delay.setDecimals(1)
        self.delay.valueChanged.connect(self.changed.emit)
        drow.addWidget(self.delay)
        drow.addStretch()
        v.addLayout(drow)
        if step:
            self.apw.set_action(Action.from_dict(step.get("action", {})))
            try:
                self.delay.setValue(float(step.get("delay", 0) or 0))
            except (TypeError, ValueError):
                pass
        else:
            self.apw.set_action(Action("launch_app", {}))

    def value(self, peek: bool = False) -> dict:
        return {"action": self.apw.get_action(peek).to_dict(),
                "delay": self.delay.value()}


class MultiStepsEditor(QWidget):
    """Edits an ordered list of steps for a Multi-action."""
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._rows: list[_StepRow] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Steps (run top to bottom on press):")
        lbl.setStyleSheet("color:#9a9a9a;")
        v.addWidget(lbl)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(300)
        host = QWidget()
        self._vbox = QVBoxLayout(host)
        self._vbox.addStretch()
        self._scroll.setWidget(host)
        v.addWidget(self._scroll)
        add = QPushButton("＋ Add step")
        add.clicked.connect(self._on_add)
        v.addWidget(add)

    def _on_add(self):
        self._add_row()
        self.changed.emit()

    def _add_row(self, step: dict | None = None):
        row = _StepRow(step)
        row.changed.connect(self.changed.emit)
        row.removed.connect(lambda r=row: self._remove(r))
        self._rows.append(row)
        self._vbox.insertWidget(self._vbox.count() - 1, row)  # keep trailing stretch

    def _remove(self, row: _StepRow):
        if row in self._rows:
            self._rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self.changed.emit()

    def set_steps(self, steps):
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        for st in (steps or []):
            self._add_row(st)

    def get_steps(self, peek: bool = False) -> list:
        return [r.value(peek) for r in self._rows]


class ActionEditor(QWidget):
    """Edits a single key's appearance + action."""
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._kc: KeyConfig | None = None
        self._index: int | None = None
        self._building = False
        # The action as of the last edit (and its signature), so we can tell
        # an action change (auto icon may follow) from an icon/label/colour
        # edit (icon must NOT be touched) — and know the PREVIOUS action's
        # default icon, which is what identifies an untouched auto icon.
        self._last_action: Action = Action()
        self._last_action_sig: tuple | None = None

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title = QLabel("No key selected")
        self.title.setStyleSheet("font-weight:bold;font-size:14px;")
        header.addWidget(self.title)
        header.addStretch()
        self.clear_btn = QPushButton("Clear key")
        self.clear_btn.clicked.connect(self._clear_key)
        header.addWidget(self.clear_btn)
        root.addLayout(header)

        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.label_edit.textChanged.connect(self._on_edit)
        form.addRow("Label", self.label_edit)

        icon_row = QHBoxLayout()
        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("(optional) image or .gif")
        self.icon_edit.textChanged.connect(self._on_edit)
        lib_btn = QPushButton("Library…"); lib_btn.clicked.connect(self._pick_library)
        icon_btn = QPushButton("File…"); icon_btn.clicked.connect(self._browse_icon)
        clr_btn = QPushButton("×"); clr_btn.setFixedWidth(28)
        clr_btn.clicked.connect(lambda: self.icon_edit.setText(""))
        for w in (self.icon_edit, lib_btn, icon_btn, clr_btn):
            icon_row.addWidget(w)
        form.addRow("Icon", self._wrap(icon_row))

        color_row = QHBoxLayout()
        self.bg_btn = ColorButton("#101020"); self.fg_btn = ColorButton("#ffffff")
        self.bg_btn.changed.connect(self._on_edit); self.fg_btn.changed.connect(self._on_edit)
        color_row.addWidget(QLabel("BG")); color_row.addWidget(self.bg_btn)
        color_row.addWidget(QLabel("Text")); color_row.addWidget(self.fg_btn)
        color_row.addStretch()
        form.addRow("Colors", self._wrap(color_row))
        root.addLayout(form)

        self.params = ActionParamsWidget()
        self.params.changed.connect(self._on_edit)
        root.addWidget(self.params)

        # Second action slot: fires after holding the key ~0.5 s. Excluded
        # types: monitor is display-only, and open_folder is bound to the
        # key's folder slot (long-press folder_back IS allowed — it makes a
        # natural "hold to go back").
        hold_title = QLabel("Hold action (long press)")
        hold_title.setStyleSheet("font-weight: bold; margin-top: 6px;")
        root.addWidget(hold_title)
        self.hold_params = ActionParamsWidget(exclude={"monitor", "open_folder"})
        self.hold_params.changed.connect(self._on_edit)
        root.addWidget(self.hold_params)
        root.addStretch()
        self.setEnabled(False)

    def _wrap(self, layout):
        w = QWidget(); w.setLayout(layout); return w

    def set_key(self, kc: KeyConfig, index: int):
        self._building = True
        self._kc = kc
        self._index = index
        self.setEnabled(True)
        self.title.setText(f"Key {index}")
        self.label_edit.setText(kc.label)
        self.icon_edit.setText(kc.icon)
        self.bg_btn.set_color(kc.bg_color)
        self.fg_btn.set_color(kc.text_color)
        self.params.set_action(kc.action)
        self.hold_params.set_action(kc.hold_action)
        # Baseline MUST come from the widgets, exactly like _on_edit reads it:
        # the editor materializes every field of the action type, so a stored
        # action with optional fields omitted (e.g. volume without "step")
        # would otherwise look "changed" on the very first edit and clobber the
        # icon the user had just picked. peek=True: baselining on SELECTION
        # must have no side effects (get_action on a password key would
        # otherwise write the keyring / pop the cleartext warning).
        self._last_action = self.params.get_action(peek=True)
        self._last_action_sig = self._action_sig(self._last_action)
        self._building = False

    @staticmethod
    def _action_sig(action: Action) -> tuple:
        """Cheap comparable identity of an action (params may hold lists)."""
        return (action.type,
                tuple(sorted((k, str(v)) for k, v in action.params.items())))

    def clear(self):
        self._kc = None
        self._index = None
        self.setEnabled(False)
        self.title.setText("No key selected")

    def _clear_key(self):
        """Reset the selected key to empty (label, icon, colours, action)."""
        if self._kc is None or self._index is None:
            return
        default = KeyConfig()
        self._kc.label = default.label
        self._kc.icon = default.icon
        self._kc.bg_color = default.bg_color
        self._kc.text_color = default.text_color
        self._kc.action = Action()
        self._kc.hold_action = Action()
        # The explicit Clear button IS intent to wipe the key, folder included.
        # Folders survive mere action-type changes as dormant state, but a
        # cleared key must not silently resurrect old pages when a folder is
        # later dropped onto it.
        self._kc.folder = None
        self.set_key(self._kc, self._index)   # refresh the editor fields
        self.changed.emit()

    def _browse_icon(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose icon", "", "Images (*.png *.jpg *.jpeg *.svg *.gif *.bmp)")
        if path:
            self.icon_edit.setText(path)

    def _pick_library(self):
        dlg = IconLibraryDialog(self)
        if dlg.exec() and dlg.chosen:
            self.icon_edit.setText(dlg.chosen)

    def _on_edit(self, *_):
        if self._building or self._kc is None:
            return
        from ..actions import default_icon_for
        new_action = self.params.get_action()
        prev_action = self._last_action
        self._last_action = new_action
        self._last_action_sig = self._action_sig(new_action)
        # Make the icon follow the action's sub-command (up/down/mute, etc.) —
        # but ONLY when the current icon is still the PREVIOUS action's
        # default, i.e. it was auto-assigned and never touched by the user.
        # Provenance matters, not "is it a library icon": a library icon the
        # user deliberately picked in the Library dialog must survive every
        # later edit (three separate user-reported regressions came from
        # weaker rules here). An icon the user explicitly cleared ("" while
        # the old default is non-empty) stays cleared, too.
        cur_icon = self.icon_edit.text()
        old_default = assets.library_ref(default_icon_for(prev_action)[0])
        new_default = assets.library_ref(default_icon_for(new_action)[0])
        if new_default != old_default and cur_icon == old_default:
            self._building = True
            self.icon_edit.setText(new_default)
            self._building = False
        self._kc.label = self.label_edit.text()
        self._kc.icon = self.icon_edit.text()
        self._kc.bg_color = self.bg_btn.color()
        self._kc.text_color = self.fg_btn.color()
        self._kc.action = new_action
        self._kc.hold_action = self.hold_params.get_action()
        self.changed.emit()


# ---------------------------------------------------------------------------
# Draggable action catalog (left sidebar)
# ---------------------------------------------------------------------------
class ActionCatalog(QListWidget):
    """List of actions grouped by category; drag an item onto a key."""
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setStyleSheet(
            "QListWidget{background:#161616;border:none;}"
            "QListWidget::item{padding:6px 8px;border-radius:6px;margin:1px 4px;}"
            "QListWidget::item:selected{background:#1551ff;}")
        for cat, types in ACTION_CATALOG:
            header = QListWidgetItem(cat.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            f = header.font(); f.setBold(True); f.setPointSize(8); header.setFont(f)
            header.setForeground(QColor("#7a7a7a"))
            self.addItem(header)
            for t in types:
                label = ACTION_TYPES.get(t, {}).get("label", t)
                item = QListWidgetItem("   " + label)
                item.setData(Qt.ItemDataRole.UserRole, t)
                self.addItem(item)

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        atype = item.data(Qt.ItemDataRole.UserRole)
        if not atype:
            return
        mime = QMimeData()
        mime.setData(MIME_ACTION, atype.encode())
        mime.setText(atype)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


# ---------------------------------------------------------------------------
# Knob / dial editor (shown only for devices that have dials)
# ---------------------------------------------------------------------------
class KnobEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, knob_index: int, kn: KnobConfig):
        super().__init__()
        self._kn = kn
        self._building = True
        self.setStyleSheet("QWidget{background:#1f1f1f;border-radius:8px;}")
        v = QVBoxLayout(self)
        title = QLabel(f"Knob {knob_index}")
        title.setStyleSheet("font-weight:bold;")
        v.addWidget(title)
        self.label_edit = QLineEdit(kn.label)
        self.label_edit.setPlaceholderText("Knob label")
        self.label_edit.textChanged.connect(self._emit)
        v.addWidget(self.label_edit)
        self._pickers = {}
        for name, action in (("Press", kn.press), ("Rotate ◀", kn.left), ("Rotate ▶", kn.right)):
            v.addWidget(QLabel(name))
            # monitor is display-only — bound to a knob gesture it would be a
            # silent no-op, so don't offer it
            p = ActionParamsWidget(exclude={"monitor"})
            p.set_action(action)
            p.changed.connect(self._emit)
            self._pickers[name] = p
            v.addWidget(p)
            line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color:#333;")
            v.addWidget(line)
        self._building = False

    def _emit(self):
        if self._building:
            return
        self._kn.label = self.label_edit.text()
        self._kn.press = self._pickers["Press"].get_action()
        self._kn.left = self._pickers["Rotate ◀"].get_action()
        self._kn.right = self._pickers["Rotate ▶"].get_action()
        self.changed.emit()
