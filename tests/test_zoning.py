"""Zoning register: land-use zone polygons overlaid on parcels (synthetic layer)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import shapefile
from conftest import ADMIN, SURVEYOR
from test_cadastre import (  # noqa: F401  (fixture re-export)
    JTM_PRJ,
    OX,
    OY,
    parcel_shp,
    sq,
)

from sls.core.errors import PermissionDeniedError, ValidationError
from sls.core.units import CrsKind, DrawingUnits, LengthUnit
from sls.engine import run as engine_run
from sls.gis.shapefile_layer import inspect, suggest_mapping, suggest_zoning_mapping
from sls.persistence.cases import Case
from sls.persistence.zoning_repo import _code
from sls.rules.excel_rules import ColumnMapping
from sls.samples import SAMPLE_RULE_MAPPING
from sls.services.case_service import CaseService


@pytest.fixture
def zoning_shp(tmp_path):
    path = tmp_path / "SYN_ZONES.shp"
    w = shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8")
    w.field("ZONE_ID", "C", 10)
    w.field("الاست", "C", 40)
    w.field("DLS_KEY", "C", 15)
    pieces = [
        ("SYN-B", "سكن تجريبي ب", sq(OX - 10, OY - 10, 45, 100)),     # covers 351, 5 m of 352/353
        ("SYN-COM", "تجاري تجريبي", sq(OX + 35, OY - 10, 30, 100)),    # rest of 352/353
        ("SYN-A", "سكن تجريبي أ", sq(OX + 29.98, OY, 0.02, 25)),      # 0.5 m² digitising sliver on 351
        ("0", "", sq(OX + 195, OY - 5, 40, 30)),                        # polygon without a zone code
        ("71", "صفة غير موجودة في الجدول", sq(OX + 795, OY - 5, 40, 30)),
    ]
    for zid, name, ring in pieces:
        w.poly([ring])
        w.record(zid, name, "")
    w.close()
    path.with_suffix(".prj").write_text(JTM_PRJ)
    path.with_suffix(".cpg").write_text("UTF-8")
    return path


def _setup(workspace, parcel_shp, zoning_shp):  # noqa: F811
    workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields))
    return workspace.zoning.import_layer(ADMIN, zoning_shp, suggest_zoning_mapping(inspect(zoning_shp).fields), "Zones")


def test_code_normalisation():
    assert (_code(3.0), _code(71), _code(" 2 "), _code(None), _code(2.5)) == ("3", "71", "2", "", "2.5")


def test_mapping_and_import_permissions(workspace, zoning_shp):
    m = suggest_zoning_mapping(inspect(zoning_shp).fields)
    assert m.columns == {"zone_id": "ZONE_ID", "zone_name": "الاست", "key": "DLS_KEY"}
    with pytest.raises(PermissionDeniedError):
        workspace.zoning.import_layer(SURVEYOR, zoning_shp, m)
    res = workspace.zoning.import_layer(ADMIN, zoning_shp, m)
    assert (res["records"], res["zones"], res["without_zone"], res["version"]) == (5, 4, 1, 1)
    assert workspace.zoning.import_layer(ADMIN, zoning_shp, m)["version"] == 2
    assert workspace.zoning.active_layer()["version"] == 2


def test_overlay_single_split_sliver_unzoned_and_gap(workspace, parcel_shp, zoning_shp):  # noqa: F811
    _setup(workspace, parcel_shp, zoning_shp)
    geom = lambda key: workspace.cadastre.find(key=key)[0].geometry
    ov = workspace.zoning.overlay(geom("900103000000351"))
    assert ov.single_zone.zone_id == "SYN-B" and ov.single_zone.pct == pytest.approx(100)
    assert [s.zone_id for s in ov.slivers] == ["SYN-A"] and ov.uncovered_m2 == pytest.approx(0, abs=1e-6)
    split = workspace.zoning.overlay(geom("900103000000352"))
    assert split.single_zone is None
    assert [(z.zone_id, round(z.pct)) for z in split.zones] == [("SYN-COM", 75), ("SYN-B", 25)]
    unz = workspace.zoning.overlay(workspace.cadastre.find(key="900102500000200")[0].geometry)
    assert unz.zones == [] and len(unz.unzoned) == 1 and unz.single_zone is None
    outside = workspace.zoning.overlay(sq_poly(OX + 400, OY, 10, 10))
    assert outside.zones == [] and outside.uncovered_m2 == pytest.approx(100)


def sq_poly(x, y, w, h):
    from shapely.geometry import Polygon
    return Polygon(sq(x, y, w, h))


def test_no_zoning_layer_gives_none(workspace):
    assert workspace.zoning.overlay(sq_poly(OX, OY, 10, 10)) is None


def _case_with_rules(workspace, sample_xlsx, n, code=""):
    v = workspace.rules.import_version(ADMIN, "Synthetic", sample_xlsx, "Setbacks", 2,
                                       ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    return workspace.cases.save(ADMIN, Case(case_number=f"Z-{n}", rule_version_id=v.id, land_use_code=code,
                                            units=DrawingUnits(LengthUnit.METER, True, "ESRI:102158",
                                                               CrsKind.PROJECTED, "admin")))


def test_link_sets_land_use_only_for_single_known_zone(workspace, parcel_shp, zoning_shp, sample_xlsx):  # noqa: F811
    _setup(workspace, parcel_shp, zoning_shp)
    svc = CaseService(workspace)
    p351 = workspace.cadastre.find(key="900103000000351")[0]
    p352 = workspace.cadastre.find(key="900103000000352")[0]
    multi = workspace.cadastre.find(key="900101200500196")[0]   # zone 71: not in the rule table
    cases = ((p351, "", "SYN-B"), (p352, "", ""), (p351, "SYN-C", "SYN-C"), (multi, "", ""))
    for n, (parcel, preset, expected) in enumerate(cases):
        case = _case_with_rules(workspace, sample_xlsx, n, preset)
        svc.link_case_to_parcel(case.id, parcel.id, None, None)
        assert workspace.cases.get(case.id).land_use_code == expected, (parcel.dls_key, preset)


def _check(zoning, code):
    issues, out = [], {}
    engine_run._zoning_checks(SimpleNamespace(zoning=zoning, land_use_code=code), issues, engine_run.Trace(), out)
    return {i["code"] for i in issues}, out


def test_engine_zoning_warnings():
    one = {"layer": "Z v1", "uncovered_m2": 0.0, "zones": [{"zone_id": "2", "zone_name": "سكن ب", "pct": 100.0,
                                                            "area_m2": 500.0}]}
    assert _check(one, "2")[0] == set() and _check(one, "2")[1]["zoning"] is one
    assert _check(one, "3")[0] == {"zoning_mismatch"}
    two = {**one, "zones": one["zones"] + [{"zone_id": "4", "zone_name": "تجاري", "pct": 20.0, "area_m2": 100.0}]}
    assert _check(two, "2")[0] == {"zoning_multiple"}
    assert _check({"layer": "Z", "uncovered_m2": 30.0, "zones": []}, "2")[0] == {"zoning_none", "zoning_gap"}
    assert _check(None, "2") == (set(), {})


def test_zoning_in_calculation_and_report(full_case, tmp_path):
    from pypdf import PdfReader

    from sls.reporting.report import ReportOptions
    from sls.services.report_service import ReportService

    from sls.core.units import CrsKind, DrawingUnits, LengthUnit

    ws, case = full_case["ws"], full_case["case"]
    case.units = DrawingUnits(LengthUnit.METER, True, "ESRI:102158", CrsKind.PROJECTED, "admin")
    ws.cases.save(ADMIN, case)
    path = tmp_path / "LOCAL_ZONES.shp"   # no .prj: the coordinate system must be declared explicitly
    w = shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8")
    w.field("ZONE_ID", "C", 10)
    w.field("الاست", "C", 40)
    w.poly([sq(OX - 1000, OY - 1000, 3000, 3000)])
    w.record("SYN-A", "سكن تجريبي أ")
    w.close()
    with pytest.raises(ValidationError, match="no .prj"):
        ws.zoning.import_layer(ADMIN, path, suggest_zoning_mapping(inspect(path).fields))
    ws.zoning.import_layer(ADMIN, path, suggest_zoning_mapping(inspect(path).fields), declared_crs="ESRI:102158")
    calc = CaseService(ws).run_calculation(case.id)
    assert calc.results["zoning"]["zones"][0]["zone_id"] == "SYN-A"
    assert "zoning_mismatch" in {i["code"] for i in calc.results["issues"]}  # case uses SYN-B
    res = ReportService(ws).preview(case.id, ReportOptions(language="en", generated_at="2026-10-18T09:00:00+00:00",
                                                           generated_by="tester"))
    import io
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(res.pdf)).pages)
    assert "Zoning register" in text and "SYN-A" in text and "100.0%" in text


def test_zoning_skipped_for_other_grid(full_case, zoning_shp):
    ws, case = full_case["ws"], full_case["case"]   # case grid is a local system; layer is JTM
    ws.zoning.import_layer(ADMIN, zoning_shp, suggest_zoning_mapping(inspect(zoning_shp).fields))
    calc = CaseService(ws).run_calculation(case.id)
    assert "zoning" not in calc.results


def test_zoning_import_dialog(qtbot, qapp, workspace, zoning_shp, sample_xlsx, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from sls.ui.dialogs.cadastre_dialogs import CadastreImportDialog

    v = workspace.rules.import_version(ADMIN, "Synthetic", sample_xlsx, "Setbacks", 2,
                                       ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert v
    dlg = CadastreImportDialog(workspace, lambda _msg, fn: fn(), kind="zoning")
    qtbot.addWidget(dlg)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(zoning_shp), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dlg._browse()
    assert dlg._mapping().columns == {"zone_id": "ZONE_ID", "zone_name": "الاست", "key": "DLS_KEY"}
    dlg._check()
    text = dlg.summary.text()
    assert "71" in text and dlg.import_btn.isEnabled()   # zone 71 is not in the rule table
    staged = []
    dlg.review = lambda v: staged.append(v)  # the comparison dialog is modal; activation is explicit
    dlg._import()
    assert staged[0].status == "staged" and workspace.zoning.active_layer() is None  # nothing activated yet
    workspace.datasets.activate("zoning", workspace.user, staged[0].id, "test")
    assert workspace.zoning.active_layer()["record_count"] == 5
