"""Calculation inputs: a self-contained, serialisable snapshot.

Inputs are extracted from the drawing, the case's parcel record, the applied rule
version and the license register. The snapshot is stored with each calculation so the
result can be reproduced byte-for-byte later, independent of subsequent edits.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..cad.model import Drawing, Entity
from ..core.units import DrawingUnits
from ..licensing.reconcile import LicenseRecordInput
from ..rules.excel_rules import LandUseRule

SEGMENTS_PER_CIRCLE = 256


@dataclass
class ShapeInput:
    entity_id: str
    points: list[list[float]]
    closed: bool
    layer: str
    floor_key: str = ""
    kind: str = ""          # wall | fence for linear features
    approximated: bool = False


@dataclass
class CalculationInputs:
    units: dict[str, Any]
    parcel: ShapeInput | None
    parcel_verified: bool
    edge_types: list[str]
    rule: dict[str, Any] | None
    rule_version_id: str | None
    rule_version_verified: bool
    land_use_code: str
    footprints: list[ShapeInput]
    linear: list[ShapeInput]
    roads: list[ShapeInput]
    floor_order: list[str]
    licenses: list[dict[str, Any]]
    drawing_revision_id: str | None
    drawing_content_sha256: str
    sliver_tolerance_m2: float = 0.01
    extraction_issues: list[dict[str, str]] = field(default_factory=list)
    # Reviewer-entered values for boundary types whose rule text is conditional (see rules_repo).
    rule_override: dict[str, Any] | None = None
    # Zone overlay of the boundary from the zoning register (snapshot, see zoning_repo).
    zoning: dict[str, Any] | None = None
    # Setback algorithm (geometry.setbacks). Snapshots stored before this field existed used the
    # distance-zone method, so that is the default when a snapshot is loaded.
    setback_method: str = "distance_zone"
    # Reviewer approval of the offset construction (setback_approvals); matched by fingerprint.
    setback_approval: dict[str, Any] | None = None
    # Dataset versions this calculation used (rules, parcel register, zoning, roads); see datasets.py.
    datasets: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalculationInputs":
        d = dict(d)
        d["parcel"] = ShapeInput(**d["parcel"]) if d.get("parcel") else None
        for key in ("footprints", "linear", "roads"):
            d[key] = [ShapeInput(**s) for s in d.get(key, [])]
        return cls(**d)

    def canonical_json(self) -> str:
        d = self.to_dict()
        if d.get("setback_method") == "distance_zone":
            del d["setback_method"]  # keeps the hashes of snapshots stored before the field existed
        for key in ("setback_approval", "datasets"):
            if d.get(key) is None:
                d.pop(key, None)  # same reason
        return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def input_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def content_key(self) -> str:
        """Hash of what the calculation depends on, without the drawing revision id.

        Used to decide whether a stored calculation is out of date: saving an unchanged drawing
        (e.g. after adding generated layers, which are ignored as inputs) creates a new revision but
        does not change the inputs (``drawing_content_sha256`` covers the content).
        """
        d = json.loads(self.canonical_json())
        d.pop("drawing_revision_id", None)
        return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @property
    def drawing_units(self) -> DrawingUnits:
        return DrawingUnits.from_dict(self.units)

    @property
    def rule_obj(self) -> LandUseRule | None:
        return LandUseRule.from_dict(self.rule) if self.rule else None

    @property
    def license_inputs(self) -> list[LicenseRecordInput]:
        return [LicenseRecordInput.from_dict(x) for x in self.licenses]


def _shape(e: Entity, **extra: Any) -> ShapeInput:
    if e.kind == "circle":
        pts = e.flattened(SEGMENTS_PER_CIRCLE)[:-1]
        closed = True
    elif e.kind == "polyline":
        pts = e.flattened(SEGMENTS_PER_CIRCLE)
        closed = bool(e.data.get("closed"))
        if closed and len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
    else:
        pts = e.flattened(SEGMENTS_PER_CIRCLE)
        closed = False
    return ShapeInput(e.id, [[float(x), float(y)] for x, y in pts], closed, e.layer,
                      approximated=e.has_curves() or bool(e.meta.get("approximated_from")), **extra)


def _tolerance(units: DrawingUnits) -> float:
    """1 mm in drawing units when the units are known and metric-convertible, else exact coincidence only."""
    try:
        return 0.001 / units.require_metric_planar()
    except Exception:  # noqa: BLE001 - unverified/unknown units: no tolerance is assumed
        return 0.0


def _parcel_by_logical_edges(ent: Entity, edge_types: list[str], tol: float) -> tuple[ShapeInput, list[str]]:
    """Boundary points with each logical edge's classification copied onto its arc segments."""
    from ..cad.model import flatten_bulge_polyline, logical_boundary

    norm = logical_boundary(ent.data["points"], True, tol)
    if len(norm) < 3 or len(edge_types) != len(norm):
        return _shape(ent), edge_types  # the engine reports the mismatch / degenerate boundary
    pts: list[list[float]] = []
    types: list[str] = []
    n = len(norm)
    for i in range(n):
        a, b = norm[i], norm[(i + 1) % n]
        seg = flatten_bulge_polyline([a, (b[0], b[1])], False, SEGMENTS_PER_CIRCLE) if abs(a[2]) > 1e-12 \
            else [(a[0], a[1]), (b[0], b[1])]
        for q in seg[:-1]:
            pts.append([float(q[0]), float(q[1])])
            types.append(edge_types[i])
    return ShapeInput(ent.id, pts, True, ent.layer, approximated=ent.has_curves()), types


