"""Per-edge setback geometry.

Definition used (see TECHNICAL_DECISIONS.md TD-06): a point of the parcel is inside the
setback zone of boundary edge *i* when its Euclidean distance to that edge segment is
less than the required distance *d_i*. The buildable area is the parcel minus the union
of all edge zones. This is correct for irregular and non-convex parcels and for
different distances on different edges; it is **not** a simple negative buffer.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..core.errors import GeometryError, MissingRuleError, ValidationError
from .primitives import Point2, geom_parts

# Arc resolution for distance zones. Buffer arcs are inscribed polygons, so at reflex
# corners the buildable boundary may be closer than d by at most d*(1-cos(pi/(4*QUAD_SEGS))):
# for d = 10 m that is 0.0047 mm. Straight offsets are exact.
QUAD_SEGS = 256


@dataclass(frozen=True)
class BoundaryEdge:
    index: int
    start: Point2
    end: Point2
    boundary_type: str
    distance_m: float

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass
class SetbackResult:
    status: str  # "ok" | "multipart" | "empty"
    parcel: Polygon
    buildable: BaseGeometry
    setback_zone: BaseGeometry
    edges: list[BoundaryEdge]
    part_areas: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def buildable_area(self) -> float:
        return float(self.buildable.area)

    @property
    def zone_area(self) -> float:
        return float(self.setback_zone.area)


def parcel_edges(parcel: Polygon) -> list[tuple[Point2, Point2]]:
    """Edges in the vertex order of the boundary as drawn (indices are stable)."""
    if parcel.interiors:
        raise GeometryError("Parcel boundary must not contain holes.")
    coords = [(float(x), float(y)) for x, y in parcel.exterior.coords]
    return [(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]


def compute_setbacks(parcel: Polygon, edge_types: Sequence[str], distances: Mapping[str, float],
                     sliver_tolerance_m2: float = 0.01) -> SetbackResult:
    """Compute setback zone and buildable area.

    ``edge_types[i]`` is the boundary type confirmed by the user for edge *i*;
    ``distances`` maps each boundary type to its rule distance in metres. Any edge
    type without a rule value stops the calculation with ``MissingRuleError``.
    """
    if not parcel.is_valid or parcel.is_empty:
        raise GeometryError("Parcel polygon is invalid or empty.")
    raw_edges = parcel_edges(parcel)
    if len(edge_types) != len(raw_edges):
        raise ValidationError(
            f"Parcel has {len(raw_edges)} edges but {len(edge_types)} edge classifications were given."
        )
    missing_class = [i for i, t in enumerate(edge_types) if not str(t).strip()]
    if missing_class:
        raise ValidationError(
            "Every parcel edge must be classified (front/side/rear/...). Unclassified edges: "
            + ", ".join(str(i + 1) for i in missing_class)
        )
    edges: list[BoundaryEdge] = []
    for i, ((a, b), t) in enumerate(zip(raw_edges, edge_types)):
        if t not in distances or distances[t] is None:
            raise MissingRuleError(
                f"No setback value is defined for boundary type '{t}' (edge {i + 1}).", boundary_type=t
            )
        d = float(distances[t])
        if not math.isfinite(d) or d < 0:
            raise ValidationError(f"Setback for '{t}' must be a non-negative number, got {d}.")
        edges.append(BoundaryEdge(i, a, b, t, d))

    zones = [LineString([e.start, e.end]).buffer(e.distance_m, quad_segs=QUAD_SEGS)
             for e in edges if e.distance_m > 0]
    zone_union = unary_union(zones) if zones else Polygon()
    setback_zone = parcel.intersection(zone_union) if zones else Polygon()
    buildable = parcel.difference(zone_union) if zones else parcel

    warnings: list[str] = []
    parts = geom_parts(buildable)
    part_areas = sorted((float(p.area) for p in parts), reverse=True)
    if not parts or buildable.area <= sliver_tolerance_m2:
        status = "empty"
        warnings.append("The required setbacks leave no buildable area inside the parcel.")
    elif len(parts) > 1:
        status = "multipart"
        warnings.append(
            f"Narrow sections split the buildable area into {len(parts)} separate parts: "
            + ", ".join(f"{a:.2f} m²" for a in part_areas)
        )
    else:
        status = "ok"
    slivers = [a for a in part_areas if a <= sliver_tolerance_m2]
    if slivers and status != "empty":
        warnings.append(f"{len(slivers)} buildable part(s) are smaller than {sliver_tolerance_m2} m² "
                        "(slivers); they are kept and must be reviewed.")
    if any(e.distance_m == 0 for e in edges):
        zero = [str(e.index + 1) for e in edges if e.distance_m == 0]
        warnings.append("Rule value is 0 m for edge(s) " + ", ".join(zero) + ".")
    return SetbackResult(status, parcel, buildable, setback_zone, edges, part_areas, warnings)


def suggest_edge_types(parcel: Polygon, roads: Sequence[BaseGeometry], max_distance_m: float = 1.0
                       ) -> list[str | None]:
    """Suggest 'front' for edges lying along a road; other edges get no suggestion.

    Suggestions must be confirmed by the user; they are never applied automatically.
    """
    if not roads:
        return [None] * len(parcel_edges(parcel))
    road_union = unary_union(list(roads))
    out: list[str | None] = []
    for a, b in parcel_edges(parcel):
        mid = LineString([a, b]).interpolate(0.5, normalized=True)
        out.append("front" if mid.distance(road_union) <= max_distance_m else None)
    return out
