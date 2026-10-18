"""Geometry engine tests (synthetic geometry only)."""
from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Polygon

from sls.core.errors import CrsError, GeometryError, MissingRuleError, UnitsNotVerifiedError, ValidationError
from sls.core.units import CrsKind, DrawingUnits, LengthUnit, unit_from_insunits
from sls.geometry.areas import Footprint, LinearFeature, floor_areas, length_totals, setback_intersections
from sls.geometry.primitives import MetricFrame, build_linestring, build_polygon, require_polygon
from sls.geometry.setbacks import compute_setbacks, suggest_edge_types

RECT = [(0, 0), (20, 0), (20, 30), (0, 30)]


def test_rectangle_area_and_perimeter():
    poly = require_polygon(RECT, closed_flag=True)
    assert poly.area == pytest.approx(600.0)
    assert poly.length == pytest.approx(100.0)


def test_irregular_polygon_area_matches_shoelace():
    pts = [(0, 0), (12.5, -1.2), (18.3, 7.4), (10.1, 15.9), (2.2, 11.0), (-3.0, 4.5)]
    poly = require_polygon(pts, closed_flag=True)
    shoelace = 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                             for i in range(len(pts))))
    assert poly.area == pytest.approx(shoelace)


def test_open_polygon_is_reported_not_closed_silently():
    poly, issues = build_polygon([(0, 0), (10, 0), (10, 10), (0, 10.5)], closed_flag=False)
    assert poly is None
    assert issues[0].code == "open_polygon"


def test_polygon_closed_by_coincident_endpoints_is_accepted():
    poly, issues = build_polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0.0004)], closed_flag=False)
    assert poly is not None and not issues
    assert poly.area == pytest.approx(100, abs=0.01)


def test_self_intersecting_polygon_is_invalid():
    poly, issues = build_polygon([(0, 0), (10, 10), (10, 0), (0, 10)], closed_flag=True)
    assert poly is None
    assert issues[0].code == "invalid_polygon"


def test_zero_area_and_degenerate_shapes():
    _, issues = build_polygon([(0, 0), (5, 0), (10, 0)], closed_flag=True)
    assert issues[0].code in ("zero_area", "invalid_polygon")
    _, issues = build_polygon([(0, 0), (0, 0), (0, 0)], closed_flag=True)
    assert issues[0].code == "degenerate"
    line, issues = build_linestring([(1, 1), (1, 1)], closed=False)
    assert line is None and issues[0].code == "degenerate"
    with pytest.raises(GeometryError):
        require_polygon([(0, 0), (1, 1)], closed_flag=True)


def test_uniform_setback_on_rectangle():
    parcel = require_polygon(RECT, True)
    res = compute_setbacks(parcel, ["front", "side", "rear", "side"], {"front": 4, "side": 3, "rear": 2})
    # edges: bottom=front(4), right=side(3), top=rear(2), left=side(3) -> 14 x 24
    assert res.status == "ok"
    assert res.buildable_area == pytest.approx(14 * 24)
    assert res.zone_area == pytest.approx(600 - 14 * 24)
    minx, miny, maxx, maxy = res.buildable.bounds
    assert (minx, miny, maxx, maxy) == pytest.approx((3, 4, 17, 28))


def test_different_setbacks_per_side_and_edge_order_is_stable():
    parcel = require_polygon(RECT, True)
    res = compute_setbacks(parcel, ["rear", "front", "side", "side"], {"front": 5, "side": 1, "rear": 2})
    minx, miny, maxx, maxy = res.buildable.bounds
    assert (minx, miny, maxx, maxy) == pytest.approx((1, 2, 15, 29))


def test_missing_rule_stops_calculation():
    parcel = require_polygon(RECT, True)
    with pytest.raises(MissingRuleError):
        compute_setbacks(parcel, ["front", "side", "rear", "corner"], {"front": 4, "side": 3, "rear": 2})


