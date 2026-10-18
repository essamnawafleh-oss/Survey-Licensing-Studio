"""Road / street reference layers imported from GIS (see datasets.RoadAdapter).

Roads are reference data: they are shown, queried and may support *suggestions* (e.g. which
parcel edge faces a road), but they never replace a surveyed or registered parcel boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shapely import wkb as shapely_wkb
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from ..core.errors import ValidationError

if TYPE_CHECKING:
    from .workspace import Workspace


@dataclass(frozen=True)
class RoadFeature:
    id: int
    layer_id: str
    fid: int
    road_id: str
    name: str
    road_class: str
    width: str
    attrs: dict[str, Any]
    geometry: BaseGeometry | None
    geom_type: str
    length_m: float | None
    area_m2: float | None
    valid: bool
    issue: str


class RoadRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def _feature(self, r) -> RoadFeature:  # type: ignore[no-untyped-def]
        g = shapely_wkb.loads(r["wkb"]) if r["wkb"] is not None else None
        return RoadFeature(r["id"], r["layer_id"], r["fid"], r["road_id"], r["name"], r["road_class"], r["width"],
                           json.loads(r["attrs_json"]), g, r["geom_type"], r["length_m"], r["area_m2"],
                           bool(r["valid"]), r["issue"])

    def _layer_id(self, layer_id: str | None) -> str | None:
        if layer_id:
            return layer_id
        v = self.ws.datasets.active("roads")
        return v.id if v else None

    def get(self, feature_id: int) -> RoadFeature:
        r = self.db.query_one("SELECT * FROM road_features WHERE id=?", (feature_id,))
        if r is None:
            raise ValidationError(f"Road feature not found: {feature_id}")
        return self._feature(r)

    def in_window(self, x0: float, y0: float, x1: float, y1: float, layer_id: str | None = None,
                  limit: int = 5000) -> list[RoadFeature]:
        lid = self._layer_id(layer_id)
        if lid is None:
            return []
        rows = self.db.query("SELECT * FROM road_features WHERE layer_id=? AND maxx>=? AND minx<=? AND maxy>=? "
                             "AND miny<=? LIMIT ?", (lid, x0, x1, y0, y1, limit))
        win = box(x0, y0, x1, y1)
        return [f for f in map(self._feature, rows) if f.geometry is not None and f.geometry.intersects(win)]

    def near(self, geom: BaseGeometry, distance: float, layer_id: str | None = None) -> list[tuple[RoadFeature, float]]:
        """Road features within ``distance`` (layer units) of ``geom``, nearest first."""
        x0, y0, x1, y1 = geom.bounds
        hits = self.in_window(x0 - distance, y0 - distance, x1 + distance, y1 + distance, layer_id)
        out = [(f, float(f.geometry.distance(geom))) for f in hits if f.geometry is not None]
        return sorted([x for x in out if x[1] <= distance], key=lambda x: x[1])

    def find(self, text: str, layer_id: str | None = None, limit: int = 200) -> list[RoadFeature]:
        lid = self._layer_id(layer_id)
        if lid is None:
            return []
        like = f"%{text.strip()}%"
        rows = self.db.query("SELECT * FROM road_features WHERE layer_id=? AND (road_id=? OR name LIKE ?) LIMIT ?",
                             (lid, text.strip(), like, limit))
        return [self._feature(r) for r in rows]