def extract_inputs(drawing: Drawing, *, boundary_entity_id: str, parcel_verified: bool,
                   edge_types: list[str], rule: LandUseRule | None, rule_version_id: str | None,
                   rule_version_verified: bool, land_use_code: str, licenses: list[LicenseRecordInput],
                   units: DrawingUnits, floor_order: list[str] | None = None,
                   drawing_revision_id: str | None = None, sliver_tolerance_m2: float = 0.01,
                   rule_override: dict[str, Any] | None = None) -> CalculationInputs:
    issues: list[dict[str, str]] = []
    parcel: ShapeInput | None = None
    if boundary_entity_id:
        ent = drawing.entities.get(boundary_entity_id)
        if ent is None:
            issues.append({"severity": "error", "code": "boundary_missing",
                           "message": "The selected property boundary entity no longer exists in the drawing.",
                           "entity_id": boundary_entity_id})
        elif ent.kind not in ("polyline", "circle"):
            issues.append({"severity": "error", "code": "boundary_kind",
                           "message": f"Property boundary must be a closed polyline, not {ent.kind}.",
                           "entity_id": ent.id})
        elif ent.kind == "polyline" and ent.data.get("closed"):
            parcel, edge_types = _parcel_by_logical_edges(ent, list(edge_types), _tolerance(units))
        else:
            parcel = _shape(ent)

    layer_by_name = {layer.name: layer for layer in drawing.layers}
    footprints: list[ShapeInput] = []
    linear: list[ShapeInput] = []
    roads: list[ShapeInput] = []
    for e in drawing.entities.values():
        layer = layer_by_name.get(e.layer)
        if layer is None or e.meta.get("source") == "derived":
            continue
        if layer.role == "building":
            if e.kind in ("text", "point", "dimension"):
                continue
            floor = str(e.meta.get("floor") or layer.floor_key or "").strip()
            if not floor:
                issues.append({"severity": "error", "code": "floor_missing",
                               "message": f"Building entity on layer '{e.layer}' has no floor assigned "
                                          "(set the layer floor or the entity floor).", "entity_id": e.id})
                continue
            if not e.is_closed_shape():
                pts = e.data.get("points", []) if e.kind == "polyline" else []
                tol = _tolerance(units)
                if len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= tol:
                    # first and last vertex coincide (within 1 mm): closed in geometry, flag not set
                    shp = _shape(e, floor_key=floor)
                    footprints.append(replace(shp, points=shp.points[:-1], closed=True))
                    issues.append({"severity": "warning", "code": "closed_by_coincident_ends",
                                   "message": f"Building outline on layer '{e.layer}' is not flagged closed but its "
                                              "first and last vertices coincide; it is treated as closed.",
                                   "entity_id": e.id})
                    continue
                issues.append({"severity": "error", "code": "open_footprint",
                               "message": f"Building outline on layer '{e.layer}' is not closed; it is "
                                          "excluded from area totals.", "entity_id": e.id})
                continue
            footprints.append(_shape(e, floor_key=floor))
        elif layer.role in ("wall", "fence") and e.kind in ("line", "polyline", "arc", "circle"):
            linear.append(_shape(e, kind=layer.role))
        elif layer.role in ("wall", "fence") and e.kind not in ("text", "point", "dimension"):
            issues.append({"severity": "warning", "code": "linear_kind_ignored",
                           "message": f"{e.kind} on {layer.role} layer '{e.layer}' is not counted in lengths.",
                           "entity_id": e.id})
        elif layer.role == "road" and e.kind in ("line", "polyline"):
            roads.append(_shape(e))

    return CalculationInputs(
        units=units.to_dict(), parcel=parcel, parcel_verified=parcel_verified, edge_types=list(edge_types),
        rule=rule.to_dict() if rule else None, rule_version_id=rule_version_id,
        rule_version_verified=rule_version_verified, land_use_code=land_use_code,
        footprints=footprints, linear=linear, roads=roads, floor_order=list(floor_order or []),
        licenses=[lic.to_dict() for lic in licenses], drawing_revision_id=drawing_revision_id,
        drawing_content_sha256=drawing.source_content_hash(), sliver_tolerance_m2=sliver_tolerance_m2,
        extraction_issues=issues, rule_override=rule_override,
    )