def test_unclassified_edge_and_wrong_count_are_errors():
    parcel = require_polygon(RECT, True)
    with pytest.raises(ValidationError):
        compute_setbacks(parcel, ["front", "", "rear", "side"], {"front": 4, "side": 3, "rear": 2})
    with pytest.raises(ValidationError):
        compute_setbacks(parcel, ["front", "side"], {"front": 4, "side": 3})


def test_non_convex_parcel_setback_uses_distance_not_half_planes():
    # L-shaped parcel. A half-plane approach on the inner edges would wrongly cut away
    # the far arm. The distance-zone method keeps both arms.
    pts = [(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)]
    parcel = require_polygon(pts, True)
    res = compute_setbacks(parcel, ["side"] * 6, {"side": 2})
    assert res.status == "ok"
    for p in [(30, 5), (5, 30)]:
        from shapely.geometry import Point
        assert res.buildable.contains(Point(p))
    # every buildable point is >= 2 m from the boundary
    assert parcel.exterior.distance(res.buildable) == pytest.approx(2, abs=1e-4)


def test_narrow_section_splits_buildable_area():
    # Dumbbell: two 20x20 squares joined by a 3 m wide neck; 2 m setbacks close the neck.
    pts = [(0, 0), (20, 0), (20, 8.5), (30, 8.5), (30, 0), (50, 0), (50, 20), (30, 20), (30, 11.5),
           (20, 11.5), (20, 20), (0, 20)]
    parcel = require_polygon(pts, True)
    res = compute_setbacks(parcel, ["side"] * len(pts), {"side": 2})
    assert res.status == "multipart"
    assert len(res.part_areas) == 2
    assert any("split" in w for w in res.warnings)


def test_setbacks_consuming_whole_parcel_is_empty():
    parcel = require_polygon([(0, 0), (5, 0), (5, 5), (0, 5)], True)
    res = compute_setbacks(parcel, ["side"] * 4, {"side": 3})
    assert res.status == "empty"
    assert res.buildable_area == pytest.approx(0)


def test_building_setback_intersection():
    parcel = require_polygon(RECT, True)
    res = compute_setbacks(parcel, ["front", "side", "rear", "side"], {"front": 4, "side": 3, "rear": 2})
    # building 2..12 x 2..12 : intrudes 1 m into left side zone, 2 m into front zone
    building = require_polygon([(2, 2), (12, 2), (12, 12), (2, 12)], True)
    floor = floor_areas([Footprint("b1", "G", building)])[0]
    inter = setback_intersections(floor, parcel, res.setback_zone)
    expected_inside_buildable = (12 - 3) * (12 - 4)
    assert inter.in_setback_zone_m2 == pytest.approx(100 - expected_inside_buildable)
    assert inter.outside_parcel_m2 == pytest.approx(0)


def test_building_outside_parcel_is_reported_separately():
    parcel = require_polygon(RECT, True)
    res = compute_setbacks(parcel, ["front", "side", "rear", "side"], {"front": 0, "side": 0, "rear": 0})
    building = require_polygon([(-2, 5), (5, 5), (5, 10), (-2, 10)], True)
    inter = setback_intersections(floor_areas([Footprint("b", "G", building)])[0], parcel, res.setback_zone)
    assert inter.outside_parcel_m2 == pytest.approx(10)
    assert inter.in_setback_zone_m2 == pytest.approx(0)


def test_overlapping_footprints_not_double_counted():
    a = require_polygon([(0, 0), (10, 0), (10, 10), (0, 10)], True)
    b = require_polygon([(5, 0), (15, 0), (15, 10), (5, 10)], True)
    fa = floor_areas([Footprint("a", "G", a), Footprint("b", "G", b)])[0]
    assert fa.area_m2 == pytest.approx(150)
    assert fa.sum_of_parts_m2 == pytest.approx(200)
    assert fa.overlap_m2 == pytest.approx(50)


def test_duplicate_entities_detected():
    a = require_polygon([(0, 0), (10, 0), (10, 10), (0, 10)], True)
    fa = floor_areas([Footprint("a", "G", a), Footprint("a2", "G", a)])[0]
    assert fa.duplicate_entity_ids == ["a2"]
    assert fa.area_m2 == pytest.approx(100)


