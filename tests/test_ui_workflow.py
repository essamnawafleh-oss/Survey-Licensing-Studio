"""End-to-end workflow through the real dialogs (synthetic data)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from conftest import ADMIN
from sls.core.i18n import set_language
from sls.core.units import LengthUnit
from sls.samples import SAMPLE_RULE_MAPPING, assign_sample_roles
from sls.services.case_service import CaseService


@pytest.fixture(autouse=True)
def _no_modal(monkeypatch):
    set_language("en")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: (_ for _ in ()).throw(AssertionError(a[2])))


def test_workflow_via_dialogs(qtbot, workspace, sample_dxf, sample_xlsx):
    from sls.ui.dialogs.boundary_dialog import BoundaryDialog
    from sls.ui.dialogs.case_dialogs import CaseDialog
    from sls.ui.dialogs.licenses_dialog import LicensesDialog
    from sls.ui.dialogs.rules_dialog import RulesDialog

    ws = workspace
    # 1. Rules wizard: load, map, validate, import, verify.
    rd = RulesDialog(ws)
    qtbot.addWidget(rd)
    rd.path = sample_xlsx
    rd.sheet.addItems(["Setbacks", "Notes"])
    rd.header_row.setValue(2)
    rd._load_preview()
    for fld, header in SAMPLE_RULE_MAPPING["columns"].items():
        rd.map_boxes[fld].setCurrentText(header)
    rd.set_name.setText("Synthetic")
    rd._validate()
    assert rd.import_btn.isEnabled(), [rd.errors.item(i).text() for i in range(rd.errors.count())]
    rd._import()
    rd.versions.setCurrentRow(0)
    rd._verify()
    version = ws.rules.list_versions()[0]
    assert version.verified

    # 2. Case dialog: create with units verified and land use selected.
    cd = CaseDialog(ws)
    qtbot.addWidget(cd)
    cd.number.setText("UI-001")
    cd.surveyor.setText("مساح")
    cd.survey_date.setText("2026-10-01")
    cd.unit.setCurrentIndex(cd.unit.findData("m"))
    cd.crs_kind.setCurrentIndex(cd.crs_kind.findData("local"))
    cd.crs_id.setEditText("site grid")
    cd.units_verified.setChecked(True)
    cd.version.setCurrentIndex(cd.version.findData(version.id))
    cd.land_use.setCurrentIndex(cd.land_use.findData("SYN-A"))
    assert "Front: 5" in cd.rule_info.text()
    cd.accept()
    case = ws.cases.get(cd.case.id)
    assert case.units.verified and case.units.unit is LengthUnit.METER and case.units.verified_by == ADMIN.username
    assert case.land_use_code == "SYN-A"

    # 3. Import drawing and classify layers.
    svc = CaseService(ws)
    drawing, report = svc.preview_import(sample_dxf)
    boundary = assign_sample_roles(drawing)
    loaded = svc.import_drawing(case.id, sample_dxf, "survey", drawing, report)

    # 4. Boundary dialog: select, classify, verify.
    bd = BoundaryDialog(ws, case.id, loaded.drawing, {boundary}, [], None)
    qtbot.addWidget(bd)
    bd._use_selection({boundary})
    for r, t in enumerate(["front", "side", "rear", "side"]):
        bd.table.cellWidget(r, 2).setCurrentText(t)
    bd._save(True)
    assert ws.drawings.parcel(case.id).verified

    # 5. License dialog: enter and verify a record.
    ld = LicensesDialog(ws, case.id)
    qtbot.addWidget(ld)
    ld.number.setText("SYN-LIC-77")
    ld.date.setText("2012-02-02")
    ld._add_row(ld.areas, "area", ("G", 150.0, ""))
    ld._add_row(ld.lengths, "length", ("fence", 30.0, ""))
    ld._save()
    rec = ws.licenses.list(case.id)[0]
    assert rec.areas[0].area_m2 == 150.0 and rec.lengths[0].kind == "fence"
    ld._set_status("verified")
    assert ws.licenses.get(rec.id).verification_status == "verified"

    # 6. Calculate and check reproducibility.
    calc = svc.run_calculation(case.id)
    assert calc.results["status"] == "ok", calc.results["errors"]
    assert calc.results["totals"]["remaining_to_assess_m2"] == pytest.approx(448 - 150)
    assert ws.calculations.reproduce(calc.id)[0]


def test_case_dialog_rejects_bad_date_and_unknown_unit_verification(qtbot, workspace, monkeypatch):
    from sls.ui.dialogs import case_dialogs

    errors = []
    monkeypatch.setattr(case_dialogs, "show_error", lambda parent, exc: errors.append(str(exc)))
    cd = case_dialogs.CaseDialog(workspace)
    qtbot.addWidget(cd)
    cd.number.setText("X")
    cd.survey_date.setText("1/2/2026")
    cd.accept()
    cd.survey_date.setText("")
    assert cd.unit.currentData() == "m" and cd._crs_code() == "EPSG:3066"  # JTM pre-selected, not verified
    assert not cd.units_verified.isChecked()
    cd.unit.setCurrentIndex(cd.unit.findData("unknown"))
    cd.units_verified.setChecked(True)  # with unit "unknown"
    cd.accept()
    assert len(errors) == 2
    assert workspace.cases.list() == []
