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


def test_conditional_rule_needs_reviewer_values(workspace, sample_dxf, tmp_path):
    """Conditional rule text stops the setback calculation until a reviewer enters values for the case."""
    from openpyxl import Workbook

    from sls.rules.excel_rules import suggest_mapping

    wb = Workbook()
    ws_ = wb.active
    ws_.append(["ZONE_ID", "NAME", "SETBACK_FRONT", "SETBACK_SIDE", "SETBACK_BACK", "BUILT_UP_AREA_PERCENT", "FLOOR_NO"])
    ws_.append([7, "تجاري تجريبي", "4", "4م بعد 14م", "4", "ضمن الارتدادات", "*طابقين+روف"])
    ws_.append([9, "حدائق تجريبية", None, None, None, None, None])
    path = tmp_path / "zones.xlsx"
    wb.save(path)
    from sls.rules.excel_rules import read_sheet

    mapping = suggest_mapping(read_sheet(path, "Sheet", 1).headers)
    v = workspace.rules.import_version(REVIEWER, "Zones", path, "Sheet", 1, mapping)
    workspace.rules.verify_version(REVIEWER, v.id)
    rule = workspace.rules.rule(v.id, "7")
    assert rule.status == "conditional" and rule.setbacks_raw["side"] == "4م بعد 14م" and rule.source["NAME"]

    svc = CaseService(workspace)
    case = make_case(workspace, units=DrawingUnits(LengthUnit.METER, True, "local", CrsKind.LOCAL, "admin"),
                     rule_version_id=v.id, land_use_code="7")
    loaded = svc.import_drawing(case.id, sample_dxf)
    boundary = assign_sample_roles(loaded.drawing)
    workspace.drawings.save_revision(ADMIN, loaded.record.id, loaded.drawing)
    parcel = workspace.drawings.parcel(case.id)
    parcel.boundary_entity_id = boundary
    parcel.boundary_points = [[p[0], p[1]] for p in loaded.drawing.entities[boundary].data["points"]]
    parcel.edge_types = SAMPLE_EDGE_TYPES
    workspace.drawings.save_parcel(ADMIN, parcel, verify=True)

    calc = svc.run_calculation(case.id)
    err = next(e for e in calc.results["errors"] if e["section"] == "setback")
    assert err["code"] == "conditional_rule" and "4م بعد 14م" in err["message"]
    checks = {c["key"]: c for c in calc.results["eligibility"]["checks"]}
    assert checks["coverage"]["status"] == "not_evaluated" and "ضمن الارتدادات" in checks["coverage"]["reason"]
    assert "*طابقين+روف" in checks["floors"]["reason"]

    with pytest.raises(PermissionDeniedError):
        workspace.rules.set_case_override(SURVEYOR, case.id, v.id, "7", {"side": 4.0}, "after 14 m depth")
    with pytest.raises(ValidationError):
        workspace.rules.set_case_override(REVIEWER, case.id, v.id, "7", {"side": 4.0}, "  ")
    with pytest.raises(ValidationError):  # numeric rule values can never be replaced
        workspace.rules.set_case_override(REVIEWER, case.id, v.id, "7", {"front": 1.0}, "x")
    workspace.rules.set_case_override(REVIEWER, case.id, v.id, "7", {"side": 4.0}, "Parcel depth < 14 m: 4 m applies")
    calc2 = svc.run_calculation(case.id)
    assert "setback" in calc2.results, calc2.results["errors"]
    assert calc2.results["setback"]["edges"][1]["setback_m"] == 4.0
    assert any(i["code"] == "setback_override" for i in calc2.results["issues"])
    step = next(s for s in calc2.results["trace"] if s["key"] == "setback")
    assert step["inputs"]["reviewer_values"] == {"side": 4.0} and step["inputs"]["rule_text"]["side"] == "4م بعد 14م"
    assert workspace.calculations.reproduce(calc2.id)[0]

    # The override is tied to the rule version and code: a new version does not inherit it.
    v2 = workspace.rules.import_version(REVIEWER, "Zones", path, "Sheet", 1, mapping)
    case = workspace.cases.get(case.id)
    case.rule_version_id = v2.id
    workspace.cases.save(ADMIN, case)
    calc3 = svc.run_calculation(case.id)
    assert any(e["code"] == "conditional_rule" for e in calc3.results["errors"])

    case.land_use_code = "9"
    workspace.cases.save(ADMIN, case)
    calc4 = svc.run_calculation(case.id)
    assert any("defines no setback values" in e["message"] for e in calc4.results["errors"])


def test_existing_v1_database_is_migrated_with_backup(tmp_path, monkeypatch):
    from sls.persistence import db as db_module
    from sls.persistence.schema import MIGRATIONS

    path = tmp_path / "d" / "sls.sqlite3"
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS[:1])
    old = db_module.Database(path, backup_dir=tmp_path / "d" / "backups")
    old.migrate()
    old.execute("INSERT INTO rule_sets(id,name,created_at) VALUES ('rs','old','2026-01-01')")
    old.close()
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    ws = Workspace(Settings(data_dir=str(tmp_path / "d")), user=ADMIN, auto_backup=False)
    assert ws.db.schema_version() == MIGRATIONS[-1][0]
    assert any("pre-migration-v1" in p.name for p in list_backups(ws.backup_dir))
    assert ws.db.query_one("SELECT name FROM rule_sets")["name"] == "old"
    ws.close()


def test_snapshot_from_before_later_input_fields_still_reproduces(full_case):
    """Regression: snapshots stored before rule_override/zoning/setback fields existed failed reproduce()."""
    import hashlib
    import json

    ws, calc = full_case["ws"], full_case["calc"]
    old = json.loads(ws.db.query_one("SELECT inputs_json FROM calculations WHERE id=?", (calc.id,))["inputs_json"])
    for key in ("rule_override", "zoning", "setback_method", "setback_approval", "datasets"):
        old.pop(key, None)
    text = json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    from sls.engine.inputs import CalculationInputs
    from sls.engine.run import result_hash, run_calculation

    results = run_calculation(CalculationInputs.from_dict(old))
    with ws.db.transaction():
        ws.db.execute("UPDATE calculations SET inputs_json=?, input_hash=?, result_hash=? WHERE id=?",
                      (text, hashlib.sha256(text.encode()).hexdigest(), result_hash(results), calc.id))
    ok, detail = ws.calculations.reproduce(calc.id)
    assert ok, detail
    with ws.db.transaction():  # tampering is still detected
        ws.db.execute("UPDATE calculations SET inputs_json=? WHERE id=?", (text.replace("SYN-B", "SYN-A"), calc.id))
    assert ws.calculations.reproduce(calc.id) == (False, "stored inputs do not match their recorded hash")
