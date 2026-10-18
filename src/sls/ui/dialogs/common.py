"""Shared dialog helpers."""
from __future__ import annotations

import traceback

from PySide6.QtWidgets import (QAbstractItemView, QDialogButtonBox, QHeaderView, QMessageBox, QTableWidget,
                               QTableWidgetItem, QWidget)

from ...core.errors import SlsError
from ...core.i18n import tr


def show_error(parent: QWidget | None, exc: BaseException) -> None:
    """Expected errors show their message; unexpected ones show the traceback (never swallowed)."""
    if isinstance(exc, SlsError):
        details = exc.details.get("errors") or exc.details.get("blockers")
        text = exc.message
        if isinstance(details, list) and details:
            text += "\n\n" + "\n".join(f"• {d if isinstance(d, str) else d}" for d in details[:30])
        QMessageBox.warning(parent, tr("app.title"), text)
    else:
        box = QMessageBox(QMessageBox.Critical, tr("app.title"), f"{type(exc).__name__}: {exc}", parent=parent)
        box.setDetailedText("".join(traceback.format_exception(exc)))
        box.exec()


def make_table(headers: list[str], stretch_last: bool = True, editable: bool = False) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    if not editable:
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch_last:
        h.setStretchLastSection(True)
    return t


def fill_table(t: QTableWidget, rows: list[list[object]]) -> None:
    t.setRowCount(0)
    for r in rows:
        i = t.rowCount()
        t.insertRow(i)
        for j, v in enumerate(r):
            t.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))


def ok_cancel(dialog) -> QDialogButtonBox:  # type: ignore[no-untyped-def]
    b = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    b.accepted.connect(dialog.accept)
    b.rejected.connect(dialog.reject)
    return b
