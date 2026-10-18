"""Reading cadastral parcel layers from Esri Shapefiles (pure Python: pyshp + shapely).

A shapefile is a set of files sharing one name: .shp (geometry), .shx (index), .dbf
(attributes), .prj (coordinate system) and optionally .cpg (text encoding). Every record is
read; problems (invalid or empty polygons, missing or repeated keys) are reported per record
and never repaired or dropped silently.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from ..core.errors import SlsError

COMPONENTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
LOGICAL_FIELDS = ("key", "village", "basin", "sector", "sheet", "parcel", "globalid")

# Header names recognised for the mapping suggestion (Jordan DLS / PDTRA schemas). Confirmed by the user.
_SUGGEST = {
    "key": ("dls_key", "parcel_key", "key"),
    "village": ("vill_code", "village", "village_code", "قرية"),
    "basin": ("hod_code", "hod", "basin", "حوض"),
    "sector": ("sector_cod", "sector_code", "sector", "hay", "حي"),
    "sheet": ("sheet_no", "sheet", "لوحة"),
    "parcel": ("parcel_id", "parcel_no", "plot_no", "parcel", "قطعة"),
    "globalid": ("globalid", "global_id", "guid"),
}


class ShapefileError(SlsError):
    code = "shapefile"


@dataclass
class FieldMapping:
    columns: dict[str, str] = field(default_factory=dict)  # logical field -> DBF field name

    def to_dict(self) -> dict[str, Any]:
        return {"columns": self.columns}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FieldMapping":
        return cls(dict(d.get("columns", {})))

    def get(self, record: dict[str, Any], logical: str) -> str:
        name = self.columns.get(logical)
        if not name:
            return ""
        v = record.get(name)
        return "" if v is None else str(v).strip()


ZONING_FIELDS = ("zone_id", "zone_name", "key")
_SUGGEST_ZONING = {
    "zone_id": ("zone_id", "zoneid", "zone", "landuse_id", "رمز الصفة"),
    "zone_name": ("الاست", "الاستعمال", "zone_name", "name", "landuse", "صفة الاستعمال", "الصفة"),
    "key": ("dls_key", "parcel_key"),
}


def suggest_zoning_mapping(field_names: Sequence[str]) -> FieldMapping:
    norm = {f.casefold(): f for f in field_names}
    cols = {}
    for logical, names in _SUGGEST_ZONING.items():
        for n in names:
            if n.casefold() in norm:
                cols[logical] = norm[n.casefold()]
                break
    return FieldMapping(cols)


def suggest_mapping(field_names: Sequence[str]) -> FieldMapping:
    norm = {f.casefold(): f for f in field_names}
    cols = {}
    for logical, names in _SUGGEST.items():
        for n in names:
            if n.casefold() in norm:
                cols[logical] = norm[n.casefold()]
                break
    return FieldMapping(cols)


def components(shp: Path) -> dict[str, Path]:
    """Existing component files next to ``shp`` (case-insensitive extensions)."""
    shp = Path(shp)
    found: dict[str, Path] = {}
    for p in shp.parent.iterdir():
        if p.stem.casefold() == shp.stem.casefold() and p.suffix.lower() in COMPONENTS:
            found[p.suffix.lower()] = p
    return found


@dataclass
class ParcelRecord:
    fid: int
    attrs: dict[str, Any]
    geometry: BaseGeometry | None
    parts: int
    valid: bool
    issue: str = ""


@dataclass
class LayerInfo:
    path: Path
    shape_type: str
    count: int
    fields: list[str]
    encoding: str
    crs_wkt: str
    crs_code: str
    bbox: tuple[float, float, float, float]
    missing_components: list[str]


def _encoding(parts: dict[str, Path]) -> str:
    cpg = parts.get(".cpg")
    if cpg is not None:
        text = cpg.read_text(encoding="ascii", errors="ignore").strip()
        return {"utf-8": "utf-8", "utf8": "utf-8", "65001": "utf-8"}.get(text.lower(), text or "utf-8")
    return "cp1256"  # Arabic Windows code page: the common default for Jordanian DBF files without .cpg


def inspect(shp: Path) -> LayerInfo:
    import shapefile

    parts = components(shp)
    missing = [c for c in (".shp", ".shx", ".dbf") if c not in parts]
    if missing:
        raise ShapefileError(f"Shapefile is incomplete: missing {', '.join(missing)} next to {Path(shp).name}.")
    enc = _encoding(parts)
    wkt = parts[".prj"].read_text(encoding="utf-8", errors="replace").strip() if ".prj" in parts else ""
    code = ""
    if wkt:
        from ..core.crs import CrsUnavailable, from_wkt

        try:
            code = from_wkt(wkt)
        except CrsUnavailable:
            code = ""
    try:
        r = shapefile.Reader(str(parts[".shp"]), encoding=enc)
    except shapefile.ShapefileException as exc:
        raise ShapefileError(f"Cannot read {Path(shp).name}: {exc}") from exc
    try:
        return LayerInfo(Path(shp), r.shapeTypeName, len(r), [f[0] for f in r.fields[1:]], enc, wkt,
                         code if len(code) < 64 else "", tuple(r.bbox),  # type: ignore[arg-type]
                         [c for c in (".prj", ".cpg") if c not in parts])
    finally:
        r.close()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


POLYGON_TYPES = ("POLYGON", "POLYGONZ", "POLYGONM")
LINE_TYPES = ("POLYLINE", "POLYLINEZ", "POLYLINEM")


def iter_records(shp: Path, encoding: str, shape_types: Sequence[str] = POLYGON_TYPES) -> Iterator[ParcelRecord]:
    """Every record of the layer, in file order, with its geometry problems (never repaired)."""
    import shapefile

    capture = _Capture()
    logger = logging.getLogger("shapefile")
    logger.addHandler(capture)
    propagate = logger.propagate
    logger.propagate = False  # geometry warnings are attached to their record instead of printed
    try:
        yield from _iter_records(shapefile, shp, encoding, capture, tuple(shape_types))
    finally:
        logger.removeHandler(capture)
        logger.propagate = propagate


def _iter_records(shapefile, shp: Path, encoding: str, capture: "_Capture",  # type: ignore[no-untyped-def]
                  shape_types: tuple[str, ...]) -> Iterator[ParcelRecord]:
    with shapefile.Reader(str(components(shp)[".shp"]), encoding=encoding) as r:
        if r.shapeTypeName not in shape_types:
            raise ShapefileError(f"Expected a {'/'.join(sorted({t.rstrip('ZM').lower() for t in shape_types}))} "
                                 f"layer, found {r.shapeTypeName}.")
        for i, sr in enumerate(r.iterShapeRecords()):
            attrs = sr.record.as_dict()
            s = sr.shape
            if not s.points:
                yield ParcelRecord(i, attrs, None, 0, False, "empty geometry")
                continue
            capture.messages.clear()
            geom = shape(s.__geo_interface__)
            n = len(getattr(geom, "geoms", [geom]))
            issue = "; ".join(m.split(":")[0] for m in capture.messages)
            valid = geom.is_valid
            if geom.geom_type in ("LineString", "MultiLineString"):
                if not valid:
                    issue = "; ".join(x for x in (issue, f"invalid line: {explain_validity(geom)}") if x)
                elif geom.length <= 1e-6:
                    valid, issue = False, "; ".join(x for x in (issue, "zero length") if x)
                yield ParcelRecord(i, attrs, geom, n, valid, issue)
                continue
            if not valid:
                issue = "; ".join(x for x in (issue, f"invalid polygon: {explain_validity(geom)}") if x)
            elif geom.area <= 1e-6:
                valid, issue = False, "; ".join(x for x in (issue, "zero area") if x)
            elif issue:
                valid = False  # e.g. ring orientation problems reported by the reader
            yield ParcelRecord(i, attrs, geom, n, valid, issue)


def summarize(records: Sequence[ParcelRecord], mapping: FieldMapping) -> dict[str, Any]:
    """Counts of the data-quality findings shown before import."""
    keys = Counter(mapping.get(r.attrs, "key") for r in records)
    repeated = {k: v for k, v in keys.items() if k and v > 1}
    return {
        "records": len(records),
        "invalid": sum(1 for r in records if not r.valid),
        "multipart": sum(1 for r in records if r.parts > 1),
        "missing_key": keys.get("", 0) if mapping.columns.get("key") else None,
        "repeated_keys": len(repeated),
        "repeated_key_records": sum(repeated.values()),
        "missing_parcel": sum(1 for r in records if mapping.columns.get("parcel") and not mapping.get(r.attrs, "parcel")),
        "examples_repeated": sorted(repeated.items(), key=lambda kv: -kv[1])[:5],
    }


def attrs_json(attrs: dict[str, Any]) -> str:
    return json.dumps({k: ("" if v is None else str(v)) for k, v in attrs.items()}, ensure_ascii=False, sort_keys=True)
