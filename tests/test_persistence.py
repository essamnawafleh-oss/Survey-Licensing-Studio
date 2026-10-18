"""Persistence, workflow, permissions, audit and backup/restore tests."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from conftest import ADMIN, REVIEWER, SURVEYOR
from sls.core.errors import PermissionDeniedError, RuleImportError, ValidationError
from sls.core.settings import Settings
from sls.core.units import CrsKind, DrawingUnits, LengthUnit
from sls.persistence.backup import list_backups, restore_backup
from sls.persistence.cases import Case
from sls.persistence.licenses_repo import LicensedArea, LicenseRecord
from sls.persistence.workspace import Workspace
from sls.rules.excel_rules import ColumnMapping
from sls.samples import SAMPLE_EDGE_TYPES, SAMPLE_RULE_MAPPING, assign_sample_roles
from sls.services.case_service import CaseService


def make_case(ws, number="SYN-2026-001", **kw):
    return ws.cases.save(ws.user, Case(case_number=number, parcel_identifier="SYN-PLOT-123",
                                       surveyor="مساح تجريبي", fields={"basin": "حوض تجريبي"}, **kw))


def test_schema_migrated_and_defaults_seeded(workspace):
    assert workspace.db.schema_version() >= 1
    keys = [f.key for f in workspace.cases.field_defs()]
    assert "owner_name" in keys and "plot_number" in keys


def test_case_create_save_reopen(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "d"))
    ws = Workspace(settings, user=ADMIN)
    case = make_case(ws)
    case.notes = "ملاحظة"
    ws.cases.save(ws.user, case)
    ws.close()
    ws2 = Workspace(settings, user=ADMIN)
    again = ws2.cases.get(case.id)
    assert again.case_number == "SYN-2026-001" and again.fields["basin"] == "حوض تجريبي"
    assert again.notes == "ملاحظة"
    assert [a["action"] for a in ws2.audit.for_case(case.id)] == ["create_case", "update_case"]
    ws2.close()


def test_case_validation(workspace):
    with pytest.raises(ValidationError):
        workspace.cases.save(ADMIN, Case(case_number=""))
    make_case(workspace)
    with pytest.raises(ValidationError):
        make_case(workspace)  # duplicate number
    with pytest.raises(ValidationError):
        workspace.cases.save(ADMIN, Case(case_number="X", fields={"unknown_field": "1"}))


def test_file_store_never_overwrites_and_verifies(workspace, tmp_path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 a")
    case = make_case(workspace)
    a = workspace.files.add(ADMIN, src, case.id, "attachment")
    src.write_bytes(b"%PDF-1.4 b")  # same name, different content
    b = workspace.files.add(ADMIN, src, case.id, "attachment")
    assert a.stored_path != b.stored_path
    assert workspace.store.verify(a.stored_path, a.sha256)
    assert workspace.files.path(a.id).read_bytes() == b"%PDF-1.4 a"
    c = workspace.files.add(ADMIN, src, case.id, "attachment")
    assert c.id == b.id  # identical content is reused, not duplicated


def test_rule_import_versioning_and_verification(workspace, sample_xlsx):
    mapping = ColumnMapping.from_dict(SAMPLE_RULE_MAPPING)
    with pytest.raises(PermissionDeniedError):
        workspace.rules.import_version(SURVEYOR, "Synthetic", sample_xlsx, "Setbacks", 2, mapping)
    v1 = workspace.rules.import_version(REVIEWER, "Synthetic", sample_xlsx, "Setbacks", 2, mapping)
    v2 = workspace.rules.import_version(REVIEWER, "Synthetic", sample_xlsx, "Setbacks", 2, mapping)
    assert (v1.version, v2.version) == (1, 2)
    assert v1.workbook_sha256 == v2.workbook_sha256 and not v1.verified
    assert workspace.rules.rule(v1.id, "SYN-B").setbacks["front"] == 4
    workspace.rules.verify_version(REVIEWER, v1.id)
    assert workspace.rules.get_version(v1.id).verified
    with pytest.raises(RuleImportError):
        workspace.rules.import_version(REVIEWER, "Synthetic", sample_xlsx, "Setbacks", 1, mapping)  # wrong header


def test_license_edit_resets_verification_and_bumps_revision(workspace, tmp_path):
    case = make_case(workspace)
    rec = workspace.licenses.save(SURVEYOR, LicenseRecord(case.id, "SYN-LIC-1", issue_date="2015-03-01",
                                                          areas=[LicensedArea("G", 150.0)]))
    with pytest.raises(PermissionDeniedError):
        workspace.licenses.set_verification(SURVEYOR, rec.id, "verified")
    rec = workspace.licenses.set_verification(REVIEWER, rec.id, "verified", "checked against scan")
    assert rec.verification_status == "verified" and rec.verified_by == "reviewer1"
    rec.areas[0].area_m2 = 155.0
    rec = workspace.licenses.save(SURVEYOR, rec)
    assert rec.revision == 2 and rec.verification_status == "unverified"
    doc = tmp_path / "scan.png"
    doc.write_bytes(b"\x89PNG fake")
    workspace.licenses.attach_document(SURVEYOR, rec.id, doc, page_refs="p.1")
    assert len(workspace.licenses.get(rec.id).documents) == 1
    with pytest.raises(ValidationError):
        workspace.licenses.save(SURVEYOR, LicenseRecord(case.id, "", issue_date="1/2/2015"))
    audit = [a["action"] for a in workspace.audit.for_case(case.id)]
    assert audit.count("update_license") == 1 and "license_verification" in audit


def test_full_workflow_calculation_traceable_and_reproducible(workspace, sample_dxf, sample_xlsx):
    svc = CaseService(workspace)
    v = workspace.rules.import_version(ADMIN, "Synthetic", sample_xlsx, "Setbacks", 2,
                                       ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    workspace.rules.verify_version(ADMIN, v.id)
    case = make_case(workspace)
    loaded = svc.import_drawing(case.id, sample_dxf)
    assert workspace.files.get(loaded.record.source_file_id).original_name == sample_dxf.name
    case = workspace.cases.get(case.id)
    assert case.units.unit is LengthUnit.METER and not case.units.verified
    # Units unverified -> calculation reports the error instead of guessing.
    calc0 = svc.run_calculation(case.id)
    assert calc0.results["status"] == "error"

    case.units = DrawingUnits(LengthUnit.METER, True, "synthetic-local", CrsKind.LOCAL, ADMIN.username)
    case.rule_version_id, case.land_use_code = v.id, "SYN-B"
    workspace.cases.save(ADMIN, case)
    drawing = loaded.drawing
    boundary_id = assign_sample_roles(drawing)
    workspace.drawings.save_revision(ADMIN, loaded.record.id, drawing, "classified layers")
    parcel = workspace.drawings.parcel(case.id)
    parcel.boundary_entity_id = boundary_id
    parcel.boundary_points = [[p[0], p[1]] for p in drawing.entities[boundary_id].data["points"]]
    parcel.edge_types = SAMPLE_EDGE_TYPES
    workspace.drawings.save_parcel(ADMIN, parcel, verify=True)
    lic = workspace.licenses.save(ADMIN, LicenseRecord(case.id, "SYN-LIC-001", issue_date="2010-01-01",
                                                       areas=[LicensedArea("G", 180.0)]))
    workspace.licenses.set_verification(ADMIN, lic.id, "verified")

    calc = svc.run_calculation(case.id)
    assert calc.results["status"] == "ok", calc.results["errors"]
    assert calc.results["totals"]["remaining_to_assess_m2"] == pytest.approx(448 - 180)
    assert calc.rule_version_id == v.id and calc.drawing_revision_id
    ok, _ = workspace.calculations.reproduce(calc.id)
    assert ok
    links = workspace.db.query("SELECT * FROM calculation_licenses WHERE calculation_id=?", (calc.id,))
    assert links[0]["license_id"] == lic.id and links[0]["license_revision"] == 1

    # A new rule version must not change the stored calculation.
    v2 = workspace.rules.import_version(ADMIN, "Synthetic", sample_xlsx, "Setbacks", 2,
                                        ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    case.rule_version_id = v2.id
    workspace.cases.save(ADMIN, case)
    assert workspace.calculations.get(calc.id).rule_version_id == v.id
    assert workspace.calculations.reproduce(calc.id)[0]

    # Derived geometry is generated into separate layers and ignored as input.
    n = svc.apply_derived_geometry(drawing, calc.results, calc.id)
    assert n >= 2
    assert all(e.meta["source"] == "derived" for e in drawing.entities_on(["SETBACK_BOUNDARY", "SETBACK_VIOLATIONS"]))
    workspace.drawings.save_revision(ADMIN, loaded.record.id, drawing, "derived")
    calc2 = svc.run_calculation(case.id)
    assert calc2.results["totals"] == calc.results["totals"]

    # Moving the boundary invalidates its verification.
    drawing.entities[boundary_id].data["points"][0][0] += 0.5
    workspace.drawings.save_revision(ADMIN, loaded.record.id, drawing, "moved")
    calc3 = svc.run_calculation(case.id)
    assert any(i["code"] == "boundary_changed" for i in calc3.results["issues"])
    assert "setback" not in calc3.results


def test_backup_and_restore(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "d"))
    ws = Workspace(settings, user=ADMIN)
    assert len(list_backups(ws.backup_dir)) == 1  # automatic daily backup
    make_case(ws, "BEFORE")
    backup = ws.backup_now("test")
    make_case(ws, "AFTER")
    db_path = ws.db_path
    ws.close()
    preserved = restore_backup(backup, db_path)
    assert preserved.exists()
    ws2 = Workspace(settings, user=ADMIN, auto_backup=False)
    numbers = [c.case_number for c in ws2.cases.list()]
    assert numbers == ["BEFORE"]
    ws2.close()
    conn = sqlite3.connect(preserved)
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 2
    conn.close()


def test_users_and_passwords(workspace):
    u = workspace.users.create("rev", "correct horse", "reviewer")
    assert workspace.users.authenticate("rev", "correct horse") == u
    assert workspace.users.authenticate("rev", "wrong") is None
    with pytest.raises(ValidationError):
        workspace.users.create("x", "short", "reviewer")
    assert u.can("approve_report") and not replace(u, role="surveyor").can("approve_report")
