"""Surveyor setback method: explode → individual inward offsets → trim/extend → sharp corners.

Synthetic geometry only. The distance-zone method (TD-06) stays available and is compared here.
"""
from __future__ import annotations

import hashlib
import json
import math
import random

import pytest
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from sls.core.errors import MissingRuleError, ValidationError
from sls.engine.inputs import CalculationInputs
from sls.engine.run import result_hash, run_calculation
from sls.geometry.setbacks import (
    METHOD_DISTANCE_ZONE,
    METHOD_OFFSET_CORNER,
    compute_setbacks,
    compute_setbacks_offset,
    setback_function,
)

RECT = [(0, 0), (20, 0), (20, 30), (0, 30)]
L_SHAPE = [(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)]
DUMBBELL = [(0, 0), (20, 0), (20, 8.5), (30, 8.5), (30, 0), (50, 0), (50, 20), (30, 20), (30, 11.5),
            (20, 11.5), (20, 20), (0, 20)]


def coords(geom):
    return sorted((round(x, 6), round(y, 6)) for x, y in list(geom.exterior.coords)[:-1])


def strips(parcel, types, distances):
    o = 1 if parcel.exterior.is_ccw else -1
    cs = list(parcel.exterior.coords)
    return unary_union([LineString([cs[i], cs[i + 1]]).buffer(o * distances[t], single_sided=True)
                        for i, t in enumerate(types) if distances[t] > 0])


def test_rectangle_matches_distance_zone_method_exactly():
    parcel, types, d = Polygon(RECT), ["front", "side", "rear", "side"], {"front": 4, "side": 3, "rear": 2}
    res = compute_setbacks_offset(parcel, types, d)
    assert res.method == METHOD_OFFSET_CORNER and res.status == "ok" and not res.warnings
    assert coords(res.buildable) == [(3, 4), (3, 28), (17, 4), (17, 28)]
    assert res.buildable_area == pytest.approx(compute_setbacks(parcel, types, d).buildable_area)
    assert res.zone_area == pytest.approx(600 - 14 * 24)


def test_reflex_corner_is_sharp_intersection_not_arc():
    parcel = Polygon(L_SHAPE)
    res = compute_setbacks_offset(parcel, ["side"] * 6, {"side": 2})
    assert coords(res.buildable) == [(2, 2), (2, 38), (8, 8), (8, 38), (38, 2), (38, 8)]
    assert res.buildable_area == pytest.approx(396)
    # The distance-zone method keeps the quarter disc at the reflex corner (arc): d²(1 − π/4) more.
    legacy = compute_setbacks(parcel, ["side"] * 6, {"side": 2})
    assert legacy.buildable_area - res.buildable_area == pytest.approx(4 * (1 - math.pi / 4), abs=1e-3)
    assert len(legacy.buildable.exterior.coords) > 50 and len(res.buildable.exterior.coords) == 7


def test_individual_distances_per_edge_and_clockwise_input():
    types = ["front", "side", "rear", "side", "rear", "side"]
    d = {"front": 5, "side": 2, "rear": 3}
    ccw = compute_setbacks_offset(Polygon(L_SHAPE), types, d)
    # edge 1 (y=0) at 5, edge 2 (x=40) at 2, edge 3 (y=10) at 3, edge 4 (x=10) at 2, edge 5 (y=40) at 3, edge 6 (x=0) at 2
    assert coords(ccw.buildable) == [(2, 5), (2, 37), (8, 7), (8, 37), (38, 5), (38, 7)]
    rev = Polygon([L_SHAPE[0]] + L_SHAPE[:0:-1])  # same physical edges, clockwise vertex order
    rev_types = [types[5], types[4], types[3], types[2], types[1], types[0]]
    cw = compute_setbacks_offset(rev, rev_types, d)
    assert coords(cw.buildable) == coords(ccw.buildable)


