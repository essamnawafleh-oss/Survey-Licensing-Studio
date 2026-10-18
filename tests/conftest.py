"""Shared fixtures. All data is synthetic (see sls.samples)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sls.core.settings import Settings  # noqa: E402
from sls.persistence.users import User  # noqa: E402
from sls.persistence.workspace import Workspace  # noqa: E402
from sls.samples import write_sample_dxf, write_sample_rules_xlsx  # noqa: E402

ADMIN = User("u-admin", "admin", "Admin", "admin")
SURVEYOR = User("u-surv", "surveyor1", "Surveyor", "surveyor")
REVIEWER = User("u-rev", "reviewer1", "Reviewer", "reviewer")


@pytest.fixture
def sample_dxf(tmp_path: Path) -> Path:
    return write_sample_dxf(tmp_path / "synthetic_survey.dxf")


@pytest.fixture
def sample_xlsx(tmp_path: Path) -> Path:
    return write_sample_rules_xlsx(tmp_path / "synthetic_rules.xlsx")


@pytest.fixture
def workspace(tmp_path: Path):
    ws = Workspace(Settings(data_dir=str(tmp_path / "data")), user=ADMIN)
    yield ws
    ws.close()


@pytest.fixture
def full_case(workspace, sample_dxf, sample_xlsx, tmp_path):
    """A complete synthetic case: drawing, verified units/boundary/rules, one verified license."""
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit
    from sls.persistence.cases import Case
    from sls.persistence.licenses_repo import LicensedArea, LicensedLength, LicenseRecord
    from sls.rules.excel_rules import ColumnMapping
    from sls.samples import SAMPLE_EDGE_TYPES, SAMPLE_RULE_MAPPING, assign_sample_roles
    from sls.services.case_service import CaseService

    ws = workspace
    svc = CaseService(ws)
    v = ws.rules.import_version(ADMIN, "Synthetic", sample_xlsx, "Setbacks", 2,
                                ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    ws.rules.verify_version(ADMIN, v.id)
    case = ws.cases.save(ADMIN, Case(case_number="SYN-2026-042", parcel_identifier="SYN-PLOT-7",
                                     surveyor="مساح تجريبي", survey_date="2026-10-01",
                                     fields={"owner_name": "مالك تجريبي", "basin": "حوض ٣ تجريبي"}))
    loaded = svc.import_drawing(case.id, sample_dxf)
    case = ws.cases.get(case.id)
    case.units = DrawingUnits(LengthUnit.METER, True, "synthetic-local", CrsKind.LOCAL, ADMIN.username)
    case.rule_version_id, case.land_use_code = v.id, "SYN-B"
    ws.cases.save(ADMIN, case)
    boundary = assign_sample_roles(loaded.drawing)
    ws.drawings.save_revision(ADMIN, loaded.record.id, loaded.drawing, "classified")
    parcel = ws.drawings.parcel(case.id)
    parcel.boundary_entity_id = boundary
    parcel.boundary_points = [[p[0], p[1]] for p in loaded.drawing.entities[boundary].data["points"]]
    parcel.edge_types = SAMPLE_EDGE_TYPES
    ws.drawings.save_parcel(ADMIN, parcel, verify=True)
    lic = ws.licenses.save(ADMIN, LicenseRecord(case.id, "SYN-LIC-001", issue_date="2010-05-20",
                                                structure_description="الطابق الأرضي",
                                                areas=[LicensedArea("G", 180.0)],
                                                lengths=[LicensedLength("fence", 40.0)]))
    ws.licenses.set_verification(ADMIN, lic.id, "verified", "مطابق للوثيقة")
    calc = svc.run_calculation(case.id)
    assert calc.results["status"] == "ok", calc.results["errors"]
    return {"ws": ws, "case": ws.cases.get(case.id), "license": ws.licenses.get(lic.id), "calc": calc,
            "drawing_id": loaded.record.id, "tmp": tmp_path}