def test_multipart_floor_and_multiple_floors_ordered():
    a = require_polygon([(0, 0), (10, 0), (10, 10), (0, 10)], True)
    b = require_polygon([(20, 0), (25, 0), (25, 4), (20, 4)], True)
    c = require_polygon([(0, 0), (8, 0), (8, 8), (0, 8)], True)
    floors = floor_areas([Footprint("c", "1", c), Footprint("a", "G", a), Footprint("b", "G", b)],
                         floor_order=["G", "1"])
    assert [f.floor_key for f in floors] == ["G", "1"]
    assert floors[0].part_count == 2
    assert floors[0].area_m2 == pytest.approx(120)
    assert sum(f.area_m2 for f in floors) == pytest.approx(184)


def test_wall_and_fence_lengths_overlap_counted_once():
    feats = [
        LinearFeature("w1", "wall", LineString([(0, 0), (10, 0)])),
        LinearFeature("w2", "wall", LineString([(5, 0), (15, 0)])),
        LinearFeature("f1", "fence", LineString([(0, 0), (0, 3), (4, 3)])),
    ]
    totals = {t.kind: t for t in length_totals(feats)}
    assert totals["wall"].length_m == pytest.approx(15)
    assert totals["wall"].overlap_m == pytest.approx(5)
    assert totals["fence"].length_m == pytest.approx(7)


def test_metric_conversion_from_millimetres_and_large_coordinates():
    units = DrawingUnits(LengthUnit.MILLIMETER, True, "local", CrsKind.LOCAL)
    factor = units.require_metric_planar()
    pts_mm = [(250_000_000.0, 1_000_000_000.0), (250_020_000.0, 1_000_000_000.0),
              (250_020_000.0, 1_000_030_000.0), (250_000_000.0, 1_000_030_000.0)]
    frame = MetricFrame.for_points(pts_mm, factor)
    poly = require_polygon(frame.to_local(pts_mm), True)
    assert poly.area == pytest.approx(600.0)
    back = frame.to_drawing_geom(poly)
    assert back.bounds[0] == pytest.approx(250_000_000.0)


def test_unknown_or_unverified_units_refused():
    with pytest.raises(UnitsNotVerifiedError):
        DrawingUnits(LengthUnit.UNKNOWN).require_metric_planar()
    with pytest.raises(UnitsNotVerifiedError):
        DrawingUnits(LengthUnit.METER, verified=False, crs_kind=CrsKind.PROJECTED).require_metric_planar()
    with pytest.raises(CrsError):
        DrawingUnits(LengthUnit.METER, True, "EPSG:4326", CrsKind.GEOGRAPHIC).require_metric_planar()
    with pytest.raises(CrsError):
        DrawingUnits(LengthUnit.METER, True, "", CrsKind.UNKNOWN).require_metric_planar()
    assert unit_from_insunits(0) is LengthUnit.UNKNOWN
    assert unit_from_insunits(6) is LengthUnit.METER


def test_edge_type_suggestions_only_mark_road_edges():
    parcel = require_polygon(RECT, True)
    road = Polygon([(-5, -8), (25, -8), (25, 0), (-5, 0)])
    assert suggest_edge_types(parcel, [road]) == ["front", None, None, None]


def test_offset_geometry_reflex_corner_is_true_distance():
    pts = [(0, 0), (20, 0), (20, 20), (10, 20), (10, 10), (0, 10)]
    parcel = require_polygon(pts, True)
    res = compute_setbacks(parcel, ["side"] * 6, {"side": 1})
    # area check: min distance from boundary for all buildable vertices >= 1 (within arc tolerance)
    for x, y in res.buildable.exterior.coords:
        from shapely.geometry import Point
        assert parcel.exterior.distance(Point(x, y)) >= 1 - 1e-4
    assert not math.isclose(res.buildable_area, 0)
