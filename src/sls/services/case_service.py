"""Workflow orchestration for a licensing case (UI-independent).

Ties the persistence layer to the CAD model, the calculation engine and reporting, so the
same workflow can be driven by the desktop UI, by tests, or by a script.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry

from ..cad.dxf_io import ImportReport, import_dxf
from ..cad.model import Drawing, Entity
from ..core.errors import CadImportError, ValidationError
from ..core.units import LengthUnit
from ..engine.inputs import CalculationInputs, extract_inputs
from ..engine.run import run_calculation
from ..persistence.calculations_repo import CalculationRecord
from ..persistence.drawings_repo import DrawingRecord, RevisionRecord
from ..persistence.workspace import Workspace
from ..rules.excel_rules import LandUseRule
from ..core.errors import SlsError

CADASTRE_LAYERS = {
    "PROPERTY_BOUNDARY": {"role": "property_boundary", "color": "#4FC3F7", "lineweight": 0.5},
    "CADASTRE_PARCELS": {"role": "neighbor_parcel", "color": "#78909C"},
    "CADASTRE_LABELS": {"role": "annotation", "color": "#B0BEC5"},
}

DERIVED_LAYERS = {
    "setback": "SETBACK_BOUNDARY",
    "violation": "SETBACK_VIOLATIONS",
}
# Layers written when a setback construction is accepted in the preview (one per construction step).
CONSTRUCTION_LAYERS = {
    "offset": ("SETBACK_OFFSETS", "#4FC3F7"),       # raw OFFSET of each edge (same length as the edge)
    "joined": ("SETBACK_TRIM_EXTEND", "#80DEEA"),   # each offset trimmed/extended to its neighbours
    "trimmed": ("SETBACK_TRIMMED", "#EF5350"),      # portions removed by trimming
    "extended": ("SETBACK_EXTENDED", "#FFD54F"),    # portions added by extending
    "corner": ("SETBACK_CORNERS", "#F06292"),       # join points at each vertex
    "discarded": ("SETBACK_DISCARDED", "#9E9E9E"),  # faces removed from the joined ring, with reason
    "buildable": ("SETBACK_BUILDABLE", "#66BB6A"),  # final validated buildable area
}


@dataclass
class LoadedDrawing:
    record: DrawingRecord
    revision: RevisionRecord
    drawing: Drawing


class CaseService:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    # -- drawings ---------------------------------------------------------------------

    def preview_import(self, path: Path, case_id: str | None = None) -> tuple[Drawing, ImportReport]:
        suffix = Path(path).suffix.lower()
        if suffix == ".dxf":
            drawing, report = import_dxf(path)
        elif suffix == ".dwg":
            from ..cad.dwg import import_dwg

            drawing, report = import_dwg(path, self.ws.settings.oda_converter_path, self.ws.settings.libredwg_path)
        else:
            raise CadImportError(f"Unsupported drawing format: {suffix or '(none)'}")
        crs_code = self.ws.cases.get(case_id).units.crs_id if case_id else ""
        self.check_drawing_crs(drawing, report, crs_code or self.ws.settings.default_crs)
        return drawing, report

    @staticmethod
    def entities_in_window(drawing: Drawing, window: tuple[float, float, float, float]) -> list[Entity]:
        """Entities whose bounding box intersects the window (used to take only the part of a large
        regulatory plan that surrounds the survey). Geometry is copied unchanged."""
        from ..cad.ops import fast_bbox

        x0, y0, x1, y1 = window
        out = []
        for e in drawing.entities.values():
            try:
                b = fast_bbox(e)
            except (KeyError, ValueError, IndexError):
                continue
            if b[2] >= x0 and b[0] <= x1 and b[3] >= y0 and b[1] <= y1:
                out.append(e)
        return out

    @staticmethod
    def check_drawing_crs(drawing: Drawing, report: ImportReport, crs_code: str) -> None:
        """Check the drawing's coordinates against the case's coordinate system.

        If they fall inside the system's area of use when read in its own unit (metres for
        JTM), that unit is *suggested* even when the file header declares something else
        (GIS exports often declare inches). Nothing is moved and nothing is verified here.
        """
        from dataclasses import replace

        from ..core.crs import CrsUnavailable, check_extents, describe

        ext = drawing.extents(only_visible=False)
        if not crs_code or ext is None:
            return
        try:
            info = describe(crs_code)
        except CrsUnavailable:
            report.crs_code, report.crs_message = crs_code, f"{crs_code} is not in the PROJ database; not checked."
            return
        if info.kind != "projected" or info.unit != "metre":
            return
        check = check_extents(crs_code, ext, 1.0)
        report.crs_code, report.crs_ok, report.crs_message = crs_code, check.ok, check.message
        declared = drawing.units.unit
        if check.ok:
            if declared is not LengthUnit.METER:
                report.warnings.insert(0, f"The file header declares '{declared.value}', but the coordinates match "
                                          f"{info.name} in metres. Metres are suggested (verify in Case details).")
            drawing.units = replace(drawing.units, unit=LengthUnit.METER, verified=False, crs_id=crs_code)
        else:
            report.warnings.insert(0, check.message)

    def import_drawing(self, case_id: str, path: Path, role: str = "survey", drawing: Drawing | None = None,
                       report: ImportReport | None = None, keep_source_copy: bool = True,
                       add_default_layers: bool = True) -> LoadedDrawing:
        user = self.ws.user
        if drawing is None or report is None:
            drawing, report = self.preview_import(path)
        if add_default_layers:
            # Standard layers are added for classification; imported layers are kept as-is.
            template = Drawing.with_default_layers()
            for layer in template.layers:
                if not drawing.has_layer(layer.name):
                    drawing.add_layer(layer)
        case = self.ws.cases.get(case_id)
        if role == "survey" and drawing.units.unit is not LengthUnit.UNKNOWN and (
                case.units.unit is LengthUnit.UNKNOWN or (not case.units.verified and case.units.unit != drawing.units.unit)):
            # Suggested units are copied but remain UNVERIFIED until a user confirms them.
            from dataclasses import replace

            case.units = replace(case.units, unit=drawing.units.unit, verified=False)
            self.ws.cases.save(user, case)
        name = Path(path).name
        rec = self.ws.drawings.create(user, case_id, role, name, drawing,
                                      Path(path) if keep_source_copy else None, report.to_dict())
        return self.load_drawing(rec.id)

    def load_drawing(self, drawing_id: str, revision_id: str | None = None) -> LoadedDrawing:
        rec = self.ws.drawings.get(drawing_id)
        rev = self.ws.drawings.revision(revision_id) if revision_id else self.ws.drawings.latest_revision(drawing_id)
        if rev is None:
            raise ValidationError("Drawing has no saved revision.")
        return LoadedDrawing(rec, rev, self.ws.drawings.load(rev.id))

    def survey_drawing(self, case_id: str) -> LoadedDrawing | None:
        drawings = self.ws.drawings.for_case(case_id, "survey")
        return self.load_drawing(drawings[-1].id) if drawings else None

    def new_blank_drawing(self, case_id: str) -> LoadedDrawing:
        case = self.ws.cases.get(case_id)
        rec = self.ws.drawings.create(self.ws.user, case_id, "survey", "Untitled drawing",
                                      Drawing.with_default_layers(case.units))
        return self.load_drawing(rec.id)

    def apply_transformation(self, drawing_id: str, src: list[tuple[float, float]], dst: list[tuple[float, float]],
                             kind: str) -> LoadedDrawing:
        """Fit and apply a control-point transformation as a NEW revision of the given drawing."""
        from ..cad.transform import fit, transform_drawing
        from ..persistence.db import now_iso

        loaded = self.load_drawing(drawing_id)
        result = fit(src, dst, kind)
        out = transform_drawing(loaded.drawing, result.transform)
        record = {**result.transform.to_dict(), "rms": result.rms, "residuals": result.residuals,
                  "control_points": [{"source": list(a), "target": list(b)} for a, b in zip(src, dst)],
                  "applied_by": self.ws.user.username, "applied_at": now_iso(),
                  "from_revision": loaded.revision.revision}
        out.info.setdefault("transforms", []).append(record)
        rev = self.ws.drawings.save_revision(self.ws.user, drawing_id, out,
                                             f"Control-point {kind} transformation (RMS {result.rms:.4f})")
        self.ws.audit.record(self.ws.user, "transform_drawing", "drawing", drawing_id, loaded.record.case_id,
                             **{k: v for k, v in record.items() if k != "residuals"})
        return LoadedDrawing(loaded.record, rev, out)

    # -- zoning register -------------------------------------------------------------------------

    def zoning_for_boundary(self, drawing: Drawing, boundary_entity_id: str, crs_code: str = "") -> dict[str, Any] | None:
        """Zone overlay of the boundary polygon.

        Only when the case grid is the zoning layer's grid (e.g. ESRI:102158 / EPSG:3066); a layer
        imported without a .prj is assumed to be in the case grid.
        """
        from shapely.geometry import Polygon

        from ..cad.model import flatten_bulge_polyline
        from ..core.crs import same_grid

        layer = self.ws.zoning.active_layer()
        e = drawing.entities.get(boundary_entity_id) if boundary_entity_id else None
        if layer is None or e is None or e.kind != "polyline" or not e.data.get("closed"):
            return None
        if layer["crs_code"] and not same_grid(crs_code, layer["crs_code"]):
            return None
        poly = Polygon(flatten_bulge_polyline(e.data["points"], True, 128))
        if not poly.is_valid or poly.area <= 0:
            return None
        ov = self.ws.zoning.overlay(poly, layer["id"])
        return {"layer": layer["label"], "layer_id": layer["id"], **ov.to_dict()} if ov else None

    # -- cadastral parcel register -------------------------------------------------------------

    def cadastre_entities(self, case_id: str, parcel_ids: list[int], neighbours_margin: float = 30.0
                          ) -> tuple[list[Entity], list[str], list[str]]:
        """Entities for the chosen register parcel(s) plus surrounding parcels with their numbers.

        Returns (entities, boundary entity ids, warnings). Geometry is copied unchanged; nothing
        is verified. A single-part, valid parcel gives exactly one boundary candidate.
        """
        from ..core.crs import same_grid

        case = self.ws.cases.get(case_id)
        parcels = [self.ws.cadastre.get(i) for i in parcel_ids]
        if not parcels:
            raise ValidationError("No parcel selected.")
        layer = self.ws.cadastre.layer(parcels[0].layer_id)
        warnings: list[str] = []
        if layer.crs_code and case.units.crs_id and not same_grid(layer.crs_code, case.units.crs_id):
            raise ValidationError(f"The parcel layer is in {layer.crs_code} but the case uses {case.units.crs_id}; "
                                  "the coordinates are not compatible. Change the case coordinate system or "
                                  "use a layer in the same system.")
        meta_base = {"source": "cadastre", "cadastre_layer": layer.label, "cadastre_layer_id": layer.id}
        entities: list[Entity] = []
        boundary_ids: list[str] = []
        chosen_fids = {p.fid for p in parcels}
        for p in parcels:
            if not p.valid:
                warnings.append(f"Register parcel {p.dls_key or p.label} has a geometry problem: {p.issue}.")
            if p.geometry is None:
                continue
            polys = [p.geometry] if p.geometry.geom_type == "Polygon" else list(getattr(p.geometry, "geoms", []))
            if len(polys) > 1:
                warnings.append(f"Register parcel {p.dls_key or p.label} has {len(polys)} separate parts; choose the "
                                "part to use as the property boundary before verifying.")
            meta = {**meta_base, "dls_key": p.dls_key, "parcel_number": p.parcel_no, "village": p.village,
                    "basin": p.basin, "sector": p.sector, "sheet": p.sheet, "globalid": p.globalid,
                    "register_area_m2": round(p.area_m2 or 0.0, 3)}
            for part in polys:
                ring = [[x, y] for x, y in list(part.exterior.coords)[:-1]]
                e = Entity("polyline", "PROPERTY_BOUNDARY", {"points": ring, "closed": True}, meta=dict(meta))
                entities.append(e)
                boundary_ids.append(e.id)
                for hole in part.interiors:
                    entities.append(Entity("polyline", "PROPERTY_BOUNDARY",
                                           {"points": [[x, y] for x, y in list(hole.coords)[:-1]], "closed": True},
                                           meta={**meta, "hole": True}))
                    warnings.append(f"Register parcel {p.dls_key or p.label} contains a hole (inner ring).")
        if neighbours_margin > 0:
            x0, y0, x1, y1 = parcels[0].geometry.bounds if parcels[0].geometry is not None else (0, 0, 0, 0)
            for n in self.ws.cadastre.in_window(x0 - neighbours_margin, y0 - neighbours_margin,
                                                x1 + neighbours_margin, y1 + neighbours_margin, layer.id):
                if n.fid in chosen_fids or n.geometry is None:
                    continue
                meta = {**meta_base, "dls_key": n.dls_key, "parcel_number": n.parcel_no}
                for part in ([n.geometry] if n.geometry.geom_type == "Polygon" else list(n.geometry.geoms)):
                    entities.append(Entity("polyline", "CADASTRE_PARCELS",
                                           {"points": [[x, y] for x, y in list(part.exterior.coords)[:-1]],
                                            "closed": True}, meta=dict(meta)))
                if n.parcel_no:
                    c = n.geometry.representative_point()
                    entities.append(Entity("text", "CADASTRE_LABELS", {"position": [c.x, c.y], "text": n.parcel_no,
                                                                       "height": 1.5, "rotation": 0.0},
                                           meta=dict(meta)))
        return entities, boundary_ids, warnings

    def link_case_to_parcel(self, case_id: str, parcel_id: int, boundary_entity_id: str | None,
                            boundary_points: list[list[float]] | None) -> None:
        """Fill the case's administrative fields from the register and set the boundary candidate."""
        p = self.ws.cadastre.get(parcel_id)
        case = self.ws.cases.get(case_id)
        known = {fd.key for fd in self.ws.cases.field_defs()}
        updates = {"village": p.village, "basin": p.basin, "neighbourhood": p.sector, "plot_number": p.parcel_no,
                   "sheet_no": p.sheet, "dls_key": p.dls_key}
        # Land use from the zoning register: applied only when the parcel lies in exactly one zone,
        # the case has no classification yet, and the code exists in the applied rule version.
        ov = self.ws.zoning.overlay(p.geometry) if p.geometry is not None else None
        if ov is not None and ov.single_zone and not case.land_use_code and case.rule_version_id:
            codes = {r.code for r in self.ws.rules.rules(case.rule_version_id)}
            if ov.single_zone.zone_id in codes:
                case.land_use_code = ov.single_zone.zone_id
        case.fields.update({k: v for k, v in updates.items() if k in known and v})
        if p.dls_key:
            case.parcel_identifier = p.dls_key
        self.ws.cases.save(self.ws.user, case)
        if boundary_entity_id:
            parcel = self.ws.drawings.parcel(case_id)
            parcel.boundary_entity_id = boundary_entity_id
            parcel.boundary_points = boundary_points or []
            parcel.identifier = p.dls_key or p.label
            parcel.edge_types = []
            parcel.verified = False
            self.ws.drawings.save_parcel(self.ws.user, parcel)

    # -- rules ---------------------------------------------------------------------------

    def applied_rule(self, case_id: str) -> tuple[LandUseRule | None, str | None, bool, str]:
        """Return (rule, version_id, version_verified, error message)."""
        case = self.ws.cases.get(case_id)
        if not case.rule_version_id:
            return None, None, False, "No rule version has been applied to this case."
        version = self.ws.rules.get_version(case.rule_version_id)
        try:
            return self.ws.rules.rule(version.id, case.land_use_code), version.id, version.verified, ""
        except SlsError as exc:
            return None, version.id, version.verified, exc.message

    # -- calculation -----------------------------------------------------------------------

    def build_inputs(self, case_id: str, floor_order: list[str] | None = None) -> CalculationInputs:
        case = self.ws.cases.get(case_id)
        loaded = self.survey_drawing(case_id)
        if loaded is None:
            raise ValidationError("Import or create a survey drawing before calculating.")
        parcel = self.ws.drawings.parcel(case_id)
        rule, version_id, version_verified, _ = self.applied_rule(case_id)
        boundary = loaded.drawing.entities.get(parcel.boundary_entity_id) if parcel.boundary_entity_id else None
        current_pts = [[p[0], p[1]] for p in boundary.data.get("points", [])] if boundary is not None and \
            boundary.kind == "polyline" else []
        verified = parcel.verified_against(current_pts)
        inputs = extract_inputs(
            loaded.drawing, boundary_entity_id=parcel.boundary_entity_id, parcel_verified=verified,
            edge_types=parcel.edge_types, rule=rule, rule_version_id=version_id,
            rule_version_verified=version_verified, land_use_code=case.land_use_code,
            licenses=self.ws.licenses.inputs(case_id), units=case.units,
            floor_order=floor_order or self.floor_order(loaded.drawing),
            drawing_revision_id=loaded.revision.id, sliver_tolerance_m2=self.ws.settings.sliver_area_tolerance_m2,
            rule_override=(ov.to_dict() if (ov := self.ws.rules.case_override(case_id, version_id, case.land_use_code))
                           else None),
        )
        inputs.zoning = self.zoning_for_boundary(loaded.drawing, parcel.boundary_entity_id, case.units.crs_id)
        inputs.setback_method = self.ws.settings.setback_method
        if inputs.setback_method == "offset_corner":
            inputs.setback_approval = self.ws.setback_approvals.for_inputs(case_id)
        if parcel.verified and not verified:
            inputs.extraction_issues.append({
                "severity": "error", "code": "boundary_changed",
                "message": "The property boundary changed after it was verified; verify it again.", "entity_id":
                parcel.boundary_entity_id})
        return inputs

    # -- setback construction preview / approval ------------------------------------------------

    def setback_preview(self, case_id: str) -> dict[str, Any]:
        """Run the setback construction without storing anything (the drawing is not modified)."""
        inputs = self.build_inputs(case_id)
        if inputs.setback_method != "offset_corner":
            raise ValidationError("The setback preview needs the surveyor offset method (Settings → Setback method).")
        results = run_calculation(inputs)
        return {"construction": results.get("setback_construction"), "errors": results.get("errors", []),
                "parcel_wkt": (results.get("parcel") or {}).get("wkt", ""),
                "issues": results.get("issues", []), "setback": results.get("setback"),
                "approval": self.ws.setback_approvals.latest(case_id), "rule_version_id": inputs.rule_version_id}

    def approve_setback(self, case_id: str, note: str, fingerprint: str) -> dict[str, Any]:
        """Accept the previewed construction: store the approval and its layers as a new drawing revision.

        ``fingerprint`` is the one shown in the preview; if the case changed meanwhile, nothing is stored.
        """
        from ..persistence.db import new_id

        user = self.ws.user
        preview = self.setback_preview(case_id)
        con = preview["construction"]
        if con is None:
            blocking = "; ".join(e["message"] for e in preview["errors"]) or "no setback construction"
            raise ValidationError(f"There is no setback construction to approve: {blocking}")
        if con["fingerprint"] != fingerprint:
            raise ValidationError("The case changed while the preview was open. Reopen the setback preview.")
        errors = [i["message"] for i in con["issues"] if i["severity"] == "error"]
        if errors:
            raise ValidationError("Invalid setback geometry cannot be approved: " + " ".join(errors))
        if con["review_keys"]:
            user.require("approve_setback")
            if not note.strip():
                raise ValidationError("Enter a note explaining the resolution of the items that need review.")
        else:
            user.require("run_calculation")
        loaded = self.survey_drawing(case_id)
        if loaded is None:
            raise ValidationError("The case has no survey drawing.")
        approval_id = new_id()
        _add_construction(loaded.drawing, con, approval_id)
        rev = self.ws.drawings.save_revision(user, loaded.record.id, loaded.drawing, "setback construction approved")
        return self.ws.setback_approvals.create(user, case_id, con, "offset_corner", note.strip(),
                                                preview["rule_version_id"], loaded.record.id, rev.id, approval_id)

    def run_calculation(self, case_id: str, floor_order: list[str] | None = None) -> CalculationRecord:
        inputs = self.build_inputs(case_id, floor_order)
        results = run_calculation(inputs)
        return self.ws.calculations.save(self.ws.user, case_id, inputs, results)

    @staticmethod
    def floor_order(drawing: Drawing) -> list[str]:
        order: list[str] = []
        for layer in drawing.layers:
            if layer.role == "building" and layer.floor_key and layer.floor_key not in order:
                order.append(layer.floor_key)
        return order

    @staticmethod
    def apply_derived_geometry(drawing: Drawing, results: dict[str, Any], calc_id: str) -> int:
        """Replace generated setback/violation entities with those from ``results``.

        Generated entities carry ``meta.source = "derived"`` and the calculation ID, so
        they stay distinguishable from surveyed geometry and are ignored as inputs.
        """
        for eid in [e.id for e in drawing.entities.values() if e.meta.get("source") == "derived"
                    and e.meta.get("derived_kind") in ("buildable_limit", "setback_violation")]:
            del drawing.entities[eid]
        for key, name in DERIVED_LAYERS.items():
            drawing.ensure_layer(name, role="setback" if key == "setback" else "setback_violation", derived=True,
                                 color="#BA68C8" if key == "setback" else "#EF5350")
        added = 0
        sb = results.get("setback") or {}
        added += _add_polys(drawing, sb.get("buildable_wkt", ""), DERIVED_LAYERS["setback"],
                            {"source": "derived", "calculation_id": calc_id, "derived_kind": "buildable_limit"})
        plan = results.get("plan_projection") or {}
        added += _add_polys(drawing, plan.get("violation_wkt", ""), DERIVED_LAYERS["violation"],
                            {"source": "derived", "calculation_id": calc_id, "derived_kind": "setback_violation"})
        return added


