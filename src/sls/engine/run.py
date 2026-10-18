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
from ..geometry.setbacks import SetbackResult, compute_setbacks, suggest_edge_types
from ..licensing.reconcile import reconcile
from .inputs import CalculationInputs

ENGINE_VERSION = "1.0.0"
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
            trace.add("parcel_area", "Parcel area", "A = ½|Σ(xᵢ·yᵢ₊₁ − xᵢ₊₁·yᵢ)|",
                      {"vertices": len(parcel.exterior.coords) - 1}, parcel.area, "m²", [inputs.parcel.entity_id])
            if not inputs.parcel_verified:
                fail("parcel", "The property boundary has not been verified by a user; setbacks are not "
                               "calculated from an unverified boundary.", "boundary_not_verified")

    # 3. Setbacks ----------------------------------------------------------------------
    setback: SetbackResult | None = None
    rule = inputs.rule_obj
    out["rule"] = inputs.rule
    if rule is None:
        fail("setback", f"No rule is applied for land-use classification '{inputs.land_use_code}'.",
             "missing_rule")
    if not inputs.rule_version_verified and rule is not None:
        issues.append({"severity": "warning", "code": "rule_not_verified",
                       "message": "The applied rule version has not been verified by a reviewer.", "entity_id": ""})
    if parcel is not None and rule is not None and inputs.parcel_verified:
        try:
            setback = compute_setbacks(parcel, inputs.edge_types, rule.setbacks, inputs.sliver_tolerance_m2)
        except SlsError as exc:
            fail("setback", exc)
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
        trace.add("setback", "Buildable area after setbacks",
                  "Buildable = Parcel − ⋃ᵢ { p : dist(p, edgeᵢ) < dᵢ }",
                  {"distances_m": rule.setbacks if rule else {}, "edge_types": inputs.edge_types,
                   "rule_code": rule.code if rule else ""}, setback.buildable_area, "m²",
                  [f"rule version {inputs.rule_version_id}", f"rule row {rule.source_row}" if rule else ""],
                  setback.warnings)

    # 4. Floor areas -------------------------------------------------------------------
    fps: list[Footprint] = []
    for s in inputs.footprints:
        poly, f_issues = build_polygon(frame.to_local(s.points), s.closed, s.entity_id)
        issues += [i.to_dict() for i in f_issues]
        if poly is not None:
            fps.append(Footprint(s.entity_id, s.floor_key, poly))
    floors = floor_areas(fps, inputs.floor_order)
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
            if setback is not None:
                row["in_setback_zone_m2"] = inter.in_setback_zone_m2
                row["violation_wkt"] = _wkt(inter.in_setback_zone, frame)
        floor_rows.append(row)
        trace.add(f"floor_area:{fa.floor_key}", f"Measured area, floor {fa.floor_key}",
                  "A_floor = area(⋃ outlines on floor)", {"outlines": len(fa.entity_ids),
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
                      "area((⋃ all floors) ∩ setback zone)", {}, float(viol.area), "m²",
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
        trace.add(f"length:{t.kind}", f"Measured {t.kind} length", "L = length(⋃ segments)",
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
        checks.append({"key": "setbacks", "status": "fail" if v > 0.01 else "pass", "value": v, "limit": 0.0,
                       "unit": "m²", "excess": v})
    if rule.max_coverage_pct is None:
        not_evaluated("coverage", "Maximum coverage is not defined in the applied rule.")
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
        not_evaluated("floors", "Maximum number of floors is not defined in the applied rule.")
    else:
        n = len(floors)
        checks.append({"key": "floors", "status": "fail" if n > rule.max_floors else "pass", "value": n,
                       "limit": rule.max_floors, "unit": "floors", "excess": max(0, n - rule.max_floors),
                       "assumption": "Every distinct floor key in the drawing is counted (review basements/roofs)."})
    if rule.max_far is None:
        not_evaluated("far", "Maximum floor-area ratio is not defined in the applied rule.")
    else:
        allowed = parcel.area * rule.max_far
        checks.append({"key": "far", "status": "fail" if total_measured > allowed + 0.01 else "pass",
                       "value": total_measured, "limit": allowed, "unit": "m²",
                       "excess": max(0.0, total_measured - allowed), "value_ratio": total_measured / parcel.area,
                       "limit_ratio": rule.max_far})
    return {"checks": checks}
