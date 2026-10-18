"""Floor areas, setback intersections and wall/fence lengths (local metric frame)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

OVERLAP_TOLERANCE_M2 = 1e-4
LENGTH_TOLERANCE_M = 1e-4


@dataclass(frozen=True)
class Footprint:
    entity_id: str
    floor_key: str
    polygon: Polygon


@dataclass
class FloorArea:
    floor_key: str
    geometry: BaseGeometry
    area_m2: float
    sum_of_parts_m2: float
    overlap_m2: float
    entity_ids: list[str]
    part_count: int
    duplicate_entity_ids: list[str] = field(default_factory=list)


def floor_areas(footprints: Sequence[Footprint], floor_order: Sequence[str] | None = None
                ) -> list[FloorArea]:
    """Union footprints per floor so overlapping polygons are never double counted."""
    by_floor: dict[str, list[Footprint]] = {}
    for fp in footprints:
        by_floor.setdefault(fp.floor_key, []).append(fp)
    order = list(floor_order or [])
    keys = [k for k in order if k in by_floor] + sorted(k for k in by_floor if k not in order)
    result: list[FloorArea] = []
    for key in keys:
        fps = by_floor[key]
        union = unary_union([f.polygon for f in fps])
        total = sum(f.polygon.area for f in fps)
        duplicates: list[str] = []
        for i, a in enumerate(fps):
            for b in fps[:i]:
                if a.polygon.equals_exact(b.polygon, 1e-6) or (
                    a.polygon.symmetric_difference(b.polygon).area <= OVERLAP_TOLERANCE_M2
                ):
                    duplicates.append(a.entity_id)
                    break
        parts = len(getattr(union, "geoms", [union]))
        result.append(FloorArea(
            floor_key=key, geometry=union, area_m2=float(union.area), sum_of_parts_m2=float(total),
            overlap_m2=float(max(0.0, total - union.area)), entity_ids=[f.entity_id for f in fps],
            part_count=parts, duplicate_entity_ids=duplicates,
        ))
    return result


@dataclass
class Intersection:
    floor_key: str
    in_setback_zone: BaseGeometry
    outside_parcel: BaseGeometry

    @property
    def in_setback_zone_m2(self) -> float:
        return float(self.in_setback_zone.area)

    @property
    def outside_parcel_m2(self) -> float:
        return float(self.outside_parcel.area)


def setback_intersections(floor: FloorArea, parcel: Polygon, setback_zone: BaseGeometry) -> Intersection:
    """Geometric facts only: which part of a floor lies in the setback zone / outside the parcel.

    This says nothing about legal eligibility or fees (TD-07).
    """
    geom = floor.geometry
    in_zone = geom.intersection(setback_zone) if not setback_zone.is_empty else Polygon()
    outside = geom.difference(parcel)
    return Intersection(floor.floor_key, in_zone, outside)


@dataclass(frozen=True)
class LinearFeature:
    entity_id: str
    kind: str  # "wall" | "fence"
    line: LineString


@dataclass
class LengthTotal:
    kind: str
    length_m: float
    sum_of_parts_m: float
    overlap_m: float
    entity_ids: list[str]


def length_totals(features: Sequence[LinearFeature]) -> list[LengthTotal]:
    """Total length per kind; collinear overlapping segments are counted once."""
    by_kind: dict[str, list[LinearFeature]] = {}
    for f in features:
        by_kind.setdefault(f.kind, []).append(f)
    out: list[LengthTotal] = []
    for kind in sorted(by_kind):
        items = by_kind[kind]
        merged = unary_union([f.line for f in items])
        total = sum(f.line.length for f in items)
        out.append(LengthTotal(kind, float(merged.length), float(total),
                               float(max(0.0, total - merged.length)), [f.entity_id for f in items]))
    return out
