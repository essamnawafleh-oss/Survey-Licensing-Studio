"""Case creation/editing (configurable fields, units/CRS verification, land-use) and the case browser."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (QCheckBox, QMessageBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ...core.i18n import current_language, tr
from ...core.crs import PRESETS, CrsUnavailable, check_extents, describe
from ...core.units import CrsKind, DrawingUnits, LengthUnit
from ...persistence.cases import CASE_STATUSES, Case
from ...persistence.workspace import Workspace
from .common import fill_table, make_table, ok_cancel, show_error

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CaseDialog(QDialog):
    def __init__(self, ws: Workspace, case: Case | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.case = case or Case()
        self.setWindowTitle(tr("case.edit_title") if case else tr("case.new_title"))
        self.resize(620, 720)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        g = QGroupBox(tr("case.group.main"))
        f = QFormLayout(g)
        self.number = QLineEdit(self.case.case_number)
        self.parcel = QLineEdit(self.case.parcel_identifier)
        self.status = QComboBox()
        for s in CASE_STATUSES:
            self.status.addItem(tr(f"case_status.{s}"), s)
        self.status.setCurrentIndex(CASE_STATUSES.index(self.case.status))
        self.survey_date = QLineEdit(self.case.survey_date)
        self.survey_date.setPlaceholderText("YYYY-MM-DD")
        self.surveyor = QLineEdit(self.case.surveyor)
        self.notes = QPlainTextEdit(self.case.notes)
        self.notes.setMaximumHeight(70)
        f.addRow(tr("case.number") + " *", self.number)
        f.addRow(tr("case.parcel"), self.parcel)
        f.addRow(tr("case.status"), self.status)
        f.addRow(tr("case.survey_date"), self.survey_date)
        f.addRow(tr("case.surveyor"), self.surveyor)
        f.addRow(tr("case.notes"), self.notes)
        lay.addWidget(g)

        g2 = QGroupBox(tr("case.group.fields"))
        f2 = QFormLayout(g2)
        self.field_widgets: dict[str, QWidget] = {}
        lang = current_language()
        for fd in ws.cases.field_defs():
            value = self.case.fields.get(fd.key, "")
            if fd.field_type == "choice" and fd.choices:
                w: QWidget = QComboBox()
                w.addItems([""] + fd.choices)
                w.setCurrentText(value)
            elif fd.field_type == "multiline":
                w = QPlainTextEdit(value)
                w.setMaximumHeight(60)
            else:
                w = QLineEdit(value)
                if fd.field_type == "date":
                    w.setPlaceholderText("YYYY-MM-DD")
            label = fd.label(lang) + (" *" if fd.required else "") + (f" ({tr('case.sensitive')})" if fd.sensitive else "")
            f2.addRow(label, w)
            self.field_widgets[fd.key] = w
        lay.addWidget(g2)

        g3 = QGroupBox(tr("case.group.units"))
        f3 = QFormLayout(g3)
        self.unit = QComboBox()
        for u in LengthUnit:
            self.unit.addItem(tr(f"unit_name.{u.value}"), u.value)
        self.unit.setCurrentIndex([u for u in LengthUnit].index(self.case.units.unit))
        self.crs_id = QComboBox()
        self.crs_id.setEditable(True)
        for code, name in PRESETS:
            self.crs_id.addItem(f"{code} — {name}", code)
        self.crs_id.lineEdit().setPlaceholderText(tr("case.crs_hint"))
        self.crs_info = QLabel()
        self.crs_info.setWordWrap(True)
        self.crs_kind = QComboBox()
        for k in CrsKind:
            self.crs_kind.addItem(tr(f"crs_kind.{k.value}"), k.value)
        self.crs_kind.setCurrentIndex([k for k in CrsKind].index(self.case.units.crs_kind))
        self.units_verified = QCheckBox(tr("case.units_verified"))
        self.units_verified.setChecked(self.case.units.verified)
        self.units_verified.setEnabled(ws.user.can("verify_units"))
        hint = QLabel(tr("case.units_note"))
        hint.setWordWrap(True)
        hint.setObjectName("warning")
        f3.addRow(tr("case.units"), self.unit)
        f3.addRow(tr("case.crs_id"), self.crs_id)
        f3.addRow("", self.crs_info)
        f3.addRow(tr("case.crs_kind"), self.crs_kind)
        f3.addRow("", self.units_verified)
        if self.case.units.verified_by:
            f3.addRow("", QLabel(tr("case.verified_by", user=self.case.units.verified_by)))
        f3.addRow(hint)
        for w in (self.unit, self.crs_kind):
            w.currentIndexChanged.connect(lambda _=None: self.units_verified.setChecked(False))
        initial = self.case.units.crs_id
        if not self.case.id and not initial:
            # New case: the department's system (JTM by default) is pre-selected but NOT verified.
            initial = ws.settings.default_crs
            if self.case.units.unit is LengthUnit.UNKNOWN:
                self.unit.setCurrentIndex([u for u in LengthUnit].index(LengthUnit.METER))
        self._set_crs_text(initial)
        self._on_crs()
        self.crs_id.currentTextChanged.connect(lambda _=None: (self.units_verified.setChecked(False), self._on_crs()))
        lay.addWidget(g3)

        g4 = QGroupBox(tr("case.group.landuse"))
        f4 = QFormLayout(g4)
        self.version = QComboBox()
        self.version.addItem(tr("case.no_rule_version"), None)
        for v in ws.rules.list_versions():
            self.version.addItem(v.label + ("" if v.verified else f"  ({tr('rules.unverified')})"), v.id)
        idx = self.version.findData(self.case.rule_version_id)
        self.version.setCurrentIndex(max(idx, 0))
        self.land_use = QComboBox()
        self.rule_info = QLabel()
        self.rule_info.setWordWrap(True)
        self.version.currentIndexChanged.connect(self._load_codes)
        self.land_use.currentIndexChanged.connect(self._show_rule)
        f4.addRow(tr("case.rule_version"), self.version)
        f4.addRow(tr("case.land_use"), self.land_use)
        f4.addRow(self.rule_info)
        lay.addWidget(g4)
        self._load_codes()
        outer.addWidget(ok_cancel(self))

    def _set_crs_text(self, code: str) -> None:
        idx = self.crs_id.findData(code)
        if idx >= 0:
            self.crs_id.setCurrentIndex(idx)
        else:
            self.crs_id.setEditText(code)

    def _crs_code(self) -> str:
        text = self.crs_id.currentText().strip()
        idx = self.crs_id.findText(text)
        return self.crs_id.itemData(idx) if idx >= 0 else text

    def _on_crs(self) -> None:
        code = self._crs_code()
        if not code:
            self.crs_info.setText("")
            return
        try:
            info = describe(code)
        except CrsUnavailable:
            self.crs_info.setText(tr("case.crs_unknown"))
            self.crs_info.setObjectName("muted")
            return
        kind = {"projected": CrsKind.PROJECTED, "geographic": CrsKind.GEOGRAPHIC}.get(info.kind)
        if kind is not None:
            self.crs_kind.setCurrentIndex([k for k in CrsKind].index(kind))
        if info.kind == "projected" and info.unit == "metre":
            self.unit.setCurrentIndex([u for u in LengthUnit].index(LengthUnit.METER))
        self.crs_info.setText(tr("case.crs_described", name=info.name, unit=info.unit, area=info.area_name))
        self.crs_info.setObjectName("ok" if info.kind == "projected" else "error")
        self.crs_info.style().polish(self.crs_info)

    def _survey_extents(self):  # type: ignore[no-untyped-def]
        if not self.case.id:
            return None
        drawings = self.ws.drawings.for_case(self.case.id, "survey")
        if not drawings:
            return None
        rev = self.ws.drawings.latest_revision(drawings[-1].id)
        return self.ws.drawings.load(rev.id).extents(only_visible=False) if rev else None

    def _load_codes(self) -> None:
        self.land_use.blockSignals(True)
        self.land_use.clear()
        vid = self.version.currentData()
        self.land_use.addItem("", "")
        if vid:
            for r in self.ws.rules.rules(vid):
                self.land_use.addItem(r.label(current_language()), r.code)
        idx = self.land_use.findData(self.case.land_use_code)
        if idx < 0 and self.case.land_use_code:
            self.land_use.addItem(tr("case.code_missing", code=self.case.land_use_code), self.case.land_use_code)
            idx = self.land_use.count() - 1
        self.land_use.setCurrentIndex(max(idx, 0))
        self.land_use.blockSignals(False)
        self._show_rule()

    def _show_rule(self) -> None:
        vid, code = self.version.currentData(), self.land_use.currentData()
        if not vid or not code:
            self.rule_info.setText(tr("case.rule_none"))
            self.rule_info.setObjectName("muted")
            return
        try:
            r = self.ws.rules.rule(vid, code)
        except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
            self.rule_info.setText(str(exc))
            self.rule_info.setObjectName("error")
            return
        def bt(k: str) -> str:
            return tr("btype." + k) if k in ("front", "side", "rear") else k

        raw = r.setbacks_raw or {k: f"{v:g}" for k, v in r.setbacks.items()}
        parts = [f"{bt(k)}: {v or '—'}" for k, v in raw.items()]
        extra = []
        if r.coverage_raw or r.max_coverage_pct is not None:
            extra.append(f"{tr('check.coverage')}: {r.coverage_raw or f'{r.max_coverage_pct:g}%'}")
        if r.floors_raw or r.max_floors is not None:
            extra.append(f"{tr('check.floors')}: {r.floors_raw or r.max_floors}")
        text = " · ".join(parts + extra) + f"  ({tr('rpt.source_row')} {r.source_row})"
        if r.notes:
            text += f"\n{r.notes}"
        if r.status != "numeric":
            text += "\n" + tr(f"rules.st_hint.{r.status}")
        self.rule_info.setText(text)
        self.rule_info.setObjectName("ok" if r.status == "numeric" else "warning")
        self.rule_info.style().polish(self.rule_info)

    def accept(self) -> None:
        if self.survey_date.text().strip() and not _DATE.match(self.survey_date.text().strip()):
            show_error(self, ValueError(tr("case.bad_date")))
            return
        c = self.case
        c.case_number = self.number.text().strip()
        c.parcel_identifier = self.parcel.text().strip()
        c.status = self.status.currentData()
        c.survey_date = self.survey_date.text().strip()
        c.surveyor = self.surveyor.text().strip()
        c.notes = self.notes.toPlainText()
        fields = {}
        for key, w in self.field_widgets.items():
            if isinstance(w, QComboBox):
                v = w.currentText()
            elif isinstance(w, QPlainTextEdit):
                v = w.toPlainText()
            else:
                v = w.text()
            if v.strip():
                fields[key] = v.strip()
        c.fields = fields
        verified = self.units_verified.isChecked()
        crs_code = self._crs_code()
        if verified and not c.units.verified and crs_code:
            ext = self._survey_extents()
            try:
                check = check_extents(crs_code, ext, LengthUnit(self.unit.currentData()).to_meter) if ext else None
            except (CrsUnavailable, Exception):  # noqa: BLE001 - unknown/local system: nothing to check
                check = None
            if check is not None and not check.ok and QMessageBox.question(
                    self, tr("case.units"), check.message + "\n\n" + tr("case.verify_anyway"),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        new_units = DrawingUnits(LengthUnit(self.unit.currentData()), verified, crs_code,
                                 CrsKind(self.crs_kind.currentData()),
                                 (self.ws.user.username if verified and not c.units.verified else c.units.verified_by)
                                 if verified else "")
        if verified and new_units.unit is LengthUnit.UNKNOWN:
            show_error(self, ValueError(tr("case.cannot_verify_unknown")))
            return
        c.units = new_units
        c.rule_version_id = self.version.currentData()
        c.land_use_code = self.land_use.currentData() or ""
        try:
            self.case = self.ws.cases.save(self.ws.user, c)
        except Exception as exc:  # noqa: BLE001 - reported to user
            show_error(self, exc)
            return
        super().accept()


class CaseBrowser(QDialog):
    def __init__(self, ws: Workspace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.selected_id: str | None = None
        self.setWindowTitle(tr("case.browser_title"))
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("case.search"))
        self.search.textChanged.connect(self.refresh)
        top.addWidget(self.search)
        lay.addLayout(top)
        self.table = make_table([tr("case.number"), tr("case.parcel"), tr("case.status"), tr("case.surveyor"),
                                 tr("case.updated")])
        self.table.doubleClicked.connect(self.accept)
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        open_btn = QPushButton(tr("case.open"))
        open_btn.setDefault(True)
        open_btn.clicked.connect(self.accept)
        cancel = QPushButton(tr("common.cancel"))
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(open_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        self.refresh()

    def refresh(self) -> None:
        self.cases = self.ws.cases.list(self.search.text())
        fill_table(self.table, [[c.case_number, c.parcel_identifier, tr(f"case_status.{c.status}"), c.surveyor,
                                 c.updated_at] for c in self.cases])

    def accept(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.cases):
            return
        self.selected_id = self.cases[row].id
        super().accept()