def test_collapsed_short_edge_is_trimmed_away():
    # 1 m chamfer at the top-right corner; its offset collapses, so the neighbouring offsets meet.
    parcel = Polygon([(0, 0), (30, 0), (30, 19), (29, 20), (0, 20)])
    res = compute_setbacks_offset(parcel, ["front", "side", "side", "rear", "side"], {"front": 4, "side": 3, "rear": 3})
    assert res.status == "ok"
    assert coords(res.buildable) == [(3, 4), (3, 17), (27, 4), (27, 17)]


def test_narrow_section_splits_into_parts_with_sharp_corners():
    res = compute_setbacks_offset(Polygon(DUMBBELL), ["side"] * 12, {"side": 2})
    assert res.status == "multipart" and res.part_areas == pytest.approx([256, 256])
    assert any("split" in w for w in res.warnings)


def test_fully_inverted_offset_is_empty():
    res = compute_setbacks_offset(Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]), ["side"] * 4, {"side": 3})
    assert res.status == "empty" and res.buildable_area == pytest.approx(0)
    assert res.zone_area == pytest.approx(25)


def test_setback_change_on_nearly_straight_boundary_is_a_perpendicular_step():
    parcel = Polygon([(0, 0), (10, 0), (20, 0.01), (20, 15), (0, 15)])
    res = compute_setbacks_offset(parcel, ["front", "side", "side", "rear", "side"], {"front": 4, "side": 2, "rear": 3})
    c = coords(res.buildable)
    # step perpendicular to the edge with the larger setback (front, 4 m) down to the side offset (2 m)
    assert (10, 4) in c and any(x == pytest.approx(10) and y == pytest.approx(2, abs=1e-5) for x, y in c)
    assert any("perpendicular step" in w for w in res.warnings)


def test_far_reaching_acute_corner_is_listed_for_review():
    res = compute_setbacks_offset(Polygon([(0, 0), (40, 0), (5, 8)]), ["front", "side", "rear"],
                                  {"front": 2, "side": 1, "rear": 1})
    assert res.status == "ok"
    assert [(i.code, i.vertices) for i in res.construction.by_severity("review")] == [("remote_intersection", [2])]


def test_zero_setbacks_keep_the_parcel():
    res = compute_setbacks_offset(Polygon(RECT), ["front", "side", "rear", "side"], {"front": 0, "side": 0, "rear": 0})
    assert res.buildable_area == pytest.approx(600) and any("0 m" in w for w in res.warnings)


def test_validation_is_the_same_as_distance_zone_method():
    parcel = Polygon(RECT)
    with pytest.raises(MissingRuleError):
        compute_setbacks_offset(parcel, ["front", "side", "rear", "corner"], {"front": 4, "side": 3, "rear": 2})
    with pytest.raises(ValidationError):
        compute_setbacks_offset(parcel, ["front", "", "rear", "side"], {"front": 4, "side": 3, "rear": 2})
    with pytest.raises(ValidationError):
        setback_function("negative_buffer")
    assert setback_function(METHOD_DISTANCE_ZONE) is compute_setbacks


def test_invariants_on_random_irregular_parcels():
    rnd = random.Random(7)
    checked = 0
    while checked < 300:
        m = rnd.randint(3, 14)
        angs = sorted(rnd.uniform(0, 2 * math.pi) for _ in range(m))
        pts = [(rnd.uniform(5, 40) * math.cos(a), rnd.uniform(5, 40) * math.sin(a)) for a in angs]
        parcel = Polygon(pts if rnd.random() < 0.5 else pts[::-1])
        if not parcel.is_valid or parcel.area < 1:
            continue
        checked += 1
        types = [rnd.choice(["front", "side", "rear"]) for _ in pts]
        d = {"front": rnd.uniform(0, 6), "side": rnd.uniform(0, 4), "rear": rnd.uniform(0, 5)}
        res = compute_setbacks_offset(parcel, types, d)
        assert res.buildable.is_valid
        assert res.buildable.difference(parcel).area < 1e-6
        assert res.buildable_area + res.zone_area == pytest.approx(parcel.area, abs=1e-6)
        # nothing buildable inside any edge's setback strip
        assert res.buildable.intersection(strips(parcel, types, d)).area < 1e-3


