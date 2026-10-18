"""Dark technical theme and font setup."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from ..reporting.arabic import FONT_DIR

BG = "#1E2227"
PANEL = "#252A31"
PANEL_ALT = "#2C323A"
BORDER = "#3A424D"
TEXT = "#D7DDE4"
MUTED = "#8C96A3"
ACCENT = "#3D8FD6"
CANVAS_BG = "#1A1D21"
SELECT = "#42A5F5"
WARN = "#E0A040"
ERROR = "#E05A50"
OK = "#5CB85C"

STYLESHEET = f"""
QWidget {{ background: {PANEL}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 3px; height: 3px; }}
QDockWidget::title {{ background: {PANEL_ALT}; padding: 4px 6px; border-bottom: 1px solid {BORDER}; }}
QToolBar {{ background: {PANEL_ALT}; border: none; border-bottom: 1px solid {BORDER}; spacing: 2px; padding: 2px; }}
QToolBar QToolButton {{ padding: 3px 7px; border: 1px solid transparent; border-radius: 3px; }}
QToolBar QToolButton:hover {{ background: #3A4350; border-color: {BORDER}; }}
QToolBar QToolButton:checked {{ background: #2F5F8A; border-color: {ACCENT}; }}
QToolBar QToolButton:disabled {{ color: #5A636E; }}
QMenuBar {{ background: {PANEL_ALT}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: #2F5F8A; }}
QMenu {{ background: {PANEL_ALT}; border: 1px solid {BORDER}; }}
QMenu::item:disabled {{ color: #5A636E; }}
QStatusBar {{ background: {PANEL_ALT}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ padding: 0 8px; color: {MUTED}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 3px; padding: 3px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QPushButton {{ background: #34404D; border: 1px solid {BORDER}; border-radius: 3px; padding: 5px 12px; }}
QPushButton:hover {{ background: #3E4C5B; }}
QPushButton:default {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5A636E; }}
QTableWidget, QTreeWidget, QListWidget, QTableView {{ background: {BG}; alternate-background-color: #22272D;
    gridline-color: {BORDER}; border: 1px solid {BORDER}; selection-background-color: #2F5F8A; }}
QHeaderView::section {{ background: {PANEL_ALT}; border: none; border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER}; padding: 4px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; }}
QTabBar::tab {{ background: {PANEL_ALT}; padding: 5px 12px; border: 1px solid {BORDER}; border-bottom: none; }}
QTabBar::tab:selected {{ background: {BG}; color: white; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 4px; margin-top: 10px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {MUTED}; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: {BG}; width: 10px; height: 10px; }}
QScrollBar::handle {{ background: #46505C; border-radius: 4px; min-height: 20px; min-width: 20px; }}
QLabel#warning {{ color: {WARN}; }}
QLabel#error {{ color: {ERROR}; }}
QLabel#ok {{ color: {OK}; }}
QLabel#muted {{ color: {MUTED}; }}
"""


def load_fonts() -> str:
    """Register bundled fonts with Qt; returns the UI font family."""
    for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoNaskhArabic-Regular.ttf",
                 "NotoNaskhArabic-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(FONT_DIR / name))
    return "Noto Sans"


def apply_theme(app: QApplication) -> None:
    family = load_fonts()
    font = QFont(family, 10)
    # Arabic glyphs fall back to Noto Naskh Arabic through font substitution.
    QFont.insertSubstitution(family, "Noto Naskh Arabic")
    app.setFont(font)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, PANEL), (QPalette.Base, BG), (QPalette.AlternateBase, PANEL_ALT),
                        (QPalette.Text, TEXT), (QPalette.WindowText, TEXT), (QPalette.Button, "#34404D"),
                        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT),
                        (QPalette.HighlightedText, "#FFFFFF"), (QPalette.ToolTipBase, PANEL_ALT),
                        (QPalette.ToolTipText, TEXT)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
