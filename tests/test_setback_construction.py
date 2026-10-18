"""Surveyor setback construction: intermediate geometry, diagnostics, review gate, approval.

Synthetic geometry only. Expected values are derived by hand (analytic offsets), not from
AutoCAD; comparison with the authority's reference drawings is in test_setback_reference.py.
"""
from __future__ import annotations

import json
import math

import pytest
from shapely import affinity
from shapely.geometry import Polygon

from conftest import ADMIN, REVIEWER, SURVEYOR
from sls.core.errors import PermissionDeniedError, ValidationError
from sls.geometry.setback_construction import LINEAR_TOL_M, REMOTE_FACTOR
from sls.geometry.setbacks import compute_setbacks, compute_setbacks_offset
from sls.samples import OX, OY
from sls.services.case_service import CONSTRUCTION_LAYERS, CaseService

RECT = [(0, 0), (20, 0), (20, 30), (0, 30)]
L_SHAPE = [(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)]
DUMBBELL = [(0, 0), (20, 0), (20, 8.5), (30, 8.5), (30, 0), (50, 0), (50, 20), (30, 20), (30, 11.5),
            (20, 11.5), (20, 20), (0, 20)]


def build(pts, types, d):
    res = compute_setbacks_offset(Polygon(pts), types, d)
    return res, res.construction


def codes(con, severity=None):
    return sorted({i.code for i in con.issues if severity is None or i.severity == severity})


def coords(geom):
    return sorted((round(x, 6), round(y, 6)) for x, y in list(geom.exterior.coords)[:-1])


def line_distance(p, a, b):
    return abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / math.dist(a, b)


# -- geometry ------------------------------------------------------------------------------------

def test_rectangle_offsets_are_trimmed_at_every_convex_corner():
    parcel = Polygon(RECT)
    before = parcel.wkt
    res, con = build(RECT, ["front", "side", "rear", "side"], {"front": 4, "side": 3, "rear": 2})
    assert parcel.wkt == before  # original geometry untouched
    assert con.valid and codes(con) == [] and con.review_keys == []
    assert [(e.start_action, e.end_action, e.outcome) for e in con.edges] == [("trim", "trim", "used")] * 4
    assert [j.kind for j in con.joins] == ["intersection"] * 4
    # raw OFFSET of the front edge: same length as the edge, 4 m inside
    assert con.edges[0].raw == ((0, 4), (20, 4))
    assert con.edges[0].trimmed == [((0, 4), (3, 4)), ((17, 4), (20, 4))]
    assert coords(res.buildable) == [(3, 4), (3, 28), (17, 4), (17, 28)]


def test_irregular_convex_rotated_parcel_matches_analytic_offsets():
    pts = [(0, 0), (30, -4), (38, 12), (20, 26), (-3, 18)]
    poly = affinity.rotate(Polygon(pts), 23, origin=(0, 0))
    rp = list(poly.exterior.coords)[:-1]
    types = ["front", "side", "rear", "side", "side"]
    d = {"front": 5, "side": 2.5, "rear": 3}
    res, con = build(rp, types, d)
    assert con.valid and con.review_keys == []
    for e in con.edges:
        assert line_distance(e.raw[0], e.start, e.end) == pytest.approx(e.distance_m, abs=1e-9)
        assert line_distance(e.trimmed_extended[0], e.start, e.end) == pytest.approx(e.distance_m, abs=1e-9)
    # convex parcel: identical to the distance-zone result
    legacy = compute_setbacks(Polygon(rp), types, d)
    assert res.buildable.symmetric_difference(legacy.buildable).area < 1e-6


def test_concave_parcel_extends_offsets_to_a_sharp_reflex_corner():
    res, con = build(L_SHAPE, ["side"] * 6, {"side": 2})
    assert con.review_keys == []
    assert (con.edges[2].end_action, con.edges[3].start_action) == ("extend", "extend")
    assert con.edges[2].extended == [((10, 8), (8, 8))]
    assert (8, 8) in coords(res.buildable)  # no arc, no bevel


def test_obtuse_corner_join_point_is_at_d_over_sin_half_angle():
    # convex corner at (20,0) with an interior angle of 135°
    pts = [(0, 0), (20, 0), (40, 20), (40, 40), (0, 40)]
    res, con = build(pts, ["side"] * 5, {"side": 2})
    j = con.joins[1]
    assert j.kind == "intersection"
    assert j.distance_m == pytest.approx(2 / math.sin(math.radians(135 / 2)), abs=1e-9)
    assert con.review_keys == []


def test_acute_corner_with_remote_intersection_needs_review():
    res, con = build([(0, 0), (40, 0), (5, 8)], ["front", "side", "rear"], {"front": 2, "side": 1, "rear": 1})
    issue = next(i for i in con.issues if i.code == "remote_intersection")
    assert issue.severity == "review" and issue.vertices == [2] and issue.edges == [1, 2]
    assert con.joins[1].distance_m > REMOTE_FACTOR * 2


