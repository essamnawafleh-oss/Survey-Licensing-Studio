"""Jordan TM (EPSG:3066) support, coordinate checks and damaged-data handling (synthetic data)."""
from __future__ import annotations

import ezdxf
import pytest

from sls.cad.dxf_io import import_dxf
from sls.cad.model import Drawing, Entity
from sls.cad.ops import find_parcel
from sls.core.crs import JTM, JTM_EPSG, check_extents, describe, same_grid, scale_factor, to_lonlat
from sls.core.units import CrsKind, DrawingUnits, LengthUnit
from sls.services.case_service import CaseService

WADI_MUSA = (353857.0, 356254.0)  # JTM grid coordinates of Wadi Musa town centre (approx.)


def test_jtm_definition_comes_from_epsg():
    import pyproj

    assert JTM == "ESRI:102158"  # the authority's GIS definition (.prj "Jordan_JTM") is the default
    info = describe(JTM)
    assert info.name == "Jordan_JTM" and info.kind == "projected" and info.unit == "metre"
    assert describe(JTM_EPSG).name == "ED50 / Jordan TM" and same_grid(JTM, JTM_EPSG)
    params = {p.name: p.value for p in pyproj.CRS.from_epsg(3066).coordinate_operation.params}
    assert params["Longitude of natural origin"] == 37
    assert params["Scale factor at natural origin"] == pytest.approx(0.9998)
    assert params["False easting"] == 500000 and params["False northing"] == -3000000
    lon, lat = to_lonlat(JTM, *WADI_MUSA)
    assert lat == pytest.approx(30.32, abs=0.01) and lon == pytest.approx(35.48, abs=0.01)


def test_extent_check_detects_wrong_units_and_local_coordinates():
    ext = (352000.0, 355000.0, 355000.0, 357500.0)
    assert check_extents(JTM, ext).ok
    assert not check_extents(JTM, ext, unit_to_m=0.0254).ok  # same numbers read as inches
    assert not check_extents(JTM, (0.0, 0.0, 120.0, 80.0)).ok  # local site coordinates


def test_scale_factor_near_petra():
    k = scale_factor(JTM, *WADI_MUSA)
    assert k == pytest.approx(1.0000635, abs=2e-6)


def _jtm_dxf(tmp_path, insunits: int, origin=(353000.0, 355500.0)):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()
    ox, oy = origin
    msp.add_lwpolyline([(ox, oy), (ox + 30, oy), (ox + 30, oy + 25), (ox, oy + 25)], close=True,
                       dxfattribs={"layer": "PARCELS"})
    t = msp.add_text("00351", height=1.0, dxfattribs={"layer": "PARCEL_NO"})
    t.set_placement((ox + 15, oy + 12), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
    bad = msp.add_text("00352", height=1.0, dxfattribs={"layer": "PARCEL_NO"})
    bad.set_placement((ox + 5, oy + 5), align=ezdxf.enums.TextEntityAlignment.CENTER)
    bad.dxf.insert = (ox + 4, oy + 5)
    bad.dxf.align_point = (10.27, 20.0)  # corrupt value as produced by an old DWG converter
    msp.add_lwpolyline([(ox + 1, oy + 1)], dxfattribs={"layer": "PARCELS"})  # damaged: 1 vertex
    path = tmp_path / "jtm.dxf"
    doc.saveas(path)
    return path


def test_import_flags_corrupt_alignment_and_damaged_polylines(tmp_path):
    drawing, report = import_dxf(_jtm_dxf(tmp_path, 6))
    assert report.inconsistent_text_alignment == 1
    assert sum(v for k, v in report.skipped.items() if "damaged" in k) == 1
    assert report.damaged_handles
    texts = {e.data["text"]: e.data["position"] for e in drawing.entities.values() if e.kind == "text"}
    assert texts["00352"] == pytest.approx([353004.0, 355505.0])  # insertion point used, not (10.27, 20)
    assert texts["00351"] == pytest.approx([353015.0, 355512.0])  # valid alignment point kept
    assert drawing.extents()[0] > 350000


def test_header_inches_overridden_by_jtm_check_but_not_verified(tmp_path):
    drawing, report = import_dxf(_jtm_dxf(tmp_path, 1))
    assert drawing.units.unit is LengthUnit.INCH
    CaseService.check_drawing_crs(drawing, report, JTM)
    assert report.crs_ok is True and "inside the area of use" in report.crs_message
    assert drawing.units.unit is LengthUnit.METER and drawing.units.verified is False
    assert any("declares 'in'" in w for w in report.warnings)


def test_local_coordinates_fail_jtm_check(tmp_path):
    drawing, report = import_dxf(_jtm_dxf(tmp_path, 6, origin=(100.0, 100.0)))
    CaseService.check_drawing_crs(drawing, report, JTM)
    assert report.crs_ok is False and "NOT" in report.crs_message


def test_engine_records_crs_and_flags_outside_coordinates(sample_dxf):
    from sls.engine.inputs import extract_inputs
    from sls.engine.run import run_calculation
    from sls.rules.excel_rules import LandUseRule
    from sls.samples import SAMPLE_EDGE_TYPES, assign_sample_roles

    drawing, _ = import_dxf(sample_dxf)
    boundary = assign_sample_roles(drawing)
    rule = LandUseRule("3", {"front": 3, "side": 2.5, "rear": 3}, 4)

    def run(units):
        return run_calculation(extract_inputs(
            drawing, boundary_entity_id=boundary, parcel_verified=True, edge_types=SAMPLE_EDGE_TYPES, rule=rule,
            rule_version_id="v", rule_version_verified=True, land_use_code="3", licenses=[], units=units))

    ok = run(DrawingUnits(LengthUnit.METER, True, JTM, CrsKind.PROJECTED))
    assert ok["status"] == "ok" and ok["crs"]["inside_area_of_use"] is True
    assert ok["crs"]["scale_factor"] == pytest.approx(1.00006, abs=1e-4)
    assert any(s["key"] == "crs" for s in ok["trace"])
    # Same coordinates declared as millimetres: they no longer fall inside Jordan.
    wrong = run(DrawingUnits(LengthUnit.MILLIMETER, True, JTM, CrsKind.PROJECTED))
    assert any(i["code"] == "crs_extent" for i in wrong["issues"]) and wrong["status"] == "partial"


def test_find_parcel_and_label_and_window_subset():
    d = Drawing.with_default_layers()
    big = d.add(Entity("polyline", "REFERENCE", {"points": [[0, 0], [100, 0], [100, 100], [0, 100]], "closed": True}))
    small = d.add(Entity("polyline", "REFERENCE", {"points": [[10, 10], [40, 10], [40, 40], [10, 40]], "closed": True}))
    label = d.add(Entity("text", "LABELS", {"position": [20, 20], "text": "00351", "height": 1}))
    other = d.add(Entity("text", "LABELS", {"position": [70, 70], "text": "00999", "height": 1}))
    poly, labels = find_parcel([big, small], [label, other], (25, 25))
    assert poly is small and labels == ["00351"]
    assert find_parcel([big, small], [], (200, 200))[0] is None
    subset = CaseService.entities_in_window(d, (5, 5, 45, 45))
    assert small in subset and label in subset and other not in subset and big in subset
