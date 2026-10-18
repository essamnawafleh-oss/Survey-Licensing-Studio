"""License reconciliation and end-to-end calculation tests (fabricated records)."""
from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from sls.cad.dxf_io import import_dxf
from sls.core.units import CrsKind, DrawingUnits, LengthUnit
from sls.engine.inputs import CalculationInputs, extract_inputs
from sls.engine.run import result_hash, run_calculation
from sls.licensing.reconcile import LicenseRecordInput as L
from sls.licensing.reconcile import reconcile
from sls.rules.excel_rules import LandUseRule
from sls.samples import SAMPLE_EDGE_TYPES, SAMPLE_PARCEL, assign_sample_roles

UNITS = DrawingUnits(LengthUnit.METER, True, "synthetic-local", CrsKind.LOCAL, "tester")
RULE_B = LandUseRule("SYN-B", {"front": 4, "side": 3, "rear": 3}, 4, "سكن تجريبي ب", "Synthetic B",
                     max_coverage_pct=55, max_floors=4, max_far=2.2)


def test_previous_license_subtraction_example():
    r = reconcile({"G": 300.0}, {}, [L("1", "LIC-1", 1, "verified", (("G", 180.0),))])
    assert r.total_measured_area == 300
    assert r.total_licensed_area == 180
    assert r.total_remaining_area == pytest.approx(120)


def test_multiple_floors_and_multiple_licenses():
    recs = [L("1", "LIC-1", 1, "verified", (("G", 150.0),)),
            L("2", "LIC-2", 1, "verified", (("G", 50.0), ("1", 100.0)))]
    r = reconcile({"G": 260.0, "1": 160.0}, {}, recs)
    lines = {x.key: x for x in r.areas}
    assert lines["G"].licensed == 200 and lines["G"].remaining == pytest.approx(60)
    assert lines["1"].remaining == pytest.approx(60)
    assert r.total_remaining_area == pytest.approx(120)
    assert r.assumptions  # overlap assumption stated


def test_missing_records_are_warned():
    r = reconcile({"G": 100.0}, {}, [])
    assert r.total_remaining_area == 100
    assert any("No previous license records" in w for w in r.warnings)


def test_unverified_and_disputed_records_excluded():
    recs = [L("1", "LIC-1", 1, "unverified", (("G", 80.0),)), L("2", "LIC-2", 1, "disputed", (("G", 10.0),))]
    r = reconcile({"G": 100.0}, {}, recs)
    assert r.total_licensed_area == 0
    assert r.excluded_records == ["1", "2"]
    assert len([w for w in r.warnings if "excluded" in w]) == 2


def test_conflicting_duplicate_license_blocks_floor():
    recs = [L("1", "LIC-9", 1, "verified", (("G", 80.0),)), L("2", "LIC-9", 1, "verified", (("G", 90.0),))]
    r = reconcile({"G": 100.0}, {}, recs)
    assert r.areas[0].remaining is None and r.areas[0].conflicts
    assert r.total_remaining_area is None and "G" in r.total_remaining_reason


def test_identical_duplicate_counted_once():
    recs = [L("1", "LIC-9", 1, "verified", (("G", 80.0),)), L("2", "LIC-9", 1, "verified", (("G", 80.0),))]
    r = reconcile({"G": 100.0}, {}, recs)
    assert r.total_remaining_area == pytest.approx(20)


def test_licensed_exceeding_measured_is_conflict_not_clamped():
    r = reconcile({"G": 100.0}, {}, [L("1", "LIC-1", 1, "verified", (("G", 120.0),))])
    assert r.areas[0].remaining is None
    assert "exceeds measured" in r.areas[0].conflicts[0]


def test_licensed_floor_absent_from_drawing_is_conflict():
    r = reconcile({"G": 100.0}, {}, [L("1", "LIC-1", 1, "verified", (("3", 50.0),))])
    assert any("does not exist" in c for line in r.areas for c in line.conflicts)
    assert r.total_remaining_area is None


def test_length_reconciliation():
    r = reconcile({}, {"fence": 80.0, "wall": 30.0}, [L("1", "LIC-1", 1, "verified", (), (("fence", 50.0),))])
    lines = {x.key: x for x in r.lengths}
    assert lines["fence"].remaining == pytest.approx(30)
    assert lines["wall"].remaining == pytest.approx(30)


def _inputs(sample_dxf, rule=RULE_B, verified=True, licenses=(), edge_types=SAMPLE_EDGE_TYPES, units=UNITS):
    drawing, _ = import_dxf(sample_dxf)
    boundary = assign_sample_roles(drawing)
    return extract_inputs(drawing, boundary_entity_id=boundary, parcel_verified=verified,
                          edge_types=list(edge_types), rule=rule, rule_version_id="rv-1",
                          rule_version_verified=True, land_use_code=rule.code if rule else "SYN-X",
                          licenses=list(licenses), units=units, floor_order=["G", "1"])