def test_parallel_offsets_with_different_setbacks_have_no_unique_intersection():
    pts = [(0, 0), (10, 0), (20, 0), (20, 15), (0, 15)]
    res, con = build(pts, ["front", "side", "side", "rear", "side"], {"front": 4, "side": 2, "rear": 3})
    assert con.joins[1].kind == "parallel_step"
    assert codes(con, "review") == ["parallel_offsets"]
    assert con.joins[1].points == [(10, 4), (10, 2)]
    # same setback on collinear edges: one line, informational only
    res2, con2 = build(pts, ["front", "front", "side", "rear", "side"], {"front": 4, "side": 2, "rear": 3})
    assert con2.joins[1].kind == "collinear" and codes(con2) == ["collinear_edges"] and con2.review_keys == []


def test_unequal_setbacks_on_rotated_rectangle():
    poly = affinity.rotate(Polygon(RECT), 30, origin=(0, 0))
    res, con = build(list(poly.exterior.coords)[:-1], ["front", "side", "rear", "side"],
                     {"front": 5, "side": 2, "rear": 3})
    expected = affinity.rotate(Polygon([(2, 5), (18, 5), (18, 27), (2, 27)]), 30, origin=(0, 0))
    assert res.buildable.symmetric_difference(expected).area < 1e-9
    assert con.review_keys == []


def test_collapsed_short_edge_is_reported_and_trimmed_away():
    res, con = build([(0, 0), (30, 0), (30, 19), (29, 20), (0, 20)], ["front", "side", "side", "rear", "side"],
                     {"front": 4, "side": 3, "rear": 3})
    assert codes(con, "review") == ["collapsed_edge", "self_intersection"]
    assert con.edges[2].collapsed and con.edges[2].outcome == "collapsed"
    assert len(con.discarded_faces) == 1 and "wound against" in con.discarded_faces[0][1]
    assert coords(res.buildable) == [(3, 4), (3, 17), (27, 4), (27, 17)]


def test_collapsed_area_is_empty_with_every_discard_reported():
    res, con = build([(0, 0), (5, 0), (5, 5), (0, 5)], ["side"] * 4, {"side": 3})
    assert res.status == "empty" and res.buildable_area == 0
    assert {"collapsed_edge", "inverted_offset", "empty_result"} <= set(codes(con, "review"))
    assert sum(f.area for f, _ in con.discarded_faces) == pytest.approx(1)


def test_self_intersection_and_multiple_polygons_list_discarded_faces():
    res, con = build(DUMBBELL, ["side"] * 12, {"side": 2})
    assert {"self_intersection", "multiple_polygons", "edge_eliminated"} <= set(codes(con, "review"))
    assert sorted(p.area for p in con.kept_faces) == pytest.approx([256, 256])
    assert sum(f.area for f, _ in con.discarded_faces) == pytest.approx(14)
    assert [e.index + 1 for e in con.edges if e.outcome == "eliminated"] == [3, 9]
    assert res.status == "multipart" and res.part_areas == pytest.approx([256, 256])


def test_clockwise_input_gives_the_same_construction():
    types = ["front", "side", "rear", "side", "rear", "side"]
    d = {"front": 5, "side": 2, "rear": 3}
    res, con = build(L_SHAPE, types, d)
    rev = [L_SHAPE[0]] + L_SHAPE[:0:-1]
    res2, con2 = build(rev, [types[5], types[4], types[3], types[2], types[1], types[0]], d)
    assert con2.orientation == "cw" and coords(res2.buildable) == coords(res.buildable)


def test_issue_keys_are_stable_and_construction_is_json_ready():
    _, con = build(DUMBBELL, ["side"] * 12, {"side": 2})
    _, con2 = build(DUMBBELL, ["side"] * 12, {"side": 2})
    assert con.review_keys == con2.review_keys and con.review_keys
    d = con.to_dict(lambda p: p, lambda g: g.wkt)
    json.dumps(d)
    assert len(d["edges"]) == 12 and d["discarded"] and d["buildable_wkt"].startswith("MULTIPOLYGON")
    assert LINEAR_TOL_M == 1e-6


# -- engine / service: review gate, approval, invalidation ----------------------------------------------

ACUTE = [(0.0, 0.0), (60.0, 0.0), (10.0, 8.0), (0.0, 8.0)]  # remote join at the acute corner (review item)


def _set_boundary(ws, case_id, local_pts):
    svc = CaseService(ws)
    loaded = svc.survey_drawing(case_id)
    parcel = ws.drawings.parcel(case_id)
    ent = loaded.drawing.entities[parcel.boundary_entity_id]
    ent.data["points"] = [[OX + x, OY + y] for x, y in local_pts]
    ws.drawings.save_revision(ADMIN, loaded.record.id, loaded.drawing, "boundary edited")
    parcel.boundary_points = [list(p) for p in ent.data["points"]]
    ws.drawings.save_parcel(ADMIN, parcel, verify=True)
    return svc


