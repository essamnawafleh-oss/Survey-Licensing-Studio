"""Previous-license register: manual entry, documents, verification by reviewers, optional OCR."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTableWidgetItem, QTextBrowser, QVBoxLayout,
                               QWidget)
from PySide6.QtCore import Qt

from ...core.i18n import tr
from ...persistence.licenses_repo import ApprovedViolation, LicensedArea, LicensedLength, LicenseRecord
from ...persistence.workspace import Workspace
from .common import make_table, show_error


class LicensesDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.case_id = ws, case_id
        self.current: LicenseRecord | None = None
        self.setWindowTitle(tr("lic.title"))
        self.resize(1100, 720)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)
        left = QWidget()
        ll = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        ll.addWidget(self.list)
        new = QPushButton(tr("lic.new"))
        new.clicked.connect(self._new)
        ll.addWidget(new)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        g = QGroupBox(tr("lic.record"))
        f = QFormLayout(g)
        self.number = QLineEdit()
        self.date = QLineEdit()
        self.date.setPlaceholderText("YYYY-MM-DD")
        self.structure = QLineEdit()
        self.fee = QLineEdit()
        self.fee.setPlaceholderText(tr("lic.fee_hint"))
        self.currency = QLineEdit()
        self.source_kind = QComboBox()
        for k in ("transcribed", "ocr_reviewed", "imported"):
            self.source_kind.addItem(tr(f"lic.source.{k}"), k)
        f.addRow(tr("rpt.license_number") + " *", self.number)
        f.addRow(tr("rpt.issue_date"), self.date)
        f.addRow(tr("rpt.structure"), self.structure)
        fee_row = QHBoxLayout()
        fee_row.addWidget(self.fee)
        fee_row.addWidget(QLabel(tr("lic.currency")))
        fee_row.addWidget(self.currency)
        f.addRow(tr("rpt.fees"), fee_row)
        f.addRow(tr("lic.source_kind"), self.source_kind)
        rl.addWidget(g)

        tables = QHBoxLayout()
        self.areas = self._qty_table([tr("rpt.floor"), tr("lic.area_m2"), tr("rpt.remarks")])
        self.lengths = self._qty_table([tr("rpt.element"), tr("lic.length_m"), tr("rpt.remarks")])
        self.violations = self._qty_table([tr("lic.violation_desc"), tr("lic.area_m2")])
        for title, t, kind in ((tr("rpt.licensed_areas"), self.areas, "area"),
                               (tr("rpt.licensed_lengths"), self.lengths, "length"),
                               (tr("rpt.approved_violations"), self.violations, "violation")):
            box = QGroupBox(title)
            bl = QVBoxLayout(box)
            bl.addWidget(t)
            br = QHBoxLayout()
            add = QPushButton("+")
            add.clicked.connect(lambda _=False, tt=t, k=kind: self._add_row(tt, k))
            rem = QPushButton("−")
            rem.clicked.connect(lambda _=False, tt=t: tt.removeRow(tt.currentRow()) if tt.currentRow() >= 0 else None)
            br.addWidget(add)
            br.addWidget(rem)
            br.addStretch(1)
            bl.addLayout(br)
            tables.addWidget(box)
        rl.addLayout(tables)

        dg = QGroupBox(tr("rpt.documents"))
        dl = QVBoxLayout(dg)
        self.docs = make_table([tr("rpt.file"), tr("lic.page_refs"), tr("lic.include"), tr("lic.page_selection")])
        dl.addWidget(self.docs)
        dr = QHBoxLayout()
        for key, slot in (("attach", self._attach), ("open", self._open_doc), ("doc_options", self._doc_options),
                          ("ocr", self._ocr)):
            b = QPushButton(tr(f"lic.{key}"))
            b.clicked.connect(slot)
            dr.addWidget(b)
        dr.addStretch(1)
        dl.addLayout(dr)
        rl.addWidget(dg)

        vg = QGroupBox(tr("rpt.verification"))
        vl = QFormLayout(vg)
        self.status_lbl = QLabel()
        self.issues_lbl = QLabel()
        self.issues_lbl.setObjectName("warning")
        self.issues_lbl.setWordWrap(True)
        self.reviewer_notes = QPlainTextEdit()
        self.reviewer_notes.setMaximumHeight(50)
        vl.addRow(tr("lic.status"), self.status_lbl)
        vl.addRow(tr("rpt.flags"), self.issues_lbl)
        vl.addRow(tr("lic.reviewer_notes"), self.reviewer_notes)
        vrow = QHBoxLayout()
        self.verify_btns = []
        for status in ("verified", "disputed", "unverified"):
            b = QPushButton(tr(f"lic.mark.{status}"))
            b.clicked.connect(lambda _=False, s=status: self._set_status(s))
            b.setEnabled(ws.user.can("verify_license"))
            vrow.addWidget(b)
            self.verify_btns.append(b)
        vl.addRow(vrow)
        rl.addWidget(vg)
        note = QLabel(tr("lic.edit_note"))
        note.setObjectName("muted")
        note.setWordWrap(True)
        rl.addWidget(note)
        save_row = QHBoxLayout()
        save = QPushButton(tr("lic.save"))
        save.setDefault(True)
        save.clicked.connect(self._save)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        save_row.addStretch(1)
        save_row.addWidget(save)
        save_row.addWidget(close)
        rl.addLayout(save_row)
        split.addWidget(right)
        split.setSizes([260, 840])
        self._reload()

    def _qty_table(self, headers: list[str]):  # type: ignore[no-untyped-def]
        t = make_table(headers, editable=True)
        t.setMinimumHeight(110)
        return t

    def _add_row(self, t, kind: str, values: tuple = ()) -> None:  # type: ignore[no-untyped-def]
        r = t.rowCount()
        t.insertRow(r)
        if kind == "length":
            combo = QComboBox()
            combo.addItem(tr("kind.wall"), "wall")
            combo.addItem(tr("kind.fence"), "fence")
            if values:
                combo.setCurrentIndex(0 if values[0] == "wall" else 1)
            t.setCellWidget(r, 0, combo)
            start = 1
        else:
            start = 0
            if values:
                t.setItem(r, 0, QTableWidgetItem(str(values[0])))
        for j, v in enumerate(values[1:] if values else (), start=1):
            t.setItem(r, j, QTableWidgetItem("" if v is None else str(v)))
        if not values and start == 1:
            t.setItem(r, 1, QTableWidgetItem(""))

    def _reload(self, select_id: str | None = None) -> None:
        self.records = self.ws.licenses.list(self.case_id)
        self.list.blockSignals(True)
        self.list.clear()
        sel = 0
        for i, rec in enumerate(self.records):
            mark = {"verified": "✓", "disputed": "✗", "unverified": "?"}[rec.verification_status]
            self.list.addItem(f"{mark}  {rec.license_number}  {rec.issue_date}  (r{rec.revision})")
            if rec.id == select_id:
                sel = i
        self.list.blockSignals(False)
        if self.records:
            self.list.setCurrentRow(sel)
            self._select(sel)
        else:
            self._new()

    def _new(self) -> None:
        self.current = LicenseRecord(self.case_id, "")
        self._show(self.current)

    def _select(self, row: int) -> None:
        if 0 <= row < len(self.records):
            self.current = self.ws.licenses.get(self.records[row].id)
            self._show(self.current)

    def _show(self, rec: LicenseRecord) -> None:
        self.number.setText(rec.license_number)
        self.date.setText(rec.issue_date)
        self.structure.setText(rec.structure_description)
        self.fee.setText("" if rec.fee_amount is None else f"{rec.fee_amount:g}")
        self.currency.setText(rec.fee_currency)
        self.source_kind.setCurrentIndex(max(self.source_kind.findData(rec.source_kind), 0))
        for t in (self.areas, self.lengths, self.violations):
            t.setRowCount(0)
        for a in rec.areas:
            self._add_row(self.areas, "area", (a.floor_key, a.area_m2, a.note))
        for x in rec.lengths:
            self._add_row(self.lengths, "length", (x.kind, x.length_m, x.note))
        for v in rec.violations:
            self._add_row(self.violations, "violation", (v.description, v.area_m2))
        self.docs.setRowCount(0)
        for d in rec.documents:
            r = self.docs.rowCount()
            self.docs.insertRow(r)
            for j, val in enumerate((d.original_name, d.page_refs, tr("common.yes") if d.include_in_report
                                     else tr("common.no"), d.page_selection or tr("lic.all_pages"))):
                self.docs.setItem(r, j, QTableWidgetItem(val))
        st = rec.verification_status
        who = f" — {rec.verified_by} {rec.verified_at}" if rec.verified_by else ""
        self.status_lbl.setText(tr(f"verif.{st}") + who + (f"  (r{rec.revision})" if rec.id else ""))
        self.status_lbl.setObjectName({"verified": "ok", "disputed": "error"}.get(st, "warning"))
        self.status_lbl.style().polish(self.status_lbl)
        self.issues_lbl.setText("; ".join(tr(k) for k in rec.completeness_issues()) if rec.id else "")
        self.reviewer_notes.setPlainText(rec.reviewer_notes)
        for b in self.verify_btns:
            b.setEnabled(bool(rec.id) and self.ws.user.can("verify_license"))

    def _float(self, text: str, what: str) -> float:
        try:
            return float(text.strip().replace(",", ""))
        except ValueError as exc:
            raise ValueError(tr("lic.bad_number", what=what, value=text)) from exc

    def _collect(self) -> LicenseRecord:
        rec = self.current or LicenseRecord(self.case_id, "")
        rec.license_number = self.number.text().strip()
        rec.issue_date = self.date.text().strip()
        rec.structure_description = self.structure.text().strip()
        rec.fee_amount = self._float(self.fee.text(), tr("rpt.fees")) if self.fee.text().strip() else None
        rec.fee_currency = self.currency.text().strip()
        rec.source_kind = self.source_kind.currentData()
        rec.reviewer_notes = self.reviewer_notes.toPlainText()

        def cell(t, r, c) -> str:  # type: ignore[no-untyped-def]
            item = t.item(r, c)
            return item.text().strip() if item else ""

        rec.areas = [LicensedArea(cell(self.areas, r, 0), self._float(cell(self.areas, r, 1), tr("lic.area_m2")),
                                  cell(self.areas, r, 2)) for r in range(self.areas.rowCount())
                     if cell(self.areas, r, 0) or cell(self.areas, r, 1)]
        rec.lengths = [LicensedLength(self.lengths.cellWidget(r, 0).currentData(),
                                      self._float(cell(self.lengths, r, 1), tr("lic.length_m")), cell(self.lengths, r, 2))
                       for r in range(self.lengths.rowCount()) if cell(self.lengths, r, 1)]
        rec.violations = [ApprovedViolation(cell(self.violations, r, 0),
                                            self._float(cell(self.violations, r, 1), tr("lic.area_m2"))
                                            if cell(self.violations, r, 1) else None)
                          for r in range(self.violations.rowCount()) if cell(self.violations, r, 0)]
        return rec

    def _save(self) -> None:
        try:
            rec = self._collect()
            if rec.id and rec.verification_status == "verified" and QMessageBox.question(
                    self, tr("lic.save"), tr("lic.save_resets")) != QMessageBox.Yes:
                return
            saved = self.ws.licenses.save(self.ws.user, rec)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._reload(saved.id)

    def _set_status(self, status: str) -> None:
        if not self.current or not self.current.id:
            return
        try:
            self.ws.licenses.set_verification(self.ws.user, self.current.id, status,
                                              self.reviewer_notes.toPlainText())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._reload(self.current.id)

    def _attach(self) -> None:
        if not self.current or not self.current.id:
            QMessageBox.information(self, tr("lic.title"), tr("lic.save_first"))
            return
        paths, _ = QFileDialog.getOpenFileNames(self, tr("lic.attach"))
        for p in paths:
            refs, _ok = QInputDialog.getText(self, tr("lic.page_refs"), tr("lic.page_refs_prompt", name=Path(p).name))
            try:
                self.ws.licenses.attach_document(self.ws.user, self.current.id, Path(p), refs)
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc)
        self._reload(self.current.id)

    def _doc(self):  # type: ignore[no-untyped-def]
        r = self.docs.currentRow()
        if not self.current or r < 0 or r >= len(self.current.documents):
            return None
        return self.current.documents[r]

    def _open_doc(self) -> None:
        d = self._doc()
        if d:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ws.files.path(d.file_id))))

    def _doc_options(self) -> None:
        d = self._doc()
        if not d:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(d.original_name)
        f = QFormLayout(dlg)
        inc = QCheckBox()
        inc.setChecked(d.include_in_report)
        sel = QLineEdit(d.page_selection)
        sel.setPlaceholderText(tr("lic.page_selection_hint"))
        refs = QLineEdit(d.page_refs)
        f.addRow(tr("lic.include"), inc)
        f.addRow(tr("lic.page_selection"), sel)
        f.addRow(tr("lic.page_refs"), refs)
        from .common import ok_cancel

        f.addRow(ok_cancel(dlg))
        if dlg.exec() == QDialog.Accepted:
            try:
                self.ws.licenses.update_document(self.ws.user, d.id, inc.isChecked(), sel.text(), refs.text())
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc)
            self._reload(self.current.id)

    def _ocr(self) -> None:
        d = self._doc()
        if not d:
            return
        from ...licensing.ocr import ocr_file

        try:
            text, langs = ocr_file(self.ws.files.path(d.file_id), self.ws.settings.tesseract_path)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.ws.licenses.add_ocr_text(d.file_id, "tesseract", langs, text)
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("lic.ocr_title"))
        dlg.resize(700, 600)
        v = QVBoxLayout(dlg)
        warn = QLabel(tr("lic.ocr_warning"))
        warn.setObjectName("warning")
        warn.setWordWrap(True)
        v.addWidget(warn)
        tb = QTextBrowser()
        tb.setPlainText(text)
        v.addWidget(tb)
        dlg.exec()

