"""DXF import/export via ezdxf.

Import never drops data silently: every model-space entity is counted as imported,
approximated, expanded (block/dimension contents) or skipped, and the counts are
returned in an ``ImportReport``. Callers keep a byte-for-byte copy of the source file.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import colors as ezcolors
from ezdxf import recover
from ezdxf.math import Vec3

from ..core.errors import CadImportError
from ..core.units import DrawingUnits, LengthUnit, insunits_from_unit, unit_from_insunits
from .model import Drawing, Entity, Layer

APPID = "SLS"
FLATTEN_DISTANCE_FACTOR = 1e-4  # chord tolerance relative to drawing size for curve approximation
SKIPPED_TYPES_NOTE = {
    "HATCH": "hatch fills are not imported (boundary geometry must be drawn as polylines)",
    "SOLID": "2D solids/arrowheads are not imported",
    "3DFACE": "3D faces are not supported",
    "3DSOLID": "ACIS solids are not supported",
    "MESH": "meshes are not supported",
    "IMAGE": "raster images are not imported",
    "WIPEOUT": "wipeouts are not imported",
    "VIEWPORT": "viewports are layout objects",
}


@dataclass
class ImportReport:
    source_name: str
    dxf_version: str
    insunits_code: int | None
    suggested_unit: str
    imported: dict[str, int] = field(default_factory=dict)
    approximated: dict[str, int] = field(default_factory=dict)
    expanded: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    paperspace_entities: int = 0
    layers: dict[str, int] = field(default_factory=dict)
    extents: tuple[float, float, float, float] | None = None
    warnings: list[str] = field(default_factory=list)
    audit_fixes: list[str] = field(default_factory=list)
    restored_sls_metadata: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_imported(self) -> int:
        return sum(self.imported.values()) + sum(self.approximated.values())


def _layer_color(layer: Any) -> str:
    rgb = layer.rgb
    if rgb is None:
        aci = abs(layer.color) if layer.color else 7
        rgb = ezcolors.aci2rgb(aci if 1 <= aci <= 255 else 7)
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _entity_color(e: Any) -> str | None:
    if e.dxf.hasattr("true_color"):
        r, g, b = ezcolors.int2rgb(e.dxf.true_color)
        return f"#{r:02X}{g:02X}{b:02X}"
    aci = e.dxf.get("color", 256)
    if aci in (0, 256) or not (1 <= aci <= 255):
        return None  # BYBLOCK / BYLAYER
    r, g, b = ezcolors.aci2rgb(aci)
    return f"#{r:02X}{g:02X}{b:02X}"


def _read_sls_xdata(e: Any) -> dict[str, Any] | None:
    if not e.has_xdata(APPID):
        return None
    chunks = [v for code, v in e.get_xdata(APPID) if code == 1000]
    try:
        return json.loads("".join(chunks))
    except json.JSONDecodeError:
        return None


def _is_default_extrusion(e: Any) -> bool:
    ext = e.dxf.get("extrusion", Vec3(0, 0, 1))
    return Vec3(ext).isclose(Vec3(0, 0, 1), abs_tol=1e-9)


class _Importer:
    def __init__(self, doc: Any, report: ImportReport, flatten_distance: float) -> None:
        self.doc = doc
        self.report = report
        self.flatten_distance = flatten_distance
        self.drawing = Drawing()

    def count(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def add(self, kind: str, layer: str, data: dict[str, Any], src: Any, meta: dict[str, Any]) -> None:
        ent = Entity(kind, layer, data, color=_entity_color(src))
        meta = dict(meta)
        handle = src.dxf.get("handle")
        if handle:
            meta.setdefault("source_handle", handle)
        stored = _read_sls_xdata(src)
        if stored:
            ent.id = stored.get("id", ent.id)
            meta.update(stored.get("meta", {}))
            self.report.restored_sls_metadata += 1
        meta.setdefault("source", "survey")
        ent.meta = meta
        if self.drawing.has_layer(layer):
            ent.layer = self.drawing.layer(layer).name  # normalise case to the layer table entry
        else:
            self.drawing.add_layer(Layer(layer, source_name=layer))
        self.drawing.entities[ent.id] = ent

    def flatten_to_polyline(self, e: Any, layer: str, meta: dict[str, Any], kind_name: str) -> bool:
        from ezdxf import path as ezpath

        try:
            p = ezpath.make_path(e)
            pts = [(v.x, v.y) for v in p.flattening(self.flatten_distance)]
        except (TypeError, ValueError, ezdxf.DXFError) as exc:
            self.report.warnings.append(f"{kind_name} {e.dxf.get('handle')} could not be approximated: {exc}")
            self.count(self.report.skipped, kind_name)
            return False
        if len(pts) < 2:
            self.count(self.report.skipped, kind_name)
            return False
        closed = len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-9
        if closed:
            pts = pts[:-1]
        self.add("polyline", layer, {"points": [list(p) for p in pts], "closed": closed}, e,
                 {**meta, "approximated_from": kind_name, "approximation_tolerance": self.flatten_distance})
        self.count(self.report.approximated, kind_name)
        return True

    def convert(self, e: Any, layer_override: str | None = None, meta: dict[str, Any] | None = None,
                depth: int = 0) -> None:
        t = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if layer_override and layer == "0":
            layer = layer_override  # CAD convention: block entities on layer 0 take the INSERT's layer
        meta = dict(meta or {})
        expanded = bool(meta.get("expanded_from"))
        bucket = self.report.expanded if expanded else self.report.imported
        z = None
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            if abs(s.z) > 1e-12 or abs(en.z) > 1e-12:
                meta["elevation"] = [s.z, en.z]
            self.add("line", layer, {"p1": [s.x, s.y], "p2": [en.x, en.y]}, e, meta)
            self.count(bucket, t)
        elif t == "LWPOLYLINE":
            if not _is_default_extrusion(e):
                self.flatten_to_polyline(e, layer, meta, t)
                return
            pts = [[x, y, b] if abs(b) > 1e-12 else [x, y] for x, y, b in e.get_points("xyb")]
            z = e.dxf.get("elevation", 0.0)
            if z:
                meta["elevation"] = z
            self.add("polyline", layer, {"points": pts, "closed": bool(e.closed)}, e, meta)
            self.count(bucket, t)
        elif t == "POLYLINE":
            if e.is_2d_polyline and _is_default_extrusion(e):
                pts = []
                for v in e.vertices:
                    b = v.dxf.get("bulge", 0.0)
                    pts.append([v.dxf.location.x, v.dxf.location.y, b] if abs(b) > 1e-12
                               else [v.dxf.location.x, v.dxf.location.y])
                self.add("polyline", layer, {"points": pts, "closed": bool(e.is_closed)}, e, meta)
                self.count(bucket, t)
            elif e.is_3d_polyline:
                pts = [[v.dxf.location.x, v.dxf.location.y] for v in e.vertices]
                meta["elevation"] = [v.dxf.location.z for v in e.vertices]
                meta["projected_from_3d"] = True
                self.add("polyline", layer, {"points": pts, "closed": bool(e.is_closed)}, e, meta)
                self.count(self.report.approximated, "POLYLINE(3D)")
            elif e.is_2d_polyline:
                self.flatten_to_polyline(e, layer, meta, t)
            else:
                self.count(self.report.skipped, "POLYLINE(mesh/polyface)")
        elif t in ("CIRCLE", "ARC"):
            if not _is_default_extrusion(e):
                self.flatten_to_polyline(e, layer, meta, t)
                return
            c = e.dxf.center
            if abs(c.z) > 1e-12:
                meta["elevation"] = c.z
            data: dict[str, Any] = {"center": [c.x, c.y], "radius": e.dxf.radius}
            if t == "ARC":
                data.update(start_angle=e.dxf.start_angle, end_angle=e.dxf.end_angle)
            self.add(t.lower(), layer, data, e, meta)
            self.count(bucket, t)
        elif t in ("ELLIPSE", "SPLINE"):
            self.flatten_to_polyline(e, layer, meta, t)
        elif t == "POINT":
            p = e.dxf.location
            self.add("point", layer, {"position": [p.x, p.y]}, e, meta)
            self.count(bucket, t)
        elif t in ("TEXT", "ATTRIB", "MTEXT"):
            if t == "MTEXT":
                p = e.dxf.insert
                text = e.plain_text()
                height = e.dxf.get("char_height", 1.0)
                rotation = e.get_rotation()
                meta["multiline"] = True
            else:
                halign = e.dxf.get("halign", 0)
                p = e.dxf.align_point if (halign or e.dxf.get("valign", 0)) and e.dxf.hasattr("align_point") \
                    else e.dxf.insert
                text = e.plain_text()
                height = e.dxf.get("height", 1.0)
                rotation = e.dxf.get("rotation", 0.0)
                if t == "ATTRIB":
                    meta["attribute_tag"] = e.dxf.get("tag", "")
            self.add("text", layer, {"position": [p.x, p.y], "text": text, "height": height,
                                     "rotation": rotation}, e, meta)
            self.count(bucket, t)
        elif t == "INSERT":
            if depth > 16:
                self.report.warnings.append(f"Block nesting too deep at INSERT {e.dxf.get('handle')}; skipped.")
                self.count(self.report.skipped, "INSERT(nested>16)")
                return
            self.count(self.report.expanded, "INSERT")
            sub_meta = {"expanded_from": "INSERT", "block": e.dxf.name, "insert_handle": e.dxf.get("handle")}
            try:
                for ve in e.virtual_entities():
                    self.convert(ve, layer, sub_meta, depth + 1)
                for attrib in e.attribs:
                    self.convert(attrib, layer, {**sub_meta, "attribute": True}, depth + 1)
            except (ezdxf.DXFError, ValueError, ZeroDivisionError) as exc:
                self.report.warnings.append(f"Block '{e.dxf.name}' could not be expanded: {exc}")
        elif t == "DIMENSION" and (_read_sls_xdata(e) or {}).get("dim"):
            # A dimension written by this application: restore it as an editable dimension.
            stored = _read_sls_xdata(e) or {}
            self.add("dimension", layer, stored["dim"], e, meta)
            self.count(bucket, t)
        elif t == "DIMENSION" or t == "ARC_DIMENSION":
            self.count(self.report.expanded, t)
            sub_meta = {"expanded_from": t, "measurement": _safe_measurement(e)}
            try:
                for ve in e.virtual_entities():
                    self.convert(ve, layer, sub_meta, depth + 1)
            except (ezdxf.DXFError, ValueError, ZeroDivisionError) as exc:
                self.report.warnings.append(f"Dimension {e.dxf.get('handle')} could not be expanded: {exc}")
        else:
            key = f"{t} (inside {meta['expanded_from']})" if expanded else t
            self.count(self.report.skipped, key)


def _safe_measurement(e: Any) -> float | None:
    try:
        return float(e.get_measurement())
    except (ezdxf.DXFError, AttributeError, TypeError, ValueError):
        return None


def import_dxf(path: Path) -> tuple[Drawing, ImportReport]:
    path = Path(path)
    if not path.is_file():
        raise CadImportError(f"File not found: {path}")
    try:
        doc, auditor = recover.readfile(str(path))
    except IOError as exc:
        raise CadImportError(f"Cannot read {path.name}: {exc}") from exc
    except ezdxf.DXFStructureError as exc:
        raise CadImportError(f"{path.name} is not a valid DXF file: {exc}") from exc
    insunits = doc.header.get("$INSUNITS")
    suggested = unit_from_insunits(insunits)
    report = ImportReport(path.name, doc.dxfversion, insunits, suggested.value)
    report.audit_fixes = [str(f.message) for f in auditor.fixes]
    if auditor.has_errors:
        report.warnings += [f"DXF error not fixed: {err.message}" for err in auditor.errors]

    # Flattening tolerance relative to the stored extents (falls back to 1 mm in metres).
    ext_min, ext_max = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
    size = 1.0
    try:
        size = max(abs(ext_max[0] - ext_min[0]), abs(ext_max[1] - ext_min[1]), 1.0)
        if size > 1e12:
            size = 1.0
    except (TypeError, IndexError):
        pass
    importer = _Importer(doc, report, max(size * FLATTEN_DISTANCE_FACTOR, 1e-6))

    for dl in doc.layers:
        layer = Layer(
            name=dl.dxf.name, color=_layer_color(dl), linetype=dl.dxf.get("linetype", "CONTINUOUS"),
            lineweight=(dl.dxf.get("lineweight", -3) / 100.0) if dl.dxf.get("lineweight", -3) > 0 else 0.25,
            visible=dl.is_on() and not dl.is_frozen(), locked=dl.is_locked(), source_name=dl.dxf.name,
        )
        try:
            layer.transparency = float(dl.transparency or 0.0)
        except (AttributeError, TypeError, ValueError):
            pass
        if not importer.drawing.has_layer(layer.name):
            importer.drawing.add_layer(layer)

    for e in doc.modelspace():
        importer.convert(e)
    for layout in doc.layouts:
        if not layout.is_modelspace:
            report.paperspace_entities += len(layout)
    if report.paperspace_entities:
        report.warnings.append(f"{report.paperspace_entities} paper-space (layout) entities were not imported.")
    for t, n in report.skipped.items():
        note = SKIPPED_TYPES_NOTE.get(t.split(" ")[0], "entity type not supported")
        report.warnings.append(f"Skipped {n} × {t}: {note}.")
    for t, n in report.approximated.items():
        report.warnings.append(f"Approximated {n} × {t} as polylines (flagged on each entity).")

    drawing = importer.drawing
    drawing.units = DrawingUnits(unit=suggested, verified=False)
    drawing.info = {"source_name": path.name, "dxf_version": doc.dxfversion, "insunits": insunits}
    layer_counts = Counter(e.layer for e in drawing.entities.values())
    report.layers = {layer.name: layer_counts.get(layer.name, 0) for layer in drawing.layers}
    report.extents = drawing.extents(only_visible=False)
    if suggested is LengthUnit.UNKNOWN:
        report.warnings.append("The DXF file does not declare drawing units ($INSUNITS). Units must be set "
                               "and verified before any measurement.")
    drawing.active_layer = drawing.layers[0].name if drawing.layers else "0"
    return drawing, report


_VALID_LW = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]


def _dxf_lineweight(mm: float) -> int:
    target = round(mm * 100)
    return min(_VALID_LW, key=lambda v: abs(v - target))


def _xdata_chunks(payload: str, size: int = 240) -> list[tuple[int, str]]:
    return [(1000, payload[i:i + size]) for i in range(0, len(payload), size)] or [(1000, "")]


def export_dxf(drawing: Drawing, path: Path, dxfversion: str = "R2018") -> list[str]:
    """Write the drawing to DXF. Returns warnings (e.g. unknown linetypes)."""
    warnings: list[str] = []
    doc = ezdxf.new(dxfversion, setup=True)
    doc.appids.add(APPID)
    doc.header["$INSUNITS"] = insunits_from_unit(drawing.units.unit)
    for layer in drawing.layers:
        dl = doc.layers.get(layer.name) if layer.name in doc.layers else doc.layers.add(layer.name)
        r, g, b = (int(layer.color[i:i + 2], 16) for i in (1, 3, 5))
        dl.rgb = (r, g, b)
        lt = layer.linetype if layer.linetype in doc.linetypes else "CONTINUOUS"
        if lt != layer.linetype:
            warnings.append(f"Linetype '{layer.linetype}' of layer '{layer.name}' is not defined; "
                            "exported as CONTINUOUS.")
        dl.dxf.linetype = lt
        dl.dxf.lineweight = _dxf_lineweight(layer.lineweight)
        if not layer.visible:
            dl.off()
        if layer.locked:
            dl.lock()
        if layer.transparency:
            dl.transparency = layer.transparency
    msp = doc.modelspace()
    for e in drawing.entities.values():
        attribs: dict[str, Any] = {"layer": e.layer}
        if e.color:
            attribs["true_color"] = ezcolors.rgb2int(tuple(int(e.color[i:i + 2], 16) for i in (1, 3, 5)))
        d = e.data
        if e.kind == "line":
            ent = msp.add_line(d["p1"], d["p2"], dxfattribs=attribs)
        elif e.kind == "polyline":
            pts = [(p[0], p[1], 0, 0, p[2] if len(p) > 2 else 0.0) for p in d["points"]]
            ent = msp.add_lwpolyline(pts, format="xyseb", close=bool(d.get("closed")), dxfattribs=attribs)
        elif e.kind == "circle":
            ent = msp.add_circle(d["center"], d["radius"], dxfattribs=attribs)
        elif e.kind == "arc":
            ent = msp.add_arc(d["center"], d["radius"], d["start_angle"], d["end_angle"], dxfattribs=attribs)
        elif e.kind == "text":
            ent = msp.add_text(d["text"], height=d.get("height", 1.0), rotation=d.get("rotation", 0.0),
                               dxfattribs=attribs)
            ent.set_placement(d["position"])
        elif e.kind == "point":
            ent = msp.add_point(d["position"], dxfattribs=attribs)
        elif e.kind == "dimension":
            dim = msp.add_aligned_dim(p1=d["p1"], p2=d["p2"], distance=d.get("offset", 1.0),
                                      override={"dimtxt": d.get("text_height", 0.25)}, dxfattribs=attribs)
            dim.render()
            ent = dim.dimension
        else:  # pragma: no cover - model validates kinds
            warnings.append(f"Entity kind {e.kind} not exported.")
            continue
        stored: dict[str, Any] = {"id": e.id, "meta": e.meta}
        if e.kind == "dimension":
            stored["dim"] = d
        payload = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
        ent.set_xdata(APPID, _xdata_chunks(payload))
    doc.saveas(str(path), encoding="utf-8")
    return warnings