def test_review_items_block_area_until_approved_and_approval_is_invalidated(full_case):
    ws, case = full_case["ws"], full_case["case"]
    svc = _set_boundary(ws, case.id, ACUTE)
    calc = svc.run_calculation(case.id)
    assert "setback" not in calc.results
    assert [e["code"] for e in calc.results["errors"]] == ["setback_review_required"]
    con = calc.results["setback_construction"]
    assert con["review_keys"] and con["edges"][0]["raw_offset"][0][0] > OX - 1   # drawing coordinates

    revisions_before = len(ws.drawings.revisions(svc.survey_drawing(case.id).record.id))
    preview = svc.setback_preview(case.id)  # preview stores nothing
    assert len(ws.drawings.revisions(svc.survey_drawing(case.id).record.id)) == revisions_before
    with pytest.raises(ValidationError):
        svc.approve_setback(case.id, "", preview["construction"]["fingerprint"])        # note required
    with pytest.raises(ValidationError):
        svc.approve_setback(case.id, "checked", "stale-fingerprint")
    ws.user = SURVEYOR
    with pytest.raises(PermissionDeniedError):
        svc.approve_setback(case.id, "checked", preview["construction"]["fingerprint"])
    ws.user = REVIEWER
    boundary_before = svc.survey_drawing(case.id).drawing.entities[ws.drawings.parcel(case.id).boundary_entity_id].data
    appr = svc.approve_setback(case.id, "Acute corner checked on site plan.", preview["construction"]["fingerprint"])
    assert appr["approved_by"] == "reviewer1" and appr["issue_keys"] == con["review_keys"]
    ws.user = ADMIN

    loaded = svc.survey_drawing(case.id)
    assert loaded.revision.id == appr["drawing_revision_id"]
    assert loaded.drawing.entities[ws.drawings.parcel(case.id).boundary_entity_id].data == boundary_before
    layers = {e.layer for e in loaded.drawing.entities.values() if e.meta.get("approval_id") == appr["id"]}
    assert {CONSTRUCTION_LAYERS[k][0] for k in ("offset", "joined", "trimmed", "corner", "buildable")} <= layers

    ok = svc.run_calculation(case.id)
    assert ok.results["setback"]["buildable_area_m2"] > 0
    assert ok.results["setback_construction"]["approval"]["id"] == appr["id"]
    assert "setback_reviewed" in {i["code"] for i in ok.results["issues"]}
    edge = ok.results["setback"]["edges"][0]
    assert {"outcome", "start_action", "end_action"} <= set(edge) and ok.results["setback"]["rule_version_id"]
    assert ws.calculations.reproduce(ok.id)[0]

    # changing an edge classification invalidates the approval
    parcel = ws.drawings.parcel(case.id)
    parcel.edge_types = ["front", "side", "side", "side"]
    ws.drawings.save_parcel(ADMIN, parcel, verify=True)
    stale = svc.run_calculation(case.id)
    assert "setback" not in stale.results
    msg = next(i["message"] for i in stale.results["issues"] if i["code"] == "setback_approval_stale")
    assert "edge classifications" in msg and "property boundary" not in msg


def test_clean_construction_needs_no_approval_and_accept_stores_layers(full_case):
    ws, case = full_case["ws"], full_case["case"]
    svc = CaseService(ws)
    assert full_case["calc"].results["setback_construction"]["review_keys"] == []
    ws.user = SURVEYOR  # no review items: any user who may calculate can store the construction
    fp = svc.setback_preview(case.id)["construction"]["fingerprint"]
    appr = svc.approve_setback(case.id, "", fp)
    ws.user = ADMIN
    assert appr["issue_keys"] == []
    assert ws.setback_approvals.latest(case.id)["id"] == appr["id"]


def test_invalid_geometry_cannot_be_approved(full_case, monkeypatch):
    ws, case = full_case["ws"], full_case["case"]
    svc = CaseService(ws)
    prev = svc.setback_preview(case.id)
    prev["construction"]["issues"].append({"severity": "error", "code": "validation_failed", "message": "x",
                                           "edges": [], "vertices": [], "area_m2": None, "key": "k"})
    monkeypatch.setattr(svc, "setback_preview", lambda case_id: prev)
    with pytest.raises(ValidationError, match="cannot be approved"):
        svc.approve_setback(case.id, "note", prev["construction"]["fingerprint"])


def test_preview_dialog_states_and_accept(full_case, qtbot, qapp):
    from sls.ui.dialogs.setback_preview_dialog import SetbackPreviewDialog

    ws, case = full_case["ws"], full_case["case"]
    _set_boundary(ws, case.id, ACUTE)
    dlg = SetbackPreviewDialog(ws, case.id)
    qtbot.addWidget(dlg)
    assert dlg.state() == "review" and not dlg.accept_btn.isEnabled()
    dlg.note.setPlainText("checked")
    assert not dlg.accept_btn.isEnabled()
    dlg.reviewed.setChecked(True)
    assert dlg.accept_btn.isEnabled()
    dlg.issues.selectRow(0)
    assert dlg.view.highlight_edges or dlg.view.highlight_vertices
    dlg.view.grab()  # paints every layer
    dlg._accept()
    assert dlg.approval is not None and dlg.result() == dlg.DialogCode.Accepted
