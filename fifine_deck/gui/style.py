"""Dark theme matching the original fifine / Stream Dock look.

Palette (from the original app): near-black background, dark-grey panels,
grey text, blue accent (#1551ff / #409eff).
"""

BG = "#161616"
PANEL = "#1f1f1f"
PANEL2 = "#262626"
BORDER = "#333333"
TEXT = "#e6e6e6"
TEXT_DIM = "#9a9a9a"
ACCENT = "#1551ff"
ACCENT_HOVER = "#409eff"

STYLESHEET = f"""
* {{ outline: none; }}
QMainWindow, QDialog, QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 13px;
}}
QDockWidget {{ color: {TEXT_DIM}; titlebar-close-icon: none; }}
QDockWidget::title {{
    background: {PANEL}; padding: 6px 10px; border-bottom: 1px solid {BORDER};
}}
QLabel {{ color: {TEXT}; background: transparent; }}
QStatusBar {{ background: {PANEL}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}

QPushButton {{
    background: {PANEL2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT_HOVER}; }}
QPushButton:pressed {{ background: {ACCENT}; border-color: {ACCENT}; }}

QComboBox, QLineEdit, QPlainTextEdit, QSpinBox {{
    background: {PANEL2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px;
    selection-background-color: {ACCENT};
}}
QComboBox:hover, QLineEdit:hover {{ border-color: {ACCENT_HOVER}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {PANEL2}; color: {TEXT};
    border: 1px solid {BORDER}; selection-background-color: {ACCENT};
}}

QSlider::groove:horizontal {{
    height: 5px; background: {PANEL2}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 14px; margin: -5px 0; border-radius: 7px;
}}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; border: none; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {ACCENT}; }}
QMenu {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""
