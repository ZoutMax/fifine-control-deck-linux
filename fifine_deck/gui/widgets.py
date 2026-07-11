"""Reusable widgets: the key button and the action editor."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QColor, QIcon
from PyQt6.QtWidgets import (
    QToolButton, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QComboBox, QPushButton, QColorDialog, QFileDialog,
    QSpinBox,
)

from .. import rendering
from ..actions import ACTION_TYPES
from ..model import KeyConfig, Action


class KeyButton(QToolButton):
    """One key in the grid. Shows the rendered preview; emits selected(index)."""
    selected = pyqtSignal(int)

    def __init__(self, index: int, size: int = 96):
        super().__init__()
        self.index = index
        self._size = size
        self.setCheckable(True)
        self.setFixedSize(size + 12, size + 12)
        self.setIconSize(QSize(size, size))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setStyleSheet(
            "QToolButton{border:2px solid #333;border-radius:10px;background:#0b0b12;}"
            "QToolButton:checked{border:2px solid #00c8ff;}"
            "QToolButton:hover{border-color:#666;}")
        self.clicked.connect(lambda: self.selected.emit(self.index))

    def update_preview(self, kc: KeyConfig):
        img = rendering.render_key(self._size, kc.label, kc.icon,
                                   kc.bg_color, kc.text_color)
        self.setIcon(QIcon(QPixmap.fromImage(rendering.pil_to_qimage(img))))

    def flash(self, on: bool):
        self.setStyleSheet(
            ("QToolButton{border:2px solid #00ff88;border-radius:10px;background:#0b0b12;}"
             if on else
             "QToolButton{border:2px solid #333;border-radius:10px;background:#0b0b12;}"
             "QToolButton:checked{border:2px solid #00c8ff;}"
             "QToolButton:hover{border-color:#666;}"))


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


class ActionEditor(QWidget):
    """Edits a single key's appearance + action. Emits changed() on any edit."""
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._kc: KeyConfig | None = None
        self._param_widgets: dict[str, QWidget] = {}
        self._building = False

        root = QVBoxLayout(self)
        self.title = QLabel("No key selected")
        self.title.setStyleSheet("font-weight:bold;font-size:14px;")
        root.addWidget(self.title)

        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.label_edit.textChanged.connect(self._on_edit)
        form.addRow("Label", self.label_edit)

        icon_row = QHBoxLayout()
        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("(optional) path to image")
        self.icon_edit.textChanged.connect(self._on_edit)
        icon_btn = QPushButton("Browse…")
        icon_btn.clicked.connect(self._browse_icon)
        clr_btn = QPushButton("×")
        clr_btn.setFixedWidth(28)
        clr_btn.clicked.connect(lambda: self.icon_edit.setText(""))
        icon_row.addWidget(self.icon_edit)
        icon_row.addWidget(icon_btn)
        icon_row.addWidget(clr_btn)
        form.addRow("Icon", self._wrap(icon_row))

        color_row = QHBoxLayout()
        self.bg_btn = ColorButton("#101020")
        self.fg_btn = ColorButton("#ffffff")
        self.bg_btn.changed.connect(self._on_edit)
        self.fg_btn.changed.connect(self._on_edit)
        color_row.addWidget(QLabel("BG"))
        color_row.addWidget(self.bg_btn)
        color_row.addWidget(QLabel("Text"))
        color_row.addWidget(self.fg_btn)
        color_row.addStretch()
        form.addRow("Colors", self._wrap(color_row))

        self.type_combo = QComboBox()
        for key, meta in ACTION_TYPES.items():
            self.type_combo.addItem(meta["label"], key)
        self.type_combo.currentIndexChanged.connect(self._on_type_change)
        form.addRow("Action", self.type_combo)
        root.addLayout(form)

        self.params_box = QVBoxLayout()
        root.addLayout(self.params_box)
        root.addStretch()
        self.setEnabled(False)

    def _wrap(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def set_key(self, kc: KeyConfig, index: int):
        self._building = True
        self._kc = kc
        self.setEnabled(True)
        self.title.setText(f"Key {index}")
        self.label_edit.setText(kc.label)
        self.icon_edit.setText(kc.icon)
        self.bg_btn.set_color(kc.bg_color)
        self.fg_btn.set_color(kc.text_color)
        i = self.type_combo.findData(kc.action.type)
        self.type_combo.setCurrentIndex(max(0, i))
        self._build_params(kc.action.type, kc.action.params)
        self._building = False

    def clear(self):
        self._kc = None
        self.setEnabled(False)
        self.title.setText("No key selected")

    def _browse_icon(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose icon", "", "Images (*.png *.jpg *.jpeg *.svg *.gif *.bmp)")
        if path:
            self.icon_edit.setText(path)

    def _on_type_change(self):
        if self._building:
            return
        t = self.type_combo.currentData()
        self._build_params(t, {})
        self._on_edit()

    def _build_params(self, action_type: str, values: dict):
        # clear old
        while self.params_box.count():
            item = self.params_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._param_widgets = {}
        spec = ACTION_TYPES.get(action_type, {}).get("params", [])
        form = QFormLayout()
        for key, kind, label in spec:
            if kind == "multiline":
                w = QPlainTextEdit()
                w.setPlainText(str(values.get(key, "")))
                w.setFixedHeight(60)
                w.textChanged.connect(self._on_edit)
            elif kind.startswith("choice:"):
                w = QComboBox()
                for opt in kind.split(":", 1)[1].split(","):
                    w.addItem(opt)
                cur = str(values.get(key, ""))
                j = w.findText(cur)
                if j >= 0:
                    w.setCurrentIndex(j)
                w.currentIndexChanged.connect(self._on_edit)
            else:
                w = QLineEdit(str(values.get(key, "")))
                w.textChanged.connect(self._on_edit)
            self._param_widgets[key] = w
            form.addRow(label, w)
        holder = QWidget()
        holder.setLayout(form)
        self.params_box.addWidget(holder)

    def _collect_params(self) -> dict:
        out = {}
        for key, w in self._param_widgets.items():
            if isinstance(w, QPlainTextEdit):
                out[key] = w.toPlainText()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, QLineEdit):
                out[key] = w.text()
        return out

    def _on_edit(self, *_):
        if self._building or self._kc is None:
            return
        self._kc.label = self.label_edit.text()
        self._kc.icon = self.icon_edit.text()
        self._kc.bg_color = self.bg_btn.color()
        self._kc.text_color = self.fg_btn.color()
        self._kc.action = Action(self.type_combo.currentData(), self._collect_params())
        self.changed.emit()
