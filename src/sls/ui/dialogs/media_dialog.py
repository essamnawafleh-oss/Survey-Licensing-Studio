"""Current-condition photographs, supporting attachments and professional notes."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from ...core.i18n import tr
from ...persistence.media_repo import NOTE_SECTIONS
from ...persistence.workspace import Workspace
from .common import fill_table, make_table, ok_cancel, show_error

PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.tif *.tiff *.bmp)"


class MediaDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, parent: QWidget | None = None, start_tab: int = 0) -> None:
        super().__init__(parent)
        self.ws, self.case_id = ws, case_id
        self.setWindowTitle(tr("media.title"))
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.tabs.addTab(self._photos_tab(), tr("media.photos"))
        self.tabs.addTab(self._attachments_tab(), tr("media.attachments"))
        self.tabs.addTab(self._notes_tab(), tr("media.notes"))
        self.tabs.setCurrentIndex(start_tab)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        lay.addWidget(close)
        self._refresh()

    def _buttons(self, spec) -> QHBoxLayout:  # type: ignore[no-untyped-def]
        row = QHBoxLayout()
        for label, slot in spec:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        return row

    def _photos_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.photos = make_table([tr("rpt.file"), tr("media.caption"), tr("lic.include")])
        lay.addWidget(self.photos)
        lay.addLayout(self._buttons([(tr("media.add_photos"), self._add_photos), (tr("media.edit"), self._edit_photo),
                                     (tr("lic.open"), lambda: self._open(self.photo_list, self.photos))]))
        return w

    def _attachments_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.atts = make_table([tr("rpt.file"), tr("media.title_col"), tr("lic.include"), tr("lic.page_selection")])
        lay.addWidget(self.atts)
        lay.addLayout(self._buttons([(tr("media.add_attachments"), self._add_atts), (tr("media.edit"), self._edit_att),
                                     (tr("lic.open"), lambda: self._open(self.att_list, self.atts))]))
        note = QLabel(tr("media.attachment_note"))
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        return w

    def _notes_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        self.notes_list = QListWidget()
        self.notes_list.currentRowChanged.connect(self._show_note)
        lay.addWidget(self.notes_list, 1)
        right = QVBoxLayout()
        self.section = QComboBox()
        for s in NOTE_SECTIONS:
            self.section.addItem(tr(f"note.{s}"), s)
        self.note_text = QPlainTextEdit()
        right.addWidget(self.section)
        right.addWidget(self.note_text)
        right.addLayout(self._buttons([(tr("media.new_note"), self._new_note), (tr("media.save_note"), self._save_note)]))
        hint = QLabel(tr("media.notes_hint"))
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        right.addWidget(hint)
        lay.addLayout(right, 2)
        self._note_id = ""
        return w

    def _refresh(self) -> None:
        self.photo_list = self.ws.media.photos(self.case_id)
        fill_table(self.photos, [[p.original_name, p.caption, tr("common.yes") if p.include_in_report
                                  else tr("common.no")] for p in self.photo_list])
        self.att_list = self.ws.media.attachments(self.case_id)
        fill_table(self.atts, [[a.original_name, a.title, tr("common.yes") if a.include_in_report else tr("common.no"),
                                a.page_selection or tr("lic.all_pages")] for a in self.att_list])
        self.note_items = self.ws.media.notes(self.case_id)
        self.notes_list.clear()
        for n in self.note_items:
            self.notes_list.addItem(f"[{tr('note.' + n.section)}] {n.text[:60]}")

    def _add_photos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("media.add_photos"), "", PHOTO_FILTER)
        for p in paths:
            try:
                self.ws.media.add_photo(self.ws.user, self.case_id, Path(p), Path(p).stem)
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc)
        self._refresh()

    def _add_atts(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("media.add_attachments"))
        for p in paths:
            try:
                self.ws.media.add_attachment(self.ws.user, self.case_id, Path(p))
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc)
        self._refresh()

    def _edit_photo(self) -> None:
        r = self.photos.currentRow()
        if r < 0:
            return
        p = self.photo_list[r]
        dlg = QDialog(self)
        f = QFormLayout(dlg)
        cap = QLineEdit(p.caption)
        inc = QCheckBox()
        inc.setChecked(p.include_in_report)
        f.addRow(tr("media.caption"), cap)
        f.addRow(tr("lic.include"), inc)
        f.addRow(ok_cancel(dlg))
        if dlg.exec() == QDialog.Accepted:
            self.ws.media.update_photo(self.ws.user, p.id, cap.text(), inc.isChecked())
            self._refresh()

    def _edit_att(self) -> None:
        r = self.atts.currentRow()
        if r < 0:
            return
        a = self.att_list[r]
        dlg = QDialog(self)
        f = QFormLayout(dlg)
        title = QLineEdit(a.title)
        inc = QCheckBox()
        inc.setChecked(a.include_in_report)
        sel = QLineEdit(a.page_selection)
        sel.setPlaceholderText(tr("lic.page_selection_hint"))
        f.addRow(tr("media.title_col"), title)
        f.addRow(tr("lic.include"), inc)
        f.addRow(tr("lic.page_selection"), sel)
        f.addRow(ok_cancel(dlg))
        if dlg.exec() == QDialog.Accepted:
            try:
                self.ws.media.update_attachment(self.ws.user, a.id, title.text(), inc.isChecked(), sel.text())
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc)
            self._refresh()

    def _open(self, items, table) -> None:  # type: ignore[no-untyped-def]
        r = table.currentRow()
        if 0 <= r < len(items):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ws.files.path(items[r].file_id))))

    def _show_note(self, row: int) -> None:
        if 0 <= row < len(self.note_items):
            n = self.note_items[row]
            self._note_id = n.id
            self.section.setCurrentIndex(max(self.section.findData(n.section), 0))
            self.note_text.setPlainText(n.text)

    def _new_note(self) -> None:
        self._note_id = ""
        self.note_text.clear()
        self.notes_list.clearSelection()

    def _save_note(self) -> None:
        text = self.note_text.toPlainText().strip()
        if not text:
            return
        try:
            self._note_id = self.ws.media.save_note(self.ws.user, self.case_id, self.section.currentData(), text,
                                                    self._note_id)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
        self._refresh()
