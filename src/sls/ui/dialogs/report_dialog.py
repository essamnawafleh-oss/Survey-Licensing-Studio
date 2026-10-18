"""Report options, live PDF preview (QtPdf) and export of a report revision."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from ...core.i18n import current_language, tr
from ...persistence.workspace import Workspace
from ...reporting.report import ReportOptions
from ...services.report_service import ReportService
from .common import show_error

try:  # QtPdf is part of PySide6-Addons
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
    HAVE_QTPDF = True
except ImportError:  # pragma: no cover - reported in the dialog
    HAVE_QTPDF = False


class ReportDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.case_id = ws, case_id
        self.service = ReportService(ws)
        self.last = None
        self.setWindowTitle(tr("report.title"))
        self.resize(1200, 820)
        lay = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)
        left = QWidget()
        ll = QVBoxLayout(left)
        g = QGroupBox(tr("report.options"))
        f = QFormLayout(g)
        self.lang = QComboBox()
        self.lang.addItem("العربية", "ar")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(0 if current_language() == "ar" else 1)
        self.page = QComboBox()
        self.page.addItems(["A4", "A3"])
        self.orient = QComboBox()
        self.orient.addItem(tr("report.portrait"), "portrait")
        self.orient.addItem(tr("report.landscape"), "landscape")
        self.dpage = QComboBox()
        self.dpage.addItems(["A3", "A4"])
        self.dorient = QComboBox()
        self.dorient.addItem(tr("report.landscape"), "landscape")
        self.dorient.addItem(tr("report.portrait"), "portrait")
        self.scale = QSpinBox()
        self.scale.setRange(0, 100000)
        self.scale.setSpecialValueText(tr("report.scale_fit"))
        self.scale.setPrefix("1:")
        self.margins = QDoubleSpinBox()
        self.margins.setRange(5, 40)
        self.margins.setValue(15)
        self.margins.setSuffix(" mm")
        self.authority = QLineEdit()
        self.title = QLineEdit()
        self.title.setPlaceholderText(tr("rpt.title"))
        self.header = QLineEdit()
        self.footer = QLineEdit()
        self.mono = QCheckBox(tr("report.monochrome"))
        self.sensitive = QCheckBox(tr("report.sensitive"))
        self.trace = QCheckBox(tr("report.trace"))
        self.trace.setChecked(True)
        self.status = QComboBox()
        for s, perm in (("draft", "generate_report"), ("reviewed", "review_report"), ("approved", "approve_report")):
            if ws.user.can(perm):
                self.status.addItem(tr(f"status.{s}"), s)
        for label, w in ((tr("report.language"), self.lang), (tr("report.page"), self.page),
                         (tr("report.orientation"), self.orient), (tr("report.drawing_page"), self.dpage),
                         (tr("report.drawing_orientation"), self.dorient), (tr("rpt.scale"), self.scale),
                         (tr("report.margins"), self.margins), (tr("report.authority"), self.authority),
                         (tr("report.title_field"), self.title), (tr("report.header"), self.header),
                         (tr("report.footer"), self.footer), ("", self.mono), ("", self.sensitive), ("", self.trace),
                         (tr("rpt.status"), self.status)):
            f.addRow(label, w)
        ll.addWidget(g)
        row = QHBoxLayout()
        preview = QPushButton(tr("report.preview"))
        preview.clicked.connect(self._preview)
        export = QPushButton(tr("report.export"))
        export.clicked.connect(self._export)
        row.addWidget(preview)
        row.addWidget(export)
        ll.addLayout(row)
        self.warnings = QListWidget()
        ll.addWidget(QLabel(tr("report.messages")))
        ll.addWidget(self.warnings, 1)
        self.history = QListWidget()
        ll.addWidget(QLabel(tr("report.revisions")))
        ll.addWidget(self.history)
        save_copy = QPushButton(tr("report.save_copy"))
        save_copy.clicked.connect(self._save_copy)
        ll.addWidget(save_copy)
        split.addWidget(left)
        if HAVE_QTPDF:
            self.doc = QPdfDocument(self)
            self.view = QPdfView()
            self.view.setDocument(self.doc)
            self.view.setPageMode(QPdfView.PageMode.MultiPage)
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            split.addWidget(self.view)
        else:
            split.addWidget(QLabel(tr("report.no_qtpdf")))
        split.setSizes([380, 820])
        self._buffer: QBuffer | None = None
        self._load_history()

    def options(self) -> ReportOptions:
        return ReportOptions(
            language=self.lang.currentData(), page_size=self.page.currentText(), orientation=self.orient.currentData(),
            drawing_page_size=self.dpage.currentText(), drawing_orientation=self.dorient.currentData(),
            drawing_scale=self.scale.value(), margins_mm=self.margins.value(), authority_name=self.authority.text(),
            report_title=self.title.text(), header_text=self.header.text(), footer_text=self.footer.text(),
            monochrome_drawing=self.mono.isChecked(), include_sensitive_fields=self.sensitive.isChecked(),
            include_trace=self.trace.isChecked(), status=self.status.currentData() or "draft")

    def _show(self, result) -> None:  # type: ignore[no-untyped-def]
        self.last = result
        self.warnings.clear()
        for w in result.warnings:
            self.warnings.addItem("⚠ " + w)
        self.warnings.addItem(tr("report.pages", n=result.page_count))
        if HAVE_QTPDF:
            self._bytes = QByteArray(result.pdf)
            self._buffer = QBuffer(self._bytes, self)
            self._buffer.open(QIODevice.ReadOnly)
            self.doc.load(self._buffer)

    def _preview(self) -> None:
        try:
            self._show(self.service.preview(self.case_id, self.options()))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)

    def _export(self) -> None:
        opts = self.options()
        if opts.status != "draft":
            blockers = self.service.approval_blockers(self.case_id, opts.status)
            if blockers:
                QMessageBox.warning(self, tr("report.title"), "\n".join(["• " + b for b in blockers]))
                return
        try:
            export, result = self.service.export(self.case_id, opts)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._show(result)
        self._load_history()
        msg = tr("report.exported", rev=export.revision, status=tr(f"status.{export.status}"))
        if result.warnings:
            msg += "\n\n" + tr("report.exported_with_warnings", n=len(result.warnings))
        QMessageBox.information(self, tr("report.title"), msg)

    def _load_history(self) -> None:
        self.reports = self.ws.reports.list(self.case_id)
        self.history.clear()
        for r in self.reports:
            self.history.addItem(f"r{r.revision}  {tr('status.' + r.status)}  {r.generated_at}  {r.generated_by}")

    def _save_copy(self) -> None:
        row = self.history.currentRow()
        if row < 0:
            QMessageBox.information(self, tr("report.title"), tr("report.pick_revision"))
            return
        rep = self.reports[row]
        path, _ = QFileDialog.getSaveFileName(self, tr("report.save_copy"),
                                              f"report_r{rep.revision:03d}_{rep.status}.pdf", "PDF (*.pdf)")
        if path:
            Path(path).write_bytes(self.ws.files.path(rep.file_id).read_bytes())
            Path(path).with_suffix(".manifest.json").write_text(
                json.dumps(rep.manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
