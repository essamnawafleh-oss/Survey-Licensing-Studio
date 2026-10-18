"""The calculation engine: inputs snapshot -> traceable results.

Sections run independently where possible: a missing rule stops the setback section
but measured areas and lengths are still reported. Every number is accompanied by a
trace step (inputs, formula, unit, sources, warnings).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from shapely import wkt as shapely_wkt
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..core.errors import SlsError
from ..geometry.areas import Footprint, LinearFeature, floor_areas, length_totals, setback_intersections
from ..geometry.primitives import MetricFrame, build_linestring, build_polygon
from ..geometry.setback_construction import CONSTRUCTION_VERSION, ERROR
from ..geometry.setbacks import METHOD_OFFSET_CORNER, SetbackResult, setback_function, suggest_edge_types
from ..licensing.reconcile import reconcile
from .inputs import CalculationInputs

ENGINE_VERSION = "1.1.0"  # 1.1.0: outside-parcel and nested-outline issues, unknown-CRS warning (audit)
DISCLAIMER_EN = ("Preliminary rule check only. Geometric results do not constitute a legal licensing decision; "
                 "the decision rests with authorised staff.")
DISCLAIMER_AR = "فحص أولي للأنظمة فقط. النتائج الهندسية لا تشكل قراراً قانونياً بالترخيص؛ القرار للموظف المخول."


@dataclass
class Trace:
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, key: str, title: str, formula: str, inputs: dict[str, Any], result: Any, unit: str,
            sources: list[str] | None = None, warnings: list[str] | None = None) -> None:
        self.steps.append({"key": key, "title": title, "formula": formula, "inputs": inputs, "result": result,
                           "unit": unit, "sources": sources or [], "warnings": warnings or []})


def _round(obj: Any, nd: int = 6) -> Any:
    if isinstance(obj, float):
        r = round(obj, nd)
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def _wkt(geom: BaseGeometry, frame: MetricFrame) -> str:
    if geom.is_empty:
        return ""
    return shapely_wkt.dumps(frame.to_drawing_geom(geom), rounding_precision=6)


def result_hash(results: dict[str, Any]) -> str:
    payload = json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_calculation(inputs: CalculationInputs) -> dict[str, Any]:
    trace = Trace()
    errors: list[dict[str, str]] = []
    issues: list[dict[str, str]] = list(inputs.extraction_issues)
    out: dict[str, Any] = {"engine_version": ENGINE_VERSION, "input_hash": inputs.input_hash(),
                           "land_use_code": inputs.land_use_code, "rule_version_id": inputs.rule_version_id}

    def fail(section: str, exc: SlsError | str, code: str = "") -> None:
        if isinstance(exc, SlsError):
            errors.append({"section": section, "code": exc.code, "message": exc.message})
        else:
            errors.append({"section": section, "code": code or "error", "message": exc})

    # 1. Units / CRS -----------------------------------------------------------------
    units = inputs.drawing_units
    try:
        factor = units.require_metric_planar()
    except SlsError as exc:
        fail("units", exc)
        out.update(status="error", errors=errors, issues=issues, trace=trace.steps)
        return _round(out)
    all_pts = [p for s in inputs.footprints + inputs.linear for p in s.points]
    if inputs.parcel:
        all_pts += inputs.parcel.points
    frame = MetricFrame.for_points(all_pts, factor)
    out["units"] = inputs.units
    out["frame"] = frame.to_dict()
    trace.add("units", "Drawing units", "metres = drawing units × factor", {"unit": units.unit.value,
              "crs": units.crs_id, "crs_kind": units.crs_kind.value}, factor, "m / drawing unit",
              [f"verified by {units.verified_by or 'user'}"])
    _crs_checks(inputs, all_pts, factor, issues, trace, out)
    _zoning_checks(inputs, issues, trace, out)
    if inputs.datasets:
        out["datasets"] = inputs.datasets  # only when recorded, so older results reproduce unchanged
    if any(s.approximated for s in inputs.footprints + inputs.linear) or (inputs.parcel and inputs.parcel.approximated):
        issues.append({"severity": "warning", "code": "curves_approximated",
                       "message": "Curved geometry (arcs, circles, splines) was sampled at 256 segments per full "
                                  "circle for area/length calculation.", "entity_id": ""})

    # 2. Parcel ----------------------------------------------------------------------
    parcel: Polygon | None = None
    if inputs.parcel is None:
        fail("parcel", "No property boundary has been selected for this case.", "boundary_not_selected")
    else:
        parcel, p_issues = build_polygon(frame.to_local(inputs.parcel.points), inputs.parcel.closed,
                                         inputs.parcel.entity_id)
        issues += [i.to_dict() for i in p_issues]
        if parcel is None:
            fail("parcel", "The property boundary is not a valid closed polygon.", "boundary_invalid")
        else:
            out["parcel"] = {"entity_id": inputs.parcel.entity_id, "area_m2": parcel.area,
                             "perimeter_m": parcel.length, "verified": inputs.parcel_verified,
                             "wkt": _wkt(parcel, frame)}
            trace.add("parcel_area", "Parcel area", "A = ½ |Σ (x_i·y_(i+1) − x_(i+1)·y_i)|  (shoelace)",
                      {"vertices": len(parcel.exterior.coords) - 1}, parcel.area, "m²", [inputs.parcel.entity_id])
            if not inputs.parcel_verified:
                fail("parcel", "The property boundary has not been verified by a user; setbacks are not "
                               "calculated from an unverified boundary.", "boundary_not_verified")

    # 3. Setbacks ----------------------------------------------------------------------
    setback: SetbackResult | None = None
    rule = inputs.rule_obj
    out["rule"] = inputs.rule
    out["rule_override"] = inputs.rule_override
    if rule is None:
        fail("setback", f"No rule is applied for land-use classification '{inputs.land_use_code}'.",
             "missing_rule")
    if not inputs.rule_version_verified and rule is not None:
        issues.append({"severity": "warning", "code": "rule_not_verified",
                       "message": "The applied rule version has not been verified by a reviewer.", "entity_id": ""})
    override = inputs.rule_override or {}
    distances: dict[str, float] = dict(rule.setbacks) if rule else {}
    overridden = {k: float(v) for k, v in (override.get("setbacks") or {}).items() if k not in distances}
    distances.update(overridden)
    if rule is not None and overridden:
        issues.append({"severity": "warning", "code": "setback_override",
                       "message": "Setback values entered by reviewer {by} for conditional rule text: {vals}. "
                                  "Note: {note}".format(by=override.get("entered_by", ""), note=override.get("note", ""),
                                                        vals=", ".join(f"{k}={v:g} m (rule: '{rule.raw_setback(k)}')"
                                                                       for k, v in overridden.items())),
                       "entity_id": ""})
    if parcel is not None and rule is not None and inputs.parcel_verified:
        needed = [t for t in dict.fromkeys(inputs.edge_types) if t]
        unresolved = [t for t in needed if t not in distances]
        if rule.status == "no_setbacks" and unresolved:
            fail("setback", f"Classification {rule.code} ('{rule.name_ar}') defines no setback values in the rule "
                            "table; setbacks cannot be calculated.", "missing_rule")
        elif unresolved:
            conditional = [t for t in unresolved if rule.raw_setback(t)]
            undefined = [t for t in unresolved if not rule.raw_setback(t)]
            if undefined:
                fail("setback", "No setback value is defined in the rule table for boundary type(s): "
                                + ", ".join(undefined) + ".", "missing_rule")
            if conditional:
                parts = ", ".join(f"{t}: '{rule.raw_setback(t)}'" for t in conditional)
                fail("setback", f"The rule table value is not a plain number for {parts}. A reviewer must enter "
                                "the applicable value for this case (Case → Conditional setback values).",
                     "conditional_rule")
        else:
            try:
                setback = setback_function(inputs.setback_method)(parcel, inputs.edge_types, distances,
                                                                  inputs.sliver_tolerance_m2)
            except SlsError as exc:
                fail("setback", exc)
            if setback is not None and setback.construction is not None:
                setback = _setback_gate(inputs, setback, distances, frame, issues, out, fail)
    if parcel is not None and inputs.roads and not inputs.edge_types:
        roads = []
        for r in inputs.roads:
            g, _ = (build_polygon(frame.to_local(r.points), True) if r.closed
                    else build_linestring(frame.to_local(r.points), False))
            if g is not None:
                roads.append(g)
        out["edge_type_suggestions"] = suggest_edge_types(parcel, roads)
    if setback is not None:
        out["setback"] = {
            "status": setback.status, "buildable_area_m2": setback.buildable_area,
            "zone_area_m2": setback.zone_area, "part_areas_m2": setback.part_areas, "warnings": setback.warnings,
            "edges": [{"index": e.index + 1, "boundary_type": e.boundary_type, "length_m": e.length,
                       "setback_m": e.distance_m} for e in setback.edges],
            "buildable_wkt": _wkt(setback.buildable, frame), "zone_wkt": _wkt(setback.setback_zone, frame),
        }
        if setback.method == METHOD_OFFSET_CORNER:
            # Only for the new method, so results of earlier distance-zone calculations reproduce unchanged.
            out["setback"]["method"] = setback.method
            out["setback"]["rule_version_id"] = inputs.rule_version_id
            for row, e in zip(out["setback"]["edges"], setback.construction.edges):  # type: ignore[union-attr]
                row.update(start_action=e.start_action, end_action=e.end_action, outcome=e.outcome)
            formula = ("Buildable = Parcel ∩ polygon of the inward offsets of each edge i by d_i, consecutive "
                       "offsets trimmed/extended to their intersection (sharp corners); loops of collapsed "
                       "edges removed; validated before the area is taken")
        else:
            formula = "Buildable = Parcel − union over edges i of { p : dist(p, edge_i) < d_i }"
        trace.add("setback", "Buildable area after setbacks", formula,
                  {"distances_m": distances, "edge_types": inputs.edge_types,
                   "rule_code": rule.code if rule else "",
                   "rule_text": dict(rule.setbacks_raw) if rule else {},
                   "reviewer_values": overridden}, setback.buildable_area, "m²",
                  [f"rule version {inputs.rule_version_id}", f"rule row {rule.source_row}" if rule else "",
                   f"reviewer values by {override.get('entered_by')}: {override.get('note')}" if overridden else ""],
                  setback.warnings)

    # 4. Floor areas -------------------------------------------------------------------
    fps: list[Footprint] = []
    for s in inputs.footprints:
        poly, f_issues = build_polygon(frame.to_local(s.points), s.closed, s.entity_id)
        issues += [i.to_dict() for i in f_issues]
        if poly is not None:
            fps.append(Footprint(s.entity_id, s.floor_key, poly))
    floors = floor_areas(fps, inputs.floor_order)
    for a in fps:
        for b in fps:
            if a is not b and a.floor_key == b.floor_key and b.polygon.area < a.polygon.area \
                    and a.polygon.contains(b.polygon):
                issues.append({"severity": "warning", "code": "nested_outline",
                               "message": f"Floor {a.floor_key}: an outline ({b.polygon.area:.2f} m²) lies entirely "
                                          "inside another; it is counted as built area. If it is a courtyard or "
                                          "light well, draw the floor as one outline with the opening excluded.",
                               "entity_id": b.entity_id})
    floor_rows: list[dict[str, Any]] = []
    for fa in floors:
        row: dict[str, Any] = {"floor_key": fa.floor_key, "area_m2": fa.area_m2, "sum_of_parts_m2": fa.sum_of_parts_m2,
                               "overlap_m2": fa.overlap_m2, "entity_ids": fa.entity_ids, "part_count": fa.part_count,
                               "duplicate_entity_ids": fa.duplicate_entity_ids, "wkt": _wkt(fa.geometry, frame)}
        warns = []
        if fa.overlap_m2 > 1e-4:
            warns.append(f"Overlapping outlines on floor {fa.floor_key}: {fa.overlap_m2:.2f} m² counted once.")
        if fa.duplicate_entity_ids:
            warns.append(f"Duplicate outlines on floor {fa.floor_key}: {', '.join(fa.duplicate_entity_ids)}.")
        if parcel is not None:
            inter = setback_intersections(fa, parcel, setback.setback_zone if setback else Polygon())
            row["outside_parcel_m2"] = inter.outside_parcel_m2
            row["outside_parcel_wkt"] = _wkt(inter.outside_parcel, frame)
            if inter.outside_parcel_m2 > 0.01:
                warns.append(f"Floor {fa.floor_key}: {inter.outside_parcel_m2:.2f} m² of the building lies outside "
                             "the property boundary (neighbouring land or road).")
            if setback is not None:
                row["in_setback_zone_m2"] = inter.in_setback_zone_m2
                row["violation_wkt"] = _wkt(inter.in_setback_zone, frame)
        floor_rows.append(row)
        trace.add(f"floor_area:{fa.floor_key}", f"Measured area, floor {fa.floor_key}",
                  "A_floor = area(union of outlines on the floor)", {"outlines": len(fa.entity_ids),
                  "sum_of_outline_areas_m2": fa.sum_of_parts_m2}, fa.area_m2, "m²", fa.entity_ids, warns)
        issues += [{"severity": "warning", "code": "overlap", "message": w, "entity_id": ""} for w in warns]
    out["floors"] = floor_rows
    total_measured = sum(fa.area_m2 for fa in floors)
    trace.add("total_measured", "Total measured building area (all floors)", "Σ A_floor",
              {f.floor_key: f.area_m2 for f in floors}, total_measured, "m²")

    plan = unary_union([fa.geometry for fa in floors]) if floors else Polygon()
    plan_row: dict[str, Any] = {"area_m2": float(plan.area), "wkt": _wkt(plan, frame)}
    if parcel is not None and floors:
        plan_row["outside_parcel_m2"] = float(plan.difference(parcel).area)
        if setback is not None:
            viol = plan.intersection(setback.setback_zone)
            plan_row["in_setback_zone_m2"] = float(viol.area)
            plan_row["violation_wkt"] = _wkt(viol, frame)
            trace.add("violation_plan", "Building plan area inside setback zone (geometric)",
                      "area(union of all floors, intersected with setback zone)", {}, float(viol.area), "m²",
                      warnings=["Geometric intersection only; not a legal determination."])
    out["plan_projection"] = plan_row

    # 5. Lengths -----------------------------------------------------------------------
    feats: list[LinearFeature] = []
    for s in inputs.linear:
        line, l_issues = build_linestring(frame.to_local(s.points), s.closed, s.entity_id)
        issues += [i.to_dict() for i in l_issues]
        if line is not None:
            feats.append(LinearFeature(s.entity_id, s.kind, line))
    lengths = length_totals(feats)
    out["lengths"] = [{"kind": t.kind, "length_m": t.length_m, "sum_of_parts_m": t.sum_of_parts_m,
                       "overlap_m": t.overlap_m, "entity_ids": t.entity_ids} for t in lengths]
    for t in lengths:
        trace.add(f"length:{t.kind}", f"Measured {t.kind} length", "L = length(union of segments)",
                  {"segments": len(t.entity_ids), "sum_of_segment_lengths_m": t.sum_of_parts_m}, t.length_m, "m",
                  t.entity_ids, [f"Overlapping segments: {t.overlap_m:.2f} m counted once."] if t.overlap_m > 1e-4 else [])

    # 6. Previous licenses --------------------------------------------------------------
    rec = reconcile({fa.floor_key: fa.area_m2 for fa in floors}, {t.kind: t.length_m for t in lengths},
                    inputs.license_inputs)
    out["reconciliation"] = rec.to_dict()
    for line in rec.areas:
        trace.add(f"remaining:{line.key}", f"Preliminary remaining area to assess, floor {line.key}",
                  "Remaining = Measured − Σ verified licensed", {"measured_m2": line.measured,
                  "licensed_m2": line.licensed}, line.remaining, "m²", line.license_numbers,
                  line.conflicts + line.warnings)
    trace.add("remaining_total", "Preliminary remaining area to assess (all floors)",
              "Σ (Measured − Licensed) per floor", {"measured_total_m2": rec.total_measured_area,
              "licensed_total_m2": rec.total_licensed_area}, rec.total_remaining_area, "m²",
              warnings=rec.warnings + rec.assumptions + ([rec.total_remaining_reason] if rec.total_remaining_reason else []))

    # 7. Preliminary eligibility checks --------------------------------------------------
    out["eligibility"] = _eligibility(rule, parcel, plan, total_measured, floors, setback, plan_row, trace)
    out["eligibility"]["disclaimer_en"] = DISCLAIMER_EN
    out["eligibility"]["disclaimer_ar"] = DISCLAIMER_AR

    out["totals"] = {"measured_total_area_m2": total_measured, "previously_licensed_area_m2": rec.total_licensed_area,
                     "remaining_to_assess_m2": rec.total_remaining_area,
                     "setback_violation_area_m2": plan_row.get("in_setback_zone_m2"),
                     "outside_parcel_area_m2": plan_row.get("outside_parcel_m2")}
    has_blocking_issue = any(i.get("severity") == "error" for i in issues)
    out["status"] = "error" if errors and not floors else ("partial" if errors or has_blocking_issue else "ok")
    out["errors"] = errors
    out["issues"] = issues
    out["trace"] = trace.steps
    return _round(out)


FINGERPRINT_LABELS = {"boundary": "property boundary", "edge_types": "edge classifications",
                      "setbacks": "setback values", "rule": "rule version / land-use classification",
                      "crs": "units / coordinate system", "method": "setback method"}


def setback_fingerprint(inputs: CalculationInputs, distances: dict[str, float]) -> dict[str, Any]:
    """Identity of a setback construction: what a reviewer approval applies to."""
    def h(obj: Any) -> str:
        return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
    used = sorted(set(inputs.edge_types))
    parts = {"boundary": h(inputs.parcel.points if inputs.parcel else None),
             "edge_types": h(list(inputs.edge_types)),
             "setbacks": h({t: distances.get(t) for t in used}),
             "rule": h([inputs.rule_version_id, inputs.land_use_code]),
             "crs": h([inputs.units.get("unit"), inputs.units.get("crs_id"), inputs.units.get("crs_kind")]),
             "method": h([inputs.setback_method, CONSTRUCTION_VERSION])}
    return {"fingerprint": h(parts), "fingerprint_parts": parts}


def _setback_gate(inputs: CalculationInputs, setback: SetbackResult, distances: dict[str, float], frame: MetricFrame,
                  issues: list, out: dict[str, Any], fail) -> SetbackResult | None:  # type: ignore[type-arg,no-untyped-def]
    """Offset construction: area only when validated, and when every review item is approved."""
    con = setback.construction
    assert con is not None
    inv = 1.0 / frame.factor
    fp = setback_fingerprint(inputs, distances)
    out["setback_construction"] = {
        **con.to_dict(lambda p: (p[0] * inv + frame.origin_x, p[1] * inv + frame.origin_y), lambda g: _wkt(g, frame)),
        **fp, "review_keys": con.review_keys, "approval": None}
    errors = con.by_severity(ERROR)
    if errors:
        fail("setback", " ".join(i.message for i in errors), "setback_invalid")
        return None
    review = con.review_keys
    if not review:
        return setback
    appr = inputs.setback_approval or None
    if appr:
        changed = [k for k, v in fp["fingerprint_parts"].items() if (appr.get("fingerprint_parts") or {}).get(k) != v]
        missing = [k for k in review if k not in set(appr.get("issue_keys") or [])]
        if not changed and not missing:
            out["setback_construction"]["approval"] = {k: appr.get(k) for k in ("id", "approved_by", "approved_at",
                                                                               "note")}
            issues.append({"severity": "warning", "code": "setback_reviewed",
                           "message": f"{len(review)} setback construction item(s) were reviewed and accepted by "
                                      f"{appr.get('approved_by')} on {appr.get('approved_at')}: {appr.get('note')}",
                           "entity_id": ""})
            return setback
        if changed:
            issues.append({"severity": "warning", "code": "setback_approval_stale",
                           "message": "The setback approval by {by} no longer applies: the {what} changed since it "
                                      "was given.".format(by=appr.get("approved_by"),
                                                           what=", ".join(FINGERPRINT_LABELS[k] for k in changed)),
                           "entity_id": ""})
    codes = sorted({i.code for i in con.issues if i.key in review})
    fail("setback", f"{len(review)} setback construction item(s) need reviewer resolution before the buildable area "
                    f"is calculated ({', '.join(codes)}). Open Case → Setback preview.", "setback_review_required")
    return None


def _zoning_checks(inputs: CalculationInputs, issues: list, trace: Trace, out: dict[str, Any]) -> None:  # type: ignore[type-arg]
    """Compare the case classification with the zoning register overlay of the boundary."""
    z = inputs.zoning
    if not z:
        return
    out["zoning"] = z
    zones = z.get("zones", [])
    code = inputs.land_use_code
    desc = ", ".join(f"{s['zone_id']} {s['zone_name']} ({s['pct']:.1f}%, {s['area_m2']:.2f} m²)" for s in zones) or "—"
    trace.add("zoning", "Land-use zones of the boundary (zoning register)", "area(boundary ∩ zone) per zone",
              {"layer": z.get("layer"), "zones": desc, "uncovered_m2": z.get("uncovered_m2")},
              len(zones), "zones", [str(z.get("layer"))])
    if not zones:
        issues.append({"severity": "warning", "code": "zoning_none",
                       "message": "The boundary lies in no coded zone of the zoning register.", "entity_id": ""})
    elif len(zones) > 1:
        issues.append({"severity": "warning", "code": "zoning_multiple",
                       "message": f"The parcel lies in {len(zones)} zones: {desc}. This calculation applies one "
                                  f"classification ({code or '—'}) to the whole parcel; review the split.",
                       "entity_id": ""})
    if code and zones and code not in {s["zone_id"] for s in zones}:
        issues.append({"severity": "warning", "code": "zoning_mismatch",
                       "message": f"The selected classification {code} differs from the zoning register: {desc}.",
                       "entity_id": ""})
    if z.get("uncovered_m2", 0) >= 1.0:
        issues.append({"severity": "warning", "code": "zoning_gap",
                       "message": f"{z['uncovered_m2']:.2f} m² of the parcel is not covered by any zone polygon.",
                       "entity_id": ""})


def _crs_checks(inputs: CalculationInputs, pts: list, factor: float, issues: list, trace: Trace,  # type: ignore[type-arg]
                out: dict[str, Any]) -> None:
    """Check coordinates against the case CRS and record the projection scale factor.

    Results are informational: areas are grid areas in the verified planar system.
    """
    code = inputs.drawing_units.crs_id
    if not code or not pts:
        return
    from ..core.crs import CrsUnavailable, check_extents, describe, scale_factor

    try:
        info = describe(code)
    except CrsUnavailable:
        if inputs.drawing_units.crs_kind.value != "projected":
            return  # a local / site system is not looked up in the registry
        issues.append({"severity": "warning", "code": "crs_unknown",
                       "message": f"Coordinate system '{code}' is not recognised; the coordinates could not be "
                                  "checked against it. Check the code in Case details.", "entity_id": ""})
        return
    if info.kind != "projected":
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    check = check_extents(code, (min(xs), min(ys), max(xs), max(ys)), factor)
    out["crs"] = {"code": code, "name": info.name, "inside_area_of_use": check.ok}
    if not check.ok:
        issues.append({"severity": "error", "code": "crs_extent", "message": check.message, "entity_id": ""})
        return
    cx, cy = (min(xs) + max(xs)) / 2 * factor, (min(ys) + max(ys)) / 2 * factor
    k = scale_factor(code, cx / (info.unit_to_m or 1.0), cy / (info.unit_to_m or 1.0))
    if k is None:
        return
    k = round(k, 7)
    out["crs"]["scale_factor"] = k
    trace.add("crs", f"Coordinate system {info.name}", "ellipsoidal area ≈ grid area ÷ k²",
              {"code": code, "point_scale_factor_k": k}, round((1 / (k * k) - 1) * 100, 5), "% difference",
              [f"{info.name} ({code}) from the EPSG registry (PROJ)"],
              ["All areas and lengths in this calculation are grid values in the verified planar system; "
               "the projection difference shown is for information only."])


def _eligibility(rule, parcel, plan, total_measured, floors, setback, plan_row, trace) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    checks: list[dict[str, Any]] = []

    def not_evaluated(key: str, reason: str) -> None:
        checks.append({"key": key, "status": "not_evaluated", "reason": reason})

    if rule is None or parcel is None:
        reason = "No applied rule." if rule is None else "No valid parcel."
        for key in ("setbacks", "coverage", "floors", "far"):
            not_evaluated(key, reason)
        return {"checks": checks}

    if setback is None:
        not_evaluated("setbacks", "Setback geometry was not calculated.")
    else:
        v = plan_row.get("in_setback_zone_m2", 0.0)
        outside = plan_row.get("outside_parcel_m2", 0.0) or 0.0
        check = {"key": "setbacks", "status": "fail" if v > 0.01 or outside > 0.01 else "pass", "value": v,
                 "limit": 0.0, "unit": "m²", "excess": v}
        if outside > 0.01:
            check["outside_parcel_m2"] = outside
            check["reason"] = f"{outside:.2f} m² of the building lies outside the property boundary."
        checks.append(check)
    if rule.max_coverage_pct is None:
        not_evaluated("coverage", f"Rule text '{rule.coverage_raw}' is not a plain percentage." if rule.coverage_raw
                      else "Maximum coverage is not defined in the applied rule.")
    else:
        allowed = parcel.area * rule.max_coverage_pct / 100.0
        cov = plan.area
        checks.append({"key": "coverage", "status": "fail" if cov > allowed + 0.01 else "pass", "value": cov,
                       "limit": allowed, "unit": "m²", "excess": max(0.0, cov - allowed),
                       "value_pct": 100.0 * cov / parcel.area, "limit_pct": rule.max_coverage_pct,
                       "assumption": "Coverage measured as the plan projection of all floors."})
        trace.add("coverage", "Building coverage check", "coverage = area(plan) ÷ parcel area",
                  {"plan_m2": cov, "parcel_m2": parcel.area, "max_pct": rule.max_coverage_pct},
                  100.0 * cov / parcel.area, "%", warnings=["Plan projection of all floors is used as coverage."])
    if rule.max_floors is None:
        not_evaluated("floors", f"Rule text '{rule.floors_raw}' is not a plain number of floors." if rule.floors_raw
                      else "Maximum number of floors is not defined in the applied rule.")
    else:
        n = len(floors)
        checks.append({"key": "floors", "status": "fail" if n > rule.max_floors else "pass", "value": n,
                       "limit": rule.max_floors, "unit": "floors", "excess": max(0, n - rule.max_floors),
                       "assumption": "Every distinct floor key in the drawing is counted (review basements/roofs)."})
    if rule.max_far is None:
        not_evaluated("far", f"Rule text '{rule.far_raw}' is not a plain number." if rule.far_raw
                      else "Maximum floor-area ratio is not defined in the applied rule.")
    else:
        allowed = parcel.area * rule.max_far
        checks.append({"key": "far", "status": "fail" if total_measured > allowed + 0.01 else "pass",
                       "value": total_measured, "limit": allowed, "unit": "m²",
                       "excess": max(0.0, total_measured - allowed), "value_ratio": total_measured / parcel.area,
                       "limit_ratio": rule.max_far})
    return {"checks": checks}
