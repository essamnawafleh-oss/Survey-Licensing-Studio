"""Excel rule-table wizard (inspect, preview, map, validate, import) and version verification."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from ...core.i18n import current_language, tr
from ...persistence.workspace import Workspace
from ...rules.excel_rules import OPTIONAL_FIELDS, REQUIRED_FIELDS, ColumnMapping, list_sheets, read_sheet, validate
from .common import fill_table, make_table, show_error


class RulesDialog(QDialog):
    def __init__(self, ws: Workspace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.path: Path | None = None
        self.preview = None
        self.validation = None
        self.setWindowTitle(tr("rules.title"))
        self.resize(1000, 700)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._import_tab(), tr("rules.tab_import"))
        tabs.addTab(self._versions_tab(), tr("rules.tab_versions"))
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        lay.addWidget(close)

    # -- import tab -----------------------------------------------------------------------

    def _import_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.file_lbl = QLineEdit()
        self.file_lbl.setReadOnly(True)
        browse = QPushButton(tr("rules.browse"))
        browse.clicked.connect(self._browse)
        self.sheet = QComboBox()
        self.sheet.currentIndexChanged.connect(self._load_preview)
        self.header_row = QSpinBox()
        self.header_row.setRange(1, 1000)
        self.header_row.valueChanged.connect(self._load_preview)
        row.addWidget(QLabel(tr("rules.workbook")))
        row.addWidget(self.file_lbl, 1)
        row.addWidget(browse)
        row.addWidget(QLabel(tr("rules.sheet")))
        row.addWidget(self.sheet)
        row.addWidget(QLabel(tr("rules.header_row")))
        row.addWidget(self.header_row)
        lay.addLayout(row)
        self.preview_table = make_table([])
        self.preview_table.setMaximumHeight(220)
        lay.addWidget(QLabel(tr("rules.preview")))
        lay.addWidget(self.preview_table)
        mid = QHBoxLayout()
        mg = QGroupBox(tr("rules.mapping"))
        self.map_form = QFormLayout(mg)
        self.map_boxes: dict[str, QComboBox] = {}
        for fld in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            cb = QComboBox()
            self.map_boxes[fld] = cb
            self.map_form.addRow(tr(f"rules.field.{fld}") + (" *" if fld in REQUIRED_FIELDS else ""), cb)
        self.extra = QPlainTextEdit()
        self.extra.setPlaceholderText(tr("rules.extra_hint"))
        self.extra.setMaximumHeight(60)
        self.map_form.addRow(tr("rules.extra"), self.extra)
        mid.addWidget(mg, 1)
        vg = QGroupBox(tr("rules.validation"))
        vl = QVBoxLayout(vg)
        self.errors = QListWidget()
        vl.addWidget(self.errors)
        self.valid_lbl = QLabel()
        vl.addWidget(self.valid_lbl)
        self.parsed = make_table([tr("rules.field.code"), tr("rpt.setback_m"), tr("check.coverage"),
                                  tr("check.floors"), tr("check.far"), tr("rpt.source_row")])
        vl.addWidget(self.parsed)
        mid.addWidget(vg, 1)
        lay.addLayout(mid, 1)
        bottom = QHBoxLayout()
        self.set_name = QLineEdit()
        self.set_name.setPlaceholderText(tr("rules.set_name_hint"))
        validate_btn = QPushButton(tr("rules.validate"))
        validate_btn.clicked.connect(self._validate)
        self.import_btn = QPushButton(tr("rules.import"))
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import)
        if not self.ws.user.can("import_rules"):
            self.import_btn.setToolTip(tr("perm.denied"))
        bottom.addWidget(QLabel(tr("rules.set_name")))
        bottom.addWidget(self.set_name, 1)
        bottom.addWidget(validate_btn)
        bottom.addWidget(self.import_btn)
        lay.addLayout(bottom)
        return w

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("rules.browse"), "", "Excel (*.xlsx *.xlsm)")
        if not path:
            return
        try:
            sheets = list_sheets(Path(path))
        except Exception as exc:  # noqa: BLE001 - shown to user
            show_error(self, exc)
            return
        self.path = Path(path)
        self.file_lbl.setText(path)
        if not self.set_name.text():
            self.set_name.setText(self.path.stem)
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.addItems(sheets)
        self.sheet.blockSignals(False)
        self._load_preview()

    def _load_preview(self) -> None:
        self.import_btn.setEnabled(False)
        if not self.path or not self.sheet.currentText():
            return
        try:
            self.preview = read_sheet(self.path, self.sheet.currentText(), self.header_row.value())
        except Exception as exc:  # noqa: BLE001 - header row may simply be wrong; show why
            self.preview = None
            self.preview_table.setColumnCount(0)
            self.valid_lbl.setText(str(exc))
            self.valid_lbl.setObjectName("error")
            return
        headers = self.preview.headers
        self.preview_table.setColumnCount(len(headers))
        self.preview_table.setHorizontalHeaderLabels(headers)
        fill_table(self.preview_table, [[c.value for c in r] for r in self.preview.rows[:50]])
        for fld, cb in self.map_boxes.items():
            prev = cb.currentText()
            cb.clear()
            cb.addItem("", "")
            for h in headers:
                if h:
                    cb.addItem(h, h)
            cb.setCurrentText(prev if prev in headers else "")
        self.valid_lbl.setText(tr("rules.rows_loaded", n=len(self.preview.rows)))
        self.valid_lbl.setObjectName("muted")

    def _mapping(self) -> ColumnMapping:
        columns = {f: cb.currentData() for f, cb in self.map_boxes.items() if cb.currentData()}
        extra = {}
        for line in self.extra.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                extra[k.strip()] = v.strip()
        return ColumnMapping(columns, extra)

    def _validate(self) -> None:
        self.errors.clear()
        self.import_btn.setEnabled(False)
        if self.preview is None:
            return
        self.validation = validate(self.preview, self._mapping())
        for e in self.validation.errors:
            self.errors.addItem(tr("rules.row_error", row=e.row, col=e.column, msg=e.message))
        lang = current_language()
        fill_table(self.parsed, [[r.label(lang), ", ".join(f"{k}={v:g}" for k, v in r.setbacks.items()),
                                  r.max_coverage_pct, r.max_floors, r.max_far, r.source_row]
                                 for r in self.validation.rules])
        if self.validation.ok:
            self.valid_lbl.setText(tr("rules.valid", n=len(self.validation.rules),
                                      blank=self.validation.skipped_blank_rows))
            self.valid_lbl.setObjectName("ok")
            self.import_btn.setEnabled(self.ws.user.can("import_rules"))
        else:
            self.valid_lbl.setText(tr("rules.invalid", n=len(self.validation.errors)))
            self.valid_lbl.setObjectName("error")
        self.valid_lbl.style().polish(self.valid_lbl)

    def _import(self) -> None:
        try:
            v = self.ws.rules.import_version(self.ws.user, self.set_name.text(), self.path, self.sheet.currentText(),
                                             self.header_row.value(), self._mapping())
        except Exception as exc:  # noqa: BLE001 - reported
            show_error(self, exc)
            return
        QMessageBox.information(self, tr("rules.title"), tr("rules.imported", label=v.label))
        self._load_versions()

    # -- versions tab --------------------------------------------------------------------------

    def _versions_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.versions = QListWidget()
        self.versions.currentRowChanged.connect(self._show_version)
        left.addWidget(self.versions)
        self.verify_btn = QPushButton(tr("rules.verify"))
        self.verify_btn.clicked.connect(self._verify)
        left.addWidget(self.verify_btn)
        lay.addLayout(left, 1)
        right = QVBoxLayout()
        self.version_info = QLabel()
        self.version_info.setWordWrap(True)
        right.addWidget(self.version_info)
        self.version_rules = make_table([tr("rules.field.code"), tr("rules.field.name_ar"), tr("rules.field.name_en"),
                                         tr("rpt.setback_m"), tr("check.coverage"), tr("check.floors"),
                                         tr("check.far"), tr("rpt.source_row")])
        right.addWidget(self.version_rules)
        lay.addLayout(right, 3)
        self._load_versions()
        return w

    def _load_versions(self) -> None:
        self.version_list = self.ws.rules.list_versions()
        self.versions.clear()
        for v in self.version_list:
            self.versions.addItem(v.label + ("  ✓" if v.verified else f"  ({tr('rules.unverified')})"))
        if self.version_list:
            self.versions.setCurrentRow(0)

    def _show_version(self, row: int) -> None:
        if row < 0 or row >= len(self.version_list):
            return
        v = self.version_list[row]
        self.version_info.setText(tr("rules.version_info", sheet=v.sheet_name, row=v.header_row, sha=v.workbook_sha256,
                                     by=v.imported_by, at=v.imported_at,
                                     verified=(tr("rules.verified_by", user=v.verified_by, at=v.verified_at)
                                               if v.verified else tr("rules.unverified"))))
        fill_table(self.version_rules, [[r.code, r.name_ar, r.name_en,
                                         ", ".join(f"{k}={val:g}" for k, val in r.setbacks.items()),
                                         r.max_coverage_pct, r.max_floors, r.max_far, r.source_row]
                                        for r in self.ws.rules.rules(v.id)])
        self.verify_btn.setEnabled(not v.verified and self.ws.user.can("verify_rules"))

    def _verify(self) -> None:
        row = self.versions.currentRow()
        if row < 0:
            return
        v = self.version_list[row]
        if QMessageBox.question(self, tr("rules.verify"), tr("rules.verify_confirm", label=v.label)) != QMessageBox.Yes:
            return
        try:
            self.ws.rules.verify_version(self.ws.user, v.id)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._load_versions()
