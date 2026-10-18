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

DERIVED_LAYERS = {
    "setback": "SETBACK_BOUNDARY",
    "violation": "SETBACK_VIOLATIONS",
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

    def preview_import(self, path: Path) -> tuple[Drawing, ImportReport]:
        suffix = Path(path).suffix.lower()
        if suffix == ".dxf":
            return import_dxf(path)
        if suffix == ".dwg":
            from ..cad.dwg import import_dwg

            return import_dwg(path, self.ws.settings.oda_converter_path)
        raise CadImportError(f"Unsupported drawing format: {suffix or '(none)'}")

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
        if case.units.unit is LengthUnit.UNKNOWN and drawing.units.unit is not LengthUnit.UNKNOWN and role == "survey":
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
        if parcel.verified and not verified:
            inputs.extraction_issues.append({
                "severity": "error", "code": "boundary_changed",
                "message": "The property boundary changed after it was verified; verify it again.", "entity_id":
                parcel.boundary_entity_id})
        return inputs

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
        for eid in [e.id for e in drawing.entities.values() if e.meta.get("source") == "derived"]:
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
