"""Versioned GIS reference datasets: staged import, validation, comparison, activation, rollback.

One pipeline for every Shapefile-based dataset (parcels, land-use zoning, roads). Each kind has an
adapter for what differs (accepted geometry types, field mapping, feature rows, stable identity
used in comparisons); everything else is shared:

* **Stage.** The source files are inspected (required sidecars, declared CRS), every record is
  read and validated, and a new version is written with ``status='staged'`` and ``active=0``
  inside one transaction. The active version is never touched. A failure or cancellation rolls
  the transaction back and removes the source copies it made; the failure is audited.
* **Compare.** Counts, CRS, field mapping, validation findings and, where identifiers allow,
  added / removed / changed records against the active version.
* **Activate.** Explicit, audited, atomic: in one transaction the old version becomes
  ``inactive`` and the chosen one ``active``. If anything fails, the old version stays active.
* **Rollback.** Re-activates the previously active version (the same atomic activation).
* Versions are never edited or deleted. A staged version that is not wanted can be discarded:
  its feature rows are removed, its record (sources, hashes, summary) is kept.

Cases and calculations keep their own snapshots (boundary geometry, zoning overlay, rule version)
and record the dataset versions they used, so refreshing a dataset never changes them.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shapely import wkb as shapely_wkb

from ..core.errors import SlsError, ValidationError
from ..gis.shapefile_layer import (LINE_TYPES, POLYGON_TYPES, FieldMapping, LayerInfo, ParcelRecord, attrs_json,
                                   components, inspect, iter_records, suggest_mapping, suggest_zoning_mapping, summarize)
from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace

# Suspicious geometry change between versions (same identifier): reported for review.
AREA_CHANGE_PCT = 5.0
CENTROID_SHIFT_M = 5.0


class ImportCancelled(SlsError):
    code = "cancelled"


@dataclass
class ImportControl:
    """Progress reporting and cancellation for long imports (called from the worker thread)."""

    progress: Callable[[str, int, int], None] | None = None
    cancelled: Callable[[], bool] | None = None

    def step(self, stage: str, n: int, total: int) -> None:
        if self.progress is not None:
            self.progress(stage, n, total)
        if self.cancelled is not None and self.cancelled():
            raise ImportCancelled("Import cancelled. The active dataset is unchanged.")


@dataclass(frozen=True)
class DatasetVersion:
    kind: str
    id: str
    name: str
    version: int
    status: str            # staged | active | inactive | discarded
    active: bool
    crs_code: str
    encoding: str
    mapping: FieldMapping
    summary: dict[str, Any]
    validation: dict[str, Any]
    record_count: int
    imported_at: str
    imported_by: str
    activated_at: str
    activated_by: str
    sources: list[dict[str, Any]]

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version} ({self.record_count})"


# -- adapters ---------------------------------------------------------------------------------

class Adapter:
    kind = ""
    layer_table = ""
    feature_table = ""
    permission = ""
    shape_types: tuple[str, ...] = POLYGON_TYPES
    logical_fields: tuple[str, ...] = ()
    store_category = "cadastre"

    def suggest(self, fields: Sequence[str]) -> FieldMapping:
        raise NotImplementedError

    def check_mapping(self, mapping: FieldMapping) -> None:
        pass

    def summarize(self, records: Sequence[ParcelRecord], mapping: FieldMapping) -> dict[str, Any]:
        raise NotImplementedError

    def insert_sql(self) -> str:
        raise NotImplementedError

    def row(self, lid: str, r: ParcelRecord, mapping: FieldMapping) -> tuple[Any, ...]:
        raise NotImplementedError

    def identity_sql(self) -> str:
        """SELECT returning (ident, alt_ident, wkb, attrs_json) for one layer (``?`` = layer id)."""
        raise NotImplementedError


def _bounds(g) -> tuple[Any, Any, Any, Any]:  # type: ignore[no-untyped-def]
    return g.bounds if g is not None and not g.is_empty else (None, None, None, None)


class CadastreAdapter(Adapter):
    kind, layer_table, feature_table, permission = "cadastre", "cadastre_layers", "cadastre_parcels", "import_cadastre"
    logical_fields = ("key", "village", "basin", "sector", "sheet", "parcel", "globalid")

    def suggest(self, fields: Sequence[str]) -> FieldMapping:
        return suggest_mapping(fields)

    def check_mapping(self, mapping: FieldMapping) -> None:
        if not mapping.columns.get("parcel") and not mapping.columns.get("key"):
            raise ValidationError("Map at least the parcel number or the parcel key field.")

    def summarize(self, records: Sequence[ParcelRecord], mapping: FieldMapping) -> dict[str, Any]:
        return summarize(records, mapping)

    def insert_sql(self) -> str:
        return ("INSERT INTO cadastre_parcels(layer_id,fid,dls_key,village,basin,sector,sheet,parcel_no,globalid,"
                "attrs_json,wkb,area_m2,parts,minx,miny,maxx,maxy,valid,issue) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def row(self, lid: str, r: ParcelRecord, m: FieldMapping) -> tuple[Any, ...]:
        g = r.geometry
        return (lid, r.fid, m.get(r.attrs, "key"), m.get(r.attrs, "village"), m.get(r.attrs, "basin"),
                m.get(r.attrs, "sector"), m.get(r.attrs, "sheet"), m.get(r.attrs, "parcel"),
                m.get(r.attrs, "globalid"), attrs_json(r.attrs), g.wkb if g is not None else None,
                float(g.area) if g is not None else None, r.parts, *_bounds(g), int(r.valid), r.issue)

    def identity_sql(self) -> str:
        return "SELECT globalid, dls_key, wkb, attrs_json FROM cadastre_parcels WHERE layer_id=?"


class ZoningAdapter(Adapter):
    kind, layer_table, feature_table, permission = "zoning", "zoning_layers", "zoning_pieces", "import_zoning"
    logical_fields = ("zone_id", "zone_name", "key")

    def suggest(self, fields: Sequence[str]) -> FieldMapping:
        return suggest_zoning_mapping(fields)

    def check_mapping(self, mapping: FieldMapping) -> None:
        if not mapping.columns.get("zone_id"):
            raise ValidationError("Map the zone code field (e.g. ZONE_ID).")

    @staticmethod
    def code(r: ParcelRecord, m: FieldMapping) -> str:
        from .zoning_repo import _code

        c = _code(r.attrs.get(m.columns["zone_id"]))
        return "" if c in ("", "0") else c

    @staticmethod
    def zname(r: ParcelRecord, m: FieldMapping) -> str:
        f = m.columns.get("zone_name", "")
        return " ".join(str(r.attrs.get(f, "") or "").split()) if f else ""

    def summarize(self, records: Sequence[ParcelRecord], m: FieldMapping) -> dict[str, Any]:
        codes: dict[str, str] = {}
        blank = 0
        for r in records:
            c = self.code(r, m)
            if c:
                codes.setdefault(c, self.zname(r, m))
            else:
                blank += 1
        return {"records": len(records), "zones": len(codes), "invalid": sum(1 for r in records if not r.valid),
                "multipart": sum(1 for r in records if r.parts > 1), "without_zone": blank, "codes": codes}

    def insert_sql(self) -> str:
        return ("INSERT INTO zoning_pieces(layer_id,fid,zone_id,zone_name,dls_key,wkb,area_m2,minx,miny,maxx,maxy,"
                "valid,issue) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def row(self, lid: str, r: ParcelRecord, m: FieldMapping) -> tuple[Any, ...]:
        g = r.geometry
        return (lid, r.fid, self.code(r, m), self.zname(r, m), m.get(r.attrs, "key"),
                g.wkb if g is not None else None, float(g.area) if g is not None else None, *_bounds(g),
                int(r.valid), r.issue)

    def identity_sql(self) -> str:
        return "SELECT zone_id, dls_key, wkb, zone_name FROM zoning_pieces WHERE layer_id=?"


ROAD_FIELDS = ("road_id", "name", "road_class", "width")
_SUGGEST_ROADS = {
    "road_id": ("road_id", "roadid", "street_id", "objectid", "fid_", "id"),
    "name": ("name", "road_name", "street", "st_name", "اسم_الشارع", "اسم الشارع", "الاسم"),
    "road_class": ("class", "road_class", "type", "road_type", "category", "التصنيف"),
    "width": ("width", "road_width", "row_width", "العرض", "عرض_الشارع"),
}


class RoadAdapter(Adapter):
    """Street / road reference layers (lines or polygons). Reference data, not legal parcel boundaries."""

    kind, layer_table, feature_table, permission = "roads", "road_layers", "road_features", "import_roads"
    shape_types = LINE_TYPES + POLYGON_TYPES
    logical_fields = ROAD_FIELDS
    store_category = "gis"

    def suggest(self, fields: Sequence[str]) -> FieldMapping:
        norm = {f.casefold(): f for f in fields}
        cols = {}
        for logical, names in _SUGGEST_ROADS.items():
            for n in names:
                if n.casefold() in norm:
                    cols[logical] = norm[n.casefold()]
                    break
        return FieldMapping(cols)

    def summarize(self, records: Sequence[ParcelRecord], m: FieldMapping) -> dict[str, Any]:
        ids = Counter(m.get(r.attrs, "road_id") for r in records) if m.columns.get("road_id") else Counter()
        repeated = {k: v for k, v in ids.items() if k and v > 1}
        kinds = Counter(r.geometry.geom_type if r.geometry is not None else "empty" for r in records)
        return {"records": len(records), "invalid": sum(1 for r in records if not r.valid),
                "multipart": sum(1 for r in records if r.parts > 1), "geometry_types": dict(kinds),
                "missing_id": ids.get("", 0) if m.columns.get("road_id") else None,
                "repeated_ids": len(repeated), "repeated_id_records": sum(repeated.values()),
                "missing_name": sum(1 for r in records if m.columns.get("name") and not m.get(r.attrs, "name")),
                "total_length_m": sum(r.geometry.length for r in records if r.geometry is not None
                                      and r.geometry.geom_type.endswith("LineString"))}

    def insert_sql(self) -> str:
        return ("INSERT INTO road_features(layer_id,fid,road_id,name,road_class,width,attrs_json,wkb,geom_type,"
                "length_m,area_m2,parts,minx,miny,maxx,maxy,valid,issue) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def row(self, lid: str, r: ParcelRecord, m: FieldMapping) -> tuple[Any, ...]:
        g = r.geometry
        return (lid, r.fid, m.get(r.attrs, "road_id"), m.get(r.attrs, "name"), m.get(r.attrs, "road_class"),
                m.get(r.attrs, "width"), attrs_json(r.attrs), g.wkb if g is not None else None,
                g.geom_type if g is not None else "", float(g.length) if g is not None else None,
                float(g.area) if g is not None else None, r.parts, *_bounds(g), int(r.valid), r.issue)

    def identity_sql(self) -> str:
        return "SELECT road_id, name, wkb, attrs_json FROM road_features WHERE layer_id=?"


ADAPTERS: dict[str, Adapter] = {a.kind: a for a in (CadastreAdapter(), ZoningAdapter(), RoadAdapter())}


# -- manager ----------------------------------------------------------------------------------

class DatasetManager:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    @staticmethod
    def adapter(kind: str) -> Adapter:
        if kind not in ADAPTERS:
            raise ValidationError(f"Unknown dataset kind '{kind}'.")
        return ADAPTERS[kind]

    # -- queries ----------------------------------------------------------------------------

    def _version(self, kind: str, r: Any) -> DatasetVersion:
        keys = r.keys()
        return DatasetVersion(kind, r["id"], r["name"], r["version"], r["status"], bool(r["active"]), r["crs_code"],
                              r["encoding"], FieldMapping.from_dict(json.loads(r["mapping_json"])),
                              json.loads(r["summary_json"]), json.loads(r["validation_json"] or "{}"),
                              r["record_count"], r["imported_at"], r["imported_by"], r["activated_at"],
                              r["activated_by"], json.loads(r["source_json"]) if "source_json" in keys else [])

    def version(self, kind: str, layer_id: str) -> DatasetVersion:
        r = self.db.query_one(f"SELECT * FROM {self.adapter(kind).layer_table} WHERE id=?", (layer_id,))
        if r is None:
            raise ValidationError(f"Dataset version not found: {layer_id}")
        return self._version(kind, r)

    def versions(self, kind: str) -> list[DatasetVersion]:
        rows = self.db.query(f"SELECT * FROM {self.adapter(kind).layer_table} ORDER BY imported_at DESC, rowid DESC")
        return [self._version(kind, r) for r in rows]

    def active(self, kind: str) -> DatasetVersion | None:
        r = self.db.query_one(f"SELECT * FROM {self.adapter(kind).layer_table} WHERE active=1 "
                              "ORDER BY activated_at DESC LIMIT 1")
        return self._version(kind, r) if r else None

    # -- stage ------------------------------------------------------------------------------

    def inspect_source(self, kind: str, shp: Path, declared_crs: str = "") -> tuple[LayerInfo, dict[str, Any]]:
        """Sidecar and CRS checks shared by the dialog preview and ``stage``."""
        from ..core.crs import CrsUnavailable, describe, same_grid

        ad = self.adapter(kind)
        info = inspect(shp)  # raises when .shp/.shx/.dbf are missing
        if info.shape_type not in ad.shape_types:
            raise ValidationError(f"{kind}: expected {', '.join(ad.shape_types)} geometry, found {info.shape_type}.")
        declared = declared_crs.strip()
        if not info.crs_wkt and not declared:
            raise ValidationError(f"{Path(shp).name} has no .prj file: its coordinate system is unknown. Declare the "
                                  "coordinate system explicitly (it is never assumed).")
        if info.crs_wkt and not info.crs_code and not declared:
            raise ValidationError(f"The .prj of {Path(shp).name} is not a recognised coordinate system. Declare the "
                                  "coordinate system explicitly.")
        if declared:
            try:
                describe(declared)
            except CrsUnavailable as exc:
                raise ValidationError(f"Unknown coordinate system '{declared}'.") from exc
            if info.crs_code and not same_grid(declared, info.crs_code):
                raise ValidationError(f"The declared coordinate system {declared} differs from the .prj "
                                      f"({info.crs_code}). Nothing is transformed: fix the .prj or the declaration.")
        crs = declared or info.crs_code
        default = self.ws.settings.default_crs
        warnings: list[str] = []
        compatible = same_grid(crs, default)
        if not compatible:
            warnings.append(f"The layer's coordinate system {crs} is not the application grid {default}; cases in "
                            f"{default} cannot use it (nothing is transformed).")
        if ".cpg" in info.missing_components:
            warnings.append(f"No .cpg file: text encoding assumed to be {info.encoding} (Arabic Windows code page).")
        from ..core.crs import check_extents

        if info.count:
            try:
                ext = check_extents(crs, tuple(info.bbox))  # type: ignore[arg-type]
                if not ext.ok:
                    warnings.append(f"Extents check: {ext.message}")
            except CrsUnavailable as exc:
                warnings.append(f"Extents not checked: {exc}")
        return info, {"crs": crs, "crs_source": "declared" if declared and not info.crs_code else "prj",
                      "default_crs": default, "compatible_with_default": compatible, "warnings": warnings,
                      "sidecars": sorted(components(shp))}

    def stage(self, kind: str, user: "User", shp: Path, mapping: FieldMapping, name: str = "",
              declared_crs: str = "", control: ImportControl | None = None) -> DatasetVersion:
        ad = self.adapter(kind)
        user.require(ad.permission)
        control = control or ImportControl()
        info, validation = self.inspect_source(kind, shp, declared_crs)
        unknown = [f for f in mapping.columns.values() if f not in info.fields]
        if unknown:
            raise ValidationError(f"Mapped fields do not exist in the layer: {', '.join(unknown)}")
        ad.check_mapping(mapping)
        name = (name or Path(shp).stem).strip()
        lid = new_id()
        created: list[Path] = []
        try:
            records: list[ParcelRecord] = []
            for n, r in enumerate(iter_records(shp, info.encoding, ad.shape_types)):
                records.append(r)
                if n % 1000 == 0:
                    control.step("read", n, info.count)
            summary = ad.summarize(records, mapping)
            summary["crs_code"] = validation["crs"]
            summary["missing_components"] = info.missing_components
            summary["null_fields"] = {lf: sum(1 for r in records if not mapping.get(r.attrs, lf))
                                      for lf in mapping.columns}
            with self.db.transaction():
                version = int(self.db.query_one(f"SELECT COALESCE(MAX(version),0) FROM {ad.layer_table} WHERE name=?",
                                                (name,))[0]) + 1
                rows = []
                for n, r in enumerate(records):
                    rows.append(ad.row(lid, r, mapping))
                    if n % 1000 == 0:
                        control.step("validate", n, len(records))
                sources = []
                for _, path in sorted(components(shp).items()):
                    existed = self._stored_exists(path, ad.store_category)
                    stored = self.ws.store.import_file(path, None, ad.store_category)
                    if not existed:
                        created.append(self.ws.store.root / stored.relative_path)
                    sources.append({"file": path.name, "stored_path": stored.relative_path, "sha256": stored.sha256,
                                    "size_bytes": stored.size_bytes})
                control.step("store", len(records), len(records))
                self.db.execute(
                    f"INSERT INTO {ad.layer_table}(id,name,version,crs_code,crs_wkt,encoding,mapping_json,source_json,"
                    "summary_json,record_count,imported_at,imported_by,active,status,validation_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,'staged',?)",
                    (lid, name, version, validation["crs"], info.crs_wkt, info.encoding,
                     json.dumps(mapping.to_dict(), ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
                     json.dumps(summary, ensure_ascii=False, default=str), len(records), now_iso(), user.username,
                     json.dumps(validation, ensure_ascii=False)))
                for start in range(0, len(rows), 5000):
                    self.db.conn.executemany(ad.insert_sql(), rows[start:start + 5000])
                    control.step("write", start, len(rows))
                self.ws.audit.record(user, "stage_dataset", ad.layer_table, lid, None, kind=kind, name=name,
                                     version=version, records=len(records), crs=validation["crs"],
                                     sources=[(s["file"], s["sha256"]) for s in sources])
        except BaseException as exc:
            for path in created:  # copies made by this attempt only; shared identical files are kept
                path.unlink(missing_ok=True)
            try:
                with self.db.transaction():
                    self.ws.audit.record(user, "stage_dataset_failed", ad.layer_table, lid, None, kind=kind,
                                         source=str(Path(shp).name), error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - the original error is the one to report
                pass
            raise
        comparison = self.compare(kind, lid)
        with self.db.transaction():
            validation["comparison"] = comparison
            self.db.execute(f"UPDATE {ad.layer_table} SET validation_json=? WHERE id=?",
                            (json.dumps(validation, ensure_ascii=False, default=str), lid))
        return self.version(kind, lid)

    def _stored_exists(self, path: Path, category: str) -> bool:
        from .filestore import safe_name, sha256_file

        target = self.ws.store._target_dir(None, category) / f"{sha256_file(path)[:12]}_{safe_name(path.name)}"
        return target.exists()

    # -- compare ----------------------------------------------------------------------------

    def compare(self, kind: str, layer_id: str, against_id: str | None = None) -> dict[str, Any]:
        """Differences between ``layer_id`` and ``against_id`` (default: the active version)."""
        ad = self.adapter(kind)
        new = self.version(kind, layer_id)
        old = self.version(kind, against_id) if against_id else self.active(kind)
        out: dict[str, Any] = {"against": None if old is None else {"id": old.id, "label": old.label,
                                                                    "status": old.status},
                               "records": {"old": old.record_count if old else 0, "new": new.record_count}}
        if old is None or old.id == new.id:
            return out
        out["identical_sources"] = sorted(s["sha256"] for s in old.sources) == sorted(s["sha256"] for s in new.sources)
        out["crs"] = {"old": old.crs_code, "new": new.crs_code, "changed": old.crs_code != new.crs_code}
        out["mapping_changed"] = old.mapping.columns != new.mapping.columns
        out["mapping"] = {"old": old.mapping.columns, "new": new.mapping.columns}
        out["summary"] = {k: {"old": old.summary.get(k), "new": new.summary.get(k)}
                          for k in sorted(set(old.summary) | set(new.summary))
                          if isinstance(new.summary.get(k, old.summary.get(k)), (int, float))
                          and old.summary.get(k) != new.summary.get(k)}
        out.update(self._diff(ad, old, new))
        return out

    def _feature_index(self, ad: Adapter, layer_id: str, use_alt: bool) -> dict[str, list[tuple[str, str, Any]]]:
        idx: dict[str, list[tuple[str, str, Any]]] = defaultdict(list)
        for ident, alt, wkb, attrs in self.db.query(ad.identity_sql(), (layer_id,)):
            key = (alt if use_alt else ident) or ""
            gh = hashlib.sha256(wkb).hexdigest() if wkb is not None else ""
            ah = hashlib.sha256((attrs or "").encode("utf-8")).hexdigest()
            idx[key].append((gh, ah, wkb))
        return idx

    def _diff(self, ad: Adapter, old: DatasetVersion, new: DatasetVersion) -> dict[str, Any]:
        # Identity: primary identifier (GLOBALID / zone code / road id) when mapped in both versions,
        # otherwise the alternative one (DLS key / parcel key / name), otherwise geometry only.
        prim = {"cadastre": "globalid", "zoning": "zone_id", "roads": "road_id"}[ad.kind]
        alt = {"cadastre": "key", "zoning": "key", "roads": "name"}[ad.kind]
        if old.mapping.columns.get(prim) and new.mapping.columns.get(prim):
            basis, use_alt = prim, False
        elif old.mapping.columns.get(alt) and new.mapping.columns.get(alt):
            basis, use_alt = alt, True
        else:
            basis, use_alt = "geometry", False
        a = self._feature_index(ad, old.id, use_alt)
        b = self._feature_index(ad, new.id, use_alt)
        if basis == "geometry" or ad.kind == "zoning":
            # no unique identifiers: compare the multiset of (identifier, geometry) pairs
            ca = Counter((k, gh) for k, v in a.items() for gh, _, _ in v)
            cb = Counter((k, gh) for k, v in b.items() for gh, _, _ in v)
            res: dict[str, Any] = {"basis": basis if ad.kind != "zoning" else "zone code + geometry",
                                   "added": sum((cb - ca).values()), "removed": sum((ca - cb).values()),
                                   "unchanged": sum((ca & cb).values())}
            if ad.kind == "zoning":
                za, zb = set(a) - {""}, set(b) - {""}
                res["zone_codes_added"] = sorted(zb - za, key=lambda c: (len(c), c))
                res["zone_codes_removed"] = sorted(za - zb, key=lambda c: (len(c), c))
            return res
        ka, kb = set(a) - {""}, set(b) - {""}
        geom_changed, attr_changed, suspicious = [], [], []
        for k in sorted(ka & kb):
            ga, gb = sorted(x[0] for x in a[k]), sorted(x[0] for x in b[k])
            if ga != gb:
                geom_changed.append(k)
                if len(a[k]) == 1 and len(b[k]) == 1 and a[k][0][2] is not None and b[k][0][2] is not None:
                    s = _shift(shapely_wkb.loads(a[k][0][2]), shapely_wkb.loads(b[k][0][2]))
                    if s["area_change_pct"] > AREA_CHANGE_PCT or s["centroid_shift_m"] > CENTROID_SHIFT_M:
                        suspicious.append({"id": k, **s})
            elif sorted(x[1] for x in a[k]) != sorted(x[1] for x in b[k]):
                attr_changed.append(k)
        res = {"basis": basis, "added": len(kb - ka), "removed": len(ka - kb), "geometry_changed": len(geom_changed),
               "attributes_changed": len(attr_changed), "unchanged": len(ka & kb) - len(geom_changed) - len(attr_changed),
               "without_identifier": {"old": len(a.get("", [])), "new": len(b.get("", []))},
               "examples": {"added": sorted(kb - ka)[:20], "removed": sorted(ka - kb)[:20],
                            "geometry_changed": geom_changed[:20], "attributes_changed": attr_changed[:20]},
               "suspicious_geometry_changes": sorted(suspicious, key=lambda s: -s["area_change_pct"])[:50],
               "suspicious_thresholds": {"area_change_pct": AREA_CHANGE_PCT, "centroid_shift_m": CENTROID_SHIFT_M}}
        if ad.kind == "cadastre" and basis == "globalid":
            res["identifier_changes"] = self._key_changes(old.id, new.id)
        return res

    def _key_changes(self, old_id: str, new_id_: str) -> list[dict[str, str]]:
        rows = self.db.query(
            "SELECT o.globalid AS g, o.dls_key AS ok, n.dls_key AS nk FROM cadastre_parcels o JOIN cadastre_parcels n "
            "ON n.layer_id=? AND n.globalid=o.globalid WHERE o.layer_id=? AND o.globalid<>'' AND o.dls_key<>n.dls_key "
            "LIMIT 200", (new_id_, old_id))
        return [{"globalid": r["g"], "old_key": r["ok"], "new_key": r["nk"]} for r in rows]

    # -- activation / rollback --------------------------------------------------------------

    def activate(self, kind: str, user: "User", layer_id: str, note: str = "", action: str = "activate_dataset"
                 ) -> DatasetVersion:
        ad = self.adapter(kind)
        user.require(ad.permission)
        target = self.version(kind, layer_id)
        if target.status not in ("staged", "inactive"):
            raise ValidationError(f"{target.label} is {target.status}; only staged or inactive versions can be "
                                  "activated.")
        previous = self.active(kind)
        with self.db.transaction():
            self.db.execute(f"UPDATE {ad.layer_table} SET active=0, status='inactive' WHERE active=1")
            self.db.execute(f"UPDATE {ad.layer_table} SET active=1, status='active', activated_at=?, activated_by=? "
                            "WHERE id=?", (now_iso(), user.username, layer_id))
            self.ws.audit.record(user, action, ad.layer_table, layer_id, None, kind=kind, version=target.version,
                                 name=target.name, previous=previous.id if previous else None,
                                 previous_version=previous.version if previous else None, note=note)
        self.db.execute(f"ANALYZE {ad.feature_table}")  # refresh query-planner statistics for the new rows
        return self.version(kind, layer_id)

    def rollback(self, kind: str, user: "User", note: str = "") -> DatasetVersion:
        """Re-activate the version that was active before the current one."""
        ad = self.adapter(kind)
        current = self.active(kind)
        r = self.db.query_one(
            f"SELECT id FROM {ad.layer_table} WHERE status='inactive' AND activated_at<>'' "
            f"{'AND activated_at<=?' if current else ''} ORDER BY activated_at DESC, rowid DESC LIMIT 1",
            (current.activated_at,) if current else ())
        if r is None:
            raise ValidationError("There is no earlier activated version to roll back to.")
        return self.activate(kind, user, r["id"], note, action="rollback_dataset")

    def discard(self, kind: str, user: "User", layer_id: str, note: str = "") -> None:
        ad = self.adapter(kind)
        user.require(ad.permission)
        v = self.version(kind, layer_id)
        if v.status != "staged":
            raise ValidationError("Only a staged (never activated) version can be discarded.")
        with self.db.transaction():
            self.db.execute(f"DELETE FROM {ad.feature_table} WHERE layer_id=?", (layer_id,))
            self.db.execute(f"UPDATE {ad.layer_table} SET status='discarded' WHERE id=?", (layer_id,))
            self.ws.audit.record(user, "discard_dataset", ad.layer_table, layer_id, None, kind=kind,
                                 version=v.version, note=note)

    def pins(self) -> dict[str, Any]:
        """Active version of every dataset kind, as recorded with calculations."""
        out = {}
        for kind in ADAPTERS:
            v = self.active(kind)
            if v is not None:
                out[kind] = {"id": v.id, "name": v.name, "version": v.version, "activated_at": v.activated_at}
        return out


def _shift(a, b) -> dict[str, float]:  # type: ignore[no-untyped-def]
    aa, ab = float(a.area), float(b.area)
    if aa > 0:
        pct = abs(ab - aa) / aa * 100.0
    else:
        pct = 0.0 if ab == 0 else math.inf
    return {"area_old_m2": aa, "area_new_m2": ab, "area_change_pct": pct,
            "centroid_shift_m": float(a.centroid.distance(b.centroid)) if not a.is_empty and not b.is_empty else 0.0}


def iter_kinds() -> Iterable[str]:
    return ADAPTERS.keys()