def test_end_to_end_sample_case(sample_dxf):
    lic = [L("l1", "SYN-LIC-001", 1, "verified", (("G", 200.0),), (("fence", 40.0),))]
    res = run_calculation(_inputs(sample_dxf, licenses=lic))
    assert res["status"] == "ok", res["errors"]
    parcel_area = Polygon(SAMPLE_PARCEL).area
    assert res["parcel"]["area_m2"] == pytest.approx(parcel_area, abs=1e-6)
    floors = {f["floor_key"]: f for f in res["floors"]}
    assert floors["G"]["area_m2"] == pytest.approx(288)
    assert floors["1"]["area_m2"] == pytest.approx(160)
    assert res["totals"]["measured_total_area_m2"] == pytest.approx(448)
    assert res["totals"]["previously_licensed_area_m2"] == pytest.approx(200)
    assert res["totals"]["remaining_to_assess_m2"] == pytest.approx(248)
    # Ground floor intrudes into left side setback (x=2 vs required 3 m) and front (y=5 vs 4 ok).
    assert floors["G"]["in_setback_zone_m2"] > 0
    assert res["totals"]["setback_violation_area_m2"] > 0
    checks = {c["key"]: c for c in res["eligibility"]["checks"]}
    assert checks["setbacks"]["status"] == "fail"
    assert checks["floors"]["status"] == "pass"
    lengths = {x["kind"]: x["length_m"] for x in res["lengths"]}
    assert lengths["wall"] == pytest.approx(13 + 10)
    rec = {x["key"]: x for x in res["reconciliation"]["lengths"]}
    assert rec["fence"]["remaining"] == pytest.approx(lengths["fence"] - 40)
    assert res["trace"] and all("formula" in s for s in res["trace"])


def test_calculation_is_reproducible_from_stored_inputs(sample_dxf):
    inputs = _inputs(sample_dxf)
    first = run_calculation(inputs)
    restored = CalculationInputs.from_dict(inputs.to_dict())
    second = run_calculation(restored)
    assert restored.input_hash() == inputs.input_hash()
    assert result_hash(first) == result_hash(second)


def test_missing_rule_stops_setbacks_but_keeps_areas(sample_dxf):
    res = run_calculation(_inputs(sample_dxf, rule=None))
    assert res["status"] == "partial"
    assert any(e["code"] == "missing_rule" for e in res["errors"])
    assert "setback" not in res
    assert res["totals"]["measured_total_area_m2"] == pytest.approx(448)


def test_rule_without_needed_boundary_type(sample_dxf):
    res = run_calculation(_inputs(sample_dxf, edge_types=["front", "corner", "rear", "side"]))
    assert any(e["code"] == "missing_rule" for e in res["errors"])


def test_unverified_boundary_blocks_setbacks(sample_dxf):
    res = run_calculation(_inputs(sample_dxf, verified=False))
    assert any(e["code"] == "boundary_not_verified" for e in res["errors"])
    assert "setback" not in res


def test_unverified_units_stop_everything(sample_dxf):
    res = run_calculation(_inputs(sample_dxf, units=DrawingUnits(LengthUnit.METER, False)))
    assert res["status"] == "error" and res["errors"][0]["code"] == "units_not_verified"


def test_rule_version_change_changes_input_hash_only_for_new_calcs(sample_dxf):
    old = _inputs(sample_dxf)
    newer_rule = LandUseRule("SYN-B", {"front": 5, "side": 3, "rear": 3}, 4, max_coverage_pct=55)
    new = _inputs(sample_dxf, rule=newer_rule)
    assert old.input_hash() != new.input_hash()
    r_old, r_new = run_calculation(old), run_calculation(new)
    assert r_old["setback"]["buildable_area_m2"] != r_new["setback"]["buildable_area_m2"]
    # The stored old inputs still reproduce the old result.
    assert result_hash(run_calculation(CalculationInputs.from_dict(old.to_dict()))) == result_hash(r_old)


def test_areas_exceeding_eligibility_limits(sample_dxf):
    strict = LandUseRule("SYN-S", {"front": 0.5, "side": 0.5, "rear": 0.5}, 9, max_coverage_pct=20, max_floors=1,
                         max_far=0.5)
    res = run_calculation(_inputs(sample_dxf, rule=strict))
    checks = {c["key"]: c for c in res["eligibility"]["checks"]}
    assert checks["coverage"]["status"] == "fail" and checks["coverage"]["excess"] > 0
    assert checks["floors"]["status"] == "fail" and checks["floors"]["excess"] == 1
    assert checks["far"]["status"] == "fail"
    assert checks["setbacks"]["status"] == "pass"


def test_missing_eligibility_values_are_not_evaluated(sample_dxf):
    bare = LandUseRule("SYN-N", {"front": 1, "side": 1, "rear": 1}, 9)
    res = run_calculation(_inputs(sample_dxf, rule=bare))
    statuses = {c["key"]: c["status"] for c in res["eligibility"]["checks"]}
    assert statuses["coverage"] == statuses["floors"] == statuses["far"] == "not_evaluated"


def test_floor_missing_on_building_entity_is_reported(sample_dxf):
    drawing, _ = import_dxf(sample_dxf)
    boundary = assign_sample_roles(drawing)
    drawing.layer("BLDG_1F").floor_key = ""
    inputs = extract_inputs(drawing, boundary_entity_id=boundary, parcel_verified=True,
                            edge_types=SAMPLE_EDGE_TYPES, rule=RULE_B, rule_version_id="rv", rule_version_verified=True,
                            land_use_code="SYN-B", licenses=[], units=UNITS)
    res = run_calculation(inputs)
    assert any(i["code"] == "floor_missing" for i in res["issues"])
    assert res["status"] == "partial"
