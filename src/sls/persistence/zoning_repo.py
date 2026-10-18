"""Zoning register: land-use zone polygons (ZONE_ID per polygon) and parcel/zone overlay.

The zones of a parcel are computed geometrically (parcel ∩ zone polygons), not from key
matching, so parcels split by a zone boundary report every part with its area. Very small
overlaps from digitising mismatches are reported separately as slivers, never as a zone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shapely import wkb as shapely_wkb
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..gis.shapefile_layer import FieldMapping

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace

SLIVER_M2 = 1.0      # overlaps below this area ...
SLIVER_PCT = 0.5     # ... or below this share of the parcel are slivers


def _code(value: Any) -> str:
    """ZONE_ID as text matching the rule table ('3', not '3.0')."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


@dataclass(frozen=True)
class ZoneShare:
    zone_id: str
    zone_name: str
    area_m2: float
    pct: float


@dataclass
class ZoneOverlay:
    parcel_area_m2: float
    zones: list[ZoneShare] = field(default_factory=list)       # significant shares, largest first
    slivers: list[ZoneShare] = field(default_factory=list)     # digitising overlaps, not zones
    uncovered_m2: float = 0.0                                   # parcel area outside any zone polygon
    unzoned: list[ZoneShare] = field(default_factory=list)     # zone polygons without a code (ZONE_ID 0/blank)

    @property
    def single_zone(self) -> ZoneShare | None:
        return self.zones[0] if len(self.zones) == 1 and not self.unzoned else None

    def to_dict(self) -> dict[str, Any]:
        return {"parcel_area_m2": self.parcel_area_m2, "uncovered_m2": self.uncovered_m2,
                "zones": [z.__dict__ for z in self.zones], "slivers": [z.__dict__ for z in self.slivers],
                "unzoned": [z.__dict__ for z in self.unzoned]}


class ZoningRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def import_layer(self, user: "User", shp: Path, mapping: FieldMapping, name: str = "",
                     declared_crs: str = "") -> dict[str, Any]:
        """Stage and immediately activate a zoning layer (see ``DatasetManager`` for the staged workflow)."""
        staged = self.ws.datasets.stage("zoning", user, shp, mapping, name, declared_crs)
        self.ws.datasets.activate("zoning", user, staged.id, "imported and activated in one step")
        s = staged.summary
        return {"id": staged.id, "name": staged.name, "version": staged.version, "records": s["records"],
                "zones": s["zones"], "invalid": s["invalid"], "without_zone": s["without_zone"],
                "crs_code": staged.crs_code, "codes": s["codes"]}

    def active_layer(self) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM zoning_layers WHERE active=1 ORDER BY activated_at DESC LIMIT 1")
        if r is None:
            return None
        return {"id": r["id"], "name": r["name"], "version": r["version"], "crs_code": r["crs_code"],
                "record_count": r["record_count"], "imported_at": r["imported_at"],
                "summary": json.loads(r["summary_json"]),
                "label": f"{r['name']} v{r['version']} ({r['record_count']})"}

    def overlay(self, geom: BaseGeometry, layer_id: str | None = None) -> ZoneOverlay | None:
        """Zones covering ``geom`` with area and share; None when no zoning layer is imported."""
        layer = {"id": layer_id} if layer_id else self.active_layer()
        if layer is None:
            return None
        x0, y0, x1, y1 = geom.bounds
        rows = self.db.query("SELECT zone_id, zone_name, wkb FROM zoning_pieces WHERE layer_id=? AND maxx>=? AND "
                             "minx<=? AND maxy>=? AND miny<=? AND wkb IS NOT NULL", (layer["id"], x0, x1, y0, y1))
        total = float(geom.area)
        if total <= 0:
            return ZoneOverlay(0.0)
        by_zone: dict[tuple[str, str], list[BaseGeometry]] = {}
        covered: list[BaseGeometry] = []
        for r in rows:
            piece = shapely_wkb.loads(r["wkb"])
            if not piece.is_valid:
                piece = piece.buffer(0)  # used only for measuring overlap; stored geometry is unchanged
            inter = geom.intersection(piece)
            if inter.is_empty or inter.area <= 0:
                continue
            by_zone.setdefault((r["zone_id"], r["zone_name"]), []).append(inter)
            covered.append(inter)
        out = ZoneOverlay(total)
        for (zid, zname), parts in by_zone.items():
            area = float(unary_union(parts).area)
            share = ZoneShare(zid, zname, area, 100.0 * area / total)
            if area < SLIVER_M2 or share.pct < SLIVER_PCT:
                out.slivers.append(share)
            elif not zid:
                out.unzoned.append(share)
            else:
                out.zones.append(share)
        out.zones.sort(key=lambda z: -z.area_m2)
        out.uncovered_m2 = max(0.0, total - float(unary_union(covered).area)) if covered else total
        return out
