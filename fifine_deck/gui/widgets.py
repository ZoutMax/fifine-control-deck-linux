"""Reusable widgets: key button, action editor, action catalog, knob editor."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QMimeData
from PyQt6.QtGui import QPixmap, QColor, QIcon, QDrag
from PyQt6.QtWidgets import (
    QToolButton, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QComboBox, QPushButton, QColorDialog, QFileDialog,
    QSpinBox, QDialog, QScrollArea, QGridLayout, QListWidget, QListWidgetItem,
    QAbstractItemView, QFrame,
)

from .. import rendering, assets
from ..actions import ACTION_TYPES, ACTION_CATALOG
from ..model import KeyConfig, KnobConfig, Action

MIME_ACTION = "application/x-fifine-action"


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
                b.clicked.connect(lambda _, p=it["file"]: self._pick(p))
                grid.addWidget(b, i // 6, i % 6)
            vbox.addWidget(grid_host)
        vbox.addStretch()
        scroll.setWidget(host)
        root.addWidget(scroll)

    def _pick(self, path):
        self.chosen = path
        self.accept()


# ---------------------------------------------------------------------------
# Key button (grid cell) — selectable + accepts dropped actions
# ---------------------------------------------------------------------------
class KeyButton(QToolButton):
    selected = pyqtSignal(int)
    actionDropped = pyqtSignal(int, str)   # (index, action_type)

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

    def update_preview(self, kc: KeyConfig):
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

    # drag & drop of catalog actions -------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_ACTION):
            e.acceptProposedAction()
            self.setStyleSheet(
                "QToolButton{border:2px dashed #409eff;border-radius:10px;background:#12203a;}")

    def dragLeaveEvent(self, e):
        self.setStyleSheet(self._base_qss)

    def dropEvent(self, e):
        self.setStyleSheet(self._base_qss)
        if e.mimeData().hasFormat(MIME_ACTION):
            atype = bytes(e.mimeData().data(MIME_ACTION)).decode()
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
        c = QColorDialog.getColor(QColor(self._color), self)
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

    def __init__(self):
        super().__init__()
        self._building = False
        self._params: dict[str, QWidget] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.type_combo = QComboBox()
        for key, meta in ACTION_TYPES.items():
            self.type_combo.addItem(meta["label"], key)
        self.type_combo.currentIndexChanged.connect(self._on_type)
        form = QFormLayout()
        form.addRow("Action", self.type_combo)
        self._layout.addLayout(form)
        self._params_box = QVBoxLayout()
        self._layout.addLayout(self._params_box)

    def set_action(self, action: Action):
        self._building = True
        i = self.type_combo.findData(action.type)
        self.type_combo.setCurrentIndex(max(0, i))
        self._build(action.type, action.params)
        self._building = False

    def get_action(self) -> Action:
        return Action(self.type_combo.currentData(), self._collect())

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
        spec = ACTION_TYPES.get(atype, {}).get("params", [])
        if not spec:
            return
        form = QFormLayout()
        for key, kind, label in spec:
            if kind == "multiline":
                w = QPlainTextEdit(); w.setPlainText(str(values.get(key, "")))
                w.setFixedHeight(56); w.textChanged.connect(self._emit)
            elif kind.startswith("choice:"):
                w = QComboBox()
                for opt in kind.split(":", 1)[1].split(","):
                    w.addItem(opt)
                j = w.findText(str(values.get(key, "")))
                if j >= 0:
                    w.setCurrentIndex(j)
                w.currentIndexChanged.connect(self._emit)
            else:
                w = QLineEdit(str(values.get(key, ""))); w.textChanged.connect(self._emit)
            self._params[key] = w
            form.addRow(label, w)
        holder = QWidget(); holder.setLayout(form)
        self._params_box.addWidget(holder)

    def _collect(self):
        out = {}
        for k, w in self._params.items():
            if isinstance(w, QPlainTextEdit):
                out[k] = w.toPlainText()
            elif isinstance(w, QComboBox):
                out[k] = w.currentText()
            elif isinstance(w, QLineEdit):
                out[k] = w.text()
        return out

    def _emit(self, *_):
        if not self._building:
            self.changed.emit()


class ActionEditor(QWidget):
    """Edits a single key's appearance + action."""
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._kc: KeyConfig | None = None
        self._index: int | None = None
        self._building = False

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
        self._building = False

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
        self._kc.label = self.label_edit.text()
        self._kc.icon = self.icon_edit.text()
        self._kc.bg_color = self.bg_btn.color()
        self._kc.text_color = self.fg_btn.color()
        self._kc.action = self.params.get_action()
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
            p = ActionParamsWidget()
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