def _add_construction(drawing: Drawing, con: dict[str, Any], approval_id: str) -> int:
    """Replace the setback-construction layers' entities with those of ``con`` (drawing units)."""
    for eid in [e.id for e in drawing.entities.values() if e.meta.get("derived_kind") == "setback_construction"]:
        del drawing.entities[eid]
    for name, color in CONSTRUCTION_LAYERS.values():
        drawing.ensure_layer(name, role="setback", derived=True, color=color)
    base = {"source": "derived", "derived_kind": "setback_construction", "approval_id": approval_id}
    n = 0

    def line(key: str, seg: list[list[float]], **meta: Any) -> None:
        nonlocal n
        drawing.add(Entity("line", CONSTRUCTION_LAYERS[key][0], {"p1": list(seg[0]), "p2": list(seg[1])},
                           meta={**base, "step": key, **meta}))
        n += 1

    for e in con["edges"]:
        info = {"edge": e["index"], "boundary_type": e["boundary_type"], "setback_m": e["setback_m"],
                "outcome": e["outcome"]}
        line("offset", e["raw_offset"], **info)
        if not e["collapsed"]:
            line("joined", e["trimmed_extended"], **info)
        for seg in e["trimmed"]:
            line("trimmed", seg, **info)
        for seg in e["extended"]:
            line("extended", seg, **info)
    for j in con["joins"]:
        for q in j["points"]:
            drawing.add(Entity("point", CONSTRUCTION_LAYERS["corner"][0], {"position": list(q)},
                               meta={**base, "step": "corner", "vertex": j["vertex"], "join": j["kind"]}))
            n += 1
    for f in con["discarded"]:
        n += _add_polys(drawing, f["wkt"], CONSTRUCTION_LAYERS["discarded"][0],
                        {**base, "step": "discarded", "reason": f["reason"], "area_m2": f["area_m2"]})
    n += _add_polys(drawing, con["buildable_wkt"], CONSTRUCTION_LAYERS["buildable"][0], {**base, "step": "buildable"})
    return n


def _add_polys(drawing: Drawing, wkt_text: str, layer: str, meta: dict[str, Any]) -> int:
    if not wkt_text:
        return 0
    geom: BaseGeometry = shapely_wkt.loads(wkt_text)
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", [])
                                                         if g.geom_type == "Polygon"]
    n = 0
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            pts = [[x, y] for x, y in list(ring.coords)[:-1]]
            drawing.add(Entity("polyline", layer, {"points": pts, "closed": True}, meta=dict(meta)))
            n += 1
    return n