# -- engine integration ------------------------------------------------------------------------

def test_engine_uses_selected_method_and_reproduces(sample_dxf):
    from test_licensing_engine import RULE_B, UNITS

    from sls.cad.dxf_io import import_dxf
    from sls.engine.inputs import extract_inputs
    from sls.samples import SAMPLE_EDGE_TYPES, assign_sample_roles

    drawing, _ = import_dxf(sample_dxf)
    boundary = assign_sample_roles(drawing)
    inputs = extract_inputs(drawing, boundary_entity_id=boundary, parcel_verified=True,
                            edge_types=list(SAMPLE_EDGE_TYPES), rule=RULE_B, rule_version_id="rv-1",
                            rule_version_verified=True, land_use_code="SYN-B", licenses=[], units=UNITS,
                            floor_order=["G", "1"])
    assert inputs.setback_method == METHOD_DISTANCE_ZONE   # default for snapshots and direct callers
    legacy = run_calculation(inputs)
    assert "method" not in legacy["setback"]
    inputs.setback_method = METHOD_OFFSET_CORNER
    new = run_calculation(inputs)
    assert new["setback"]["method"] == METHOD_OFFSET_CORNER
    assert "trimmed/extended" in next(s for s in new["trace"] if s["key"] == "setback")["formula"]
    # The sample parcel is convex: both methods give the same buildable area.
    assert new["setback"]["buildable_area_m2"] == pytest.approx(legacy["setback"]["buildable_area_m2"], abs=1e-6)
    restored = CalculationInputs.from_dict(inputs.to_dict())
    assert restored.setback_method == METHOD_OFFSET_CORNER and restored.input_hash() == inputs.input_hash()
    assert result_hash(run_calculation(restored)) == result_hash(new)


def test_snapshots_from_before_the_method_field_keep_their_hash(sample_dxf):
    from test_licensing_engine import _inputs

    inputs = _inputs(sample_dxf)
    d = inputs.to_dict()
    del d["setback_method"]  # a snapshot stored before the setback fields existed
    del d["setback_approval"]
    del d["datasets"]
    old_hash = hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                              .encode("utf-8")).hexdigest()
    restored = CalculationInputs.from_dict(d)
    assert restored.setback_method == METHOD_DISTANCE_ZONE and restored.input_hash() == old_hash
    # choosing the new method changes the input hash (a different calculation)
    restored.setback_method = METHOD_OFFSET_CORNER
    assert restored.input_hash() != old_hash


def test_case_service_uses_setting_and_stored_records_reproduce(full_case):
    from sls.services.case_service import CaseService

    ws, case = full_case["ws"], full_case["case"]
    assert full_case["calc"].inputs.setback_method == METHOD_OFFSET_CORNER   # new default setting
    assert ws.calculations.reproduce(full_case["calc"].id)[0]
    ws.settings.setback_method = METHOD_DISTANCE_ZONE
    old = CaseService(ws).run_calculation(case.id)
    assert old.inputs.setback_method == METHOD_DISTANCE_ZONE and "method" not in old.results["setback"]
    ws.settings.setback_method = METHOD_OFFSET_CORNER
    assert ws.calculations.reproduce(old.id)[0]   # still reproduced with the method it was made with


def test_settings_dialog_selects_method(qtbot, qapp):
    from sls.core.settings import Settings
    from sls.ui.dialogs.admin_dialogs import SettingsDialog

    s = Settings()
    assert s.setback_method == METHOD_OFFSET_CORNER
    dlg = SettingsDialog(s)
    qtbot.addWidget(dlg)
    assert dlg.setback_method.currentData() == METHOD_OFFSET_CORNER
    dlg.setback_method.setCurrentIndex(dlg.setback_method.findData(METHOD_DISTANCE_ZONE))
    assert dlg.apply().setback_method == METHOD_DISTANCE_ZONE
    assert Settings.from_dict({"language": "en"}).setback_method == METHOD_OFFSET_CORNER  # older settings files
