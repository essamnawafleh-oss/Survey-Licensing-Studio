"""Cadastral parcel register imported from GIS parcel layers (shared by all cases).

Each import is a new immutable layer version; cases that used a parcel keep a geometry
snapshot and the layer version, so later imports never change earlier cases.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shapely import wkb as shapely_wkb
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

from ..core.errors import ValidationError
from ..gis.shapefile_layer import FieldMapping

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


@dataclass(frozen=True)
class CadastreLayer:
    id: str
    name: str
    version: int
    crs_code: str
    encoding: str
    mapping: FieldMapping
    summary: dict[str, Any]
    record_count: int
    imported_at: str
    imported_by: str
    sources: list[dict[str, Any]]

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version} ({self.record_count} parcels)"


@dataclass(frozen=True)
class CadastreParcel:
    id: int
    layer_id: str
    fid: int
    dls_key: str
    village: str
    basin: str
    sector: str
    sheet: str
    parcel_no: str
    globalid: str
    attrs: dict[str, Any]
    geometry: BaseGeometry | None
    area_m2: float | None
    parts: int
    valid: bool
    issue: str

    @property
    def label(self) -> str:
        return f"{self.village}-{self.basin}-{self.sector}-{self.parcel_no}"


class CadastreRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    # -- import ------------------------------------------------------------------------

    def import_layer(self, user: "User", shp: Path, mapping: FieldMapping, name: str = "",
                     progress=None, declared_crs: str = "") -> CadastreLayer:  # type: ignore[no-untyped-def]
        """Stage and immediately activate a parcel layer (see ``DatasetManager`` for the staged workflow)."""
        from .datasets import ImportControl

        control = ImportControl(progress=(lambda _stage, n, total: progress(n, total)) if progress else None)
        staged = self.ws.datasets.stage("cadastre", user, shp, mapping, name, declared_crs, control)
        self.ws.datasets.activate("cadastre", user, staged.id, "imported and activated in one step")
        return self.layer(staged.id)

    # -- queries -------------------------------------------------------------------------

    def layer(self, layer_id: str) -> CadastreLayer:
        r = self.db.query_one("SELECT * FROM cadastre_layers WHERE id=?", (layer_id,))
        if r is None:
            raise ValidationError(f"Parcel layer not found: {layer_id}")
        return CadastreLayer(r["id"], r["name"], r["version"], r["crs_code"], r["encoding"],
                             FieldMapping.from_dict(json.loads(r["mapping_json"])), json.loads(r["summary_json"]),
                             r["record_count"], r["imported_at"], r["imported_by"], json.loads(r["source_json"]))

    def active_layer(self) -> CadastreLayer | None:
        r = self.db.query_one("SELECT id FROM cadastre_layers WHERE active=1 ORDER BY activated_at DESC LIMIT 1")
        return self.layer(r["id"]) if r else None

    def layers(self) -> list[CadastreLayer]:
        return [self.layer(r["id"]) for r in self.db.query("SELECT id FROM cadastre_layers WHERE status<>'discarded' "
                                                           "ORDER BY imported_at DESC")]

    def _parcel(self, r) -> CadastreParcel:  # type: ignore[no-untyped-def]
        g = shapely_wkb.loads(r["wkb"]) if r["wkb"] is not None else None
        return CadastreParcel(r["id"], r["layer_id"], r["fid"], r["dls_key"], r["village"], r["basin"], r["sector"],
                              r["sheet"], r["parcel_no"], r["globalid"], json.loads(r["attrs_json"]), g, r["area_m2"],
                              r["parts"], bool(r["valid"]), r["issue"])

    def _layer_id(self, layer_id: str | None) -> str | None:
        if layer_id:
            return layer_id
        lay = self.active_layer()
        return lay.id if lay else None

    def distinct(self, column: str, layer_id: str | None = None, **filters: str) -> list[str]:
        if column not in ("village", "basin", "sector", "parcel_no", "sheet"):
            raise ValueError(column)
        lid = self._layer_id(layer_id)
        if lid is None:
            return []
        where = ["layer_id=?"] + [f"{k}=?" for k in filters]
        rows = self.db.query(f"SELECT DISTINCT {column} AS v FROM cadastre_parcels WHERE {' AND '.join(where)} "
                             f"ORDER BY {column}", (lid, *filters.values()))
        return [r["v"] for r in rows]

    def find(self, village: str = "", basin: str = "", sector: str = "", parcel_no: str = "", key: str = "",
             layer_id: str | None = None) -> list[CadastreParcel]:
        lid = self._layer_id(layer_id)
        if lid is None:
            return []
        clauses, params = ["layer_id=?"], [lid]
        for col, val in (("village", village), ("basin", basin), ("sector", sector), ("dls_key", key)):
            if val:
                clauses.append(f"{col}=?")
                params.append(val.strip())
        if parcel_no:
            # Parcel numbers are zero-padded in the register ("00351"); accept "351" too.
            clauses.append("(parcel_no=? OR ltrim(parcel_no,'0')=ltrim(?,'0'))")
            params += [parcel_no.strip(), parcel_no.strip()]
        rows = self.db.query(f"SELECT * FROM cadastre_parcels WHERE {' AND '.join(clauses)} ORDER BY area_m2 DESC "
                             "LIMIT 200", tuple(params))
        return [self._parcel(r) for r in rows]

    def get(self, parcel_id: int) -> CadastreParcel:
        r = self.db.query_one("SELECT * FROM cadastre_parcels WHERE id=?", (parcel_id,))
        if r is None:
            raise ValidationError(f"Parcel not found: {parcel_id}")
        return self._parcel(r)

    def in_window(self, x0: float, y0: float, x1: float, y1: float, layer_id: str | None = None,
                  limit: int = 5000) -> list[CadastreParcel]:
        lid = self._layer_id(layer_id)
        if lid is None:
            return []
        rows = self.db.query("SELECT * FROM cadastre_parcels WHERE layer_id=? AND maxx>=? AND minx<=? AND maxy>=? "
                             "AND miny<=? LIMIT ?", (lid, x0, x1, y0, y1, limit))
        win = box(x0, y0, x1, y1)
        return [p for p in map(self._parcel, rows) if p.geometry is not None and p.geometry.intersects(win)]

    def at_point(self, x: float, y: float, layer_id: str | None = None) -> list[CadastreParcel]:
        """Parcels whose polygon contains the point, smallest first (nested/overlapping data exists)."""
        pt = Point(x, y)
        hits = [p for p in self.in_window(x, y, x, y, layer_id) if p.geometry.covers(pt)]
        return sorted(hits, key=lambda p: p.area_m2 or 0.0)
