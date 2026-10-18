"""Comparison with reference drawings produced by the authority's approved AutoCAD workflow.

No such reference is bundled: official drawings are confidential and none has been supplied.
Set ``SLS_SETBACK_REFERENCES`` to a local folder (outside the repository) containing pairs:

    <name>.dxf   closed polyline of the parcel on layer PARCEL (vertex order = edge order) and the
                 approved buildable boundary drawn in AutoCAD on layer BUILDABLE_APPROVED
                 (one closed polyline per polygon), in metres, anonymised if required
    <name>.json  {"edge_types": [...], "setbacks": {"front": 4, ...}, "tolerance_m": 0.01,
                  "source": "drawing number / reviewer / date", "parcel_layer": "PARCEL",
                  "reference_layer": "BUILDABLE_APPROVED"}

Each reference passes when the boundary of the computed buildable area is within
``tolerance_m`` of the AutoCAD boundary (Hausdorff distance) and the areas agree within
tolerance × perimeter. Until references pass, no equivalence with AutoCAD is claimed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from sls.geometry.setbacks import compute_setbacks, compute_setbacks_offset

REF_DIR = os.environ.get("SLS_SETBACK_REFERENCES", "")


def _closed_polylines(doc, layer: str) -> list[Polygon]:  # type: ignore[no-untyped-def]
    out = []
    for e in doc.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'):
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if e.closed or (len(pts) > 2 and pts[0] == pts[-1]):
            out.append(Polygon(pts))
    return out


def compare_reference(dxf: Path, spec: dict) -> dict:  # type: ignore[type-arg]
    doc = ezdxf.readfile(str(dxf))
    parcels = _closed_polylines(doc, spec.get("parcel_layer", "PARCEL"))
    refs = _closed_polylines(doc, spec.get("reference_layer", "BUILDABLE_APPROVED"))
    assert len(parcels) == 1, f"{dxf.name}: expected one parcel polyline, found {len(parcels)}"
    res = compute_setbacks_offset(parcels[0], spec["edge_types"], spec["setbacks"])
    ref = unary_union(refs) if refs else Polygon()
    tol = float(spec.get("tolerance_m", 0.01))
    hausdorff = res.buildable.boundary.hausdorff_distance(ref.boundary) if refs and not res.buildable.is_empty \
        else (0.0 if res.buildable.is_empty and not refs else float("inf"))
    return {"hausdorff_m": hausdorff, "area_m2": res.buildable_area, "reference_area_m2": float(ref.area),
            "area_tolerance_m2": tol * max(ref.length, 1.0), "tolerance_m": tol,
            "review": [i.code for i in res.construction.by_severity("review")]}


def _cases() -> list[Path]:
    if not REF_DIR:
        return []
    return sorted(p for p in Path(REF_DIR).glob("*.dxf") if p.with_suffix(".json").exists())


@pytest.mark.skipif(not REF_DIR, reason="SLS_SETBACK_REFERENCES not set: no authority AutoCAD reference drawings")
@pytest.mark.parametrize("dxf", _cases(), ids=lambda p: p.stem)
def test_matches_authority_autocad_reference(dxf):
    spec = json.loads(dxf.with_suffix(".json").read_text(encoding="utf-8"))
    r = compare_reference(dxf, spec)
    assert r["hausdorff_m"] <= r["tolerance_m"], r
    assert abs(r["area_m2"] - r["reference_area_m2"]) <= r["area_tolerance_m2"], r


def test_harness_self_check_with_synthetic_package(tmp_path):
    """Exercises the comparison code only. The 'reference' is hand-derived, NOT an AutoCAD drawing."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)], close=True,
                       dxfattribs={"layer": "PARCEL"})
    msp.add_lwpolyline([(2, 2), (38, 2), (38, 8), (8, 8), (8, 38), (2, 38)], close=True,
                       dxfattribs={"layer": "BUILDABLE_APPROVED"})
    path = tmp_path / "synthetic_L.dxf"
    doc.saveas(str(path))
    spec = {"edge_types": ["side"] * 6, "setbacks": {"side": 2}, "tolerance_m": 0.001}
    r = compare_reference(path, spec)
    assert r["hausdorff_m"] < 1e-9 and r["area_m2"] == pytest.approx(r["reference_area_m2"])
    # a reference with a rounded inner corner (distance-zone result) is detected as different
    rounded = compute_setbacks(Polygon([(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)]), ["side"] * 6,
                               {"side": 2}).buildable
    doc2 = ezdxf.new()
    doc2.modelspace().add_lwpolyline([(0, 0), (40, 0), (40, 10), (10, 10), (10, 40), (0, 40)], close=True,
                                     dxfattribs={"layer": "PARCEL"})
    doc2.modelspace().add_lwpolyline(list(rounded.exterior.coords)[:-1], close=True,
                                     dxfattribs={"layer": "BUILDABLE_APPROVED"})
    doc2.saveas(str(tmp_path / "rounded.dxf"))
    r2 = compare_reference(tmp_path / "rounded.dxf", dict(spec, tolerance_m=0.1))
    assert r2["hausdorff_m"] == pytest.approx(2 * (2 ** 0.5 - 1), abs=1e-3)  # sharp vs rounded corner gap
    assert r2["hausdorff_m"] > 0.1
