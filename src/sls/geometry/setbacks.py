"""Per-edge setback geometry.

Definition used (see TECHNICAL_DECISIONS.md TD-06): a point of the parcel is inside the
setback zone of boundary edge *i* when its Euclidean distance to that edge segment is
less than the required distance *d_i*. The buildable area is the parcel minus the union
of all edge zones. This is correct for irregular and non-convex parcels and for
different distances on different edges; it is **not** a simple negative buffer.

A second method, ``compute_setbacks_offset`` (TD-26), follows the surveyor's CAD workflow:
explode the boundary into edges, offset each edge inward by its own distance, trim/extend
consecutive offsets to their intersection (sharp corners, also at reflex corners) and remove
the loops left by collapsed edges or closed narrow sections.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from ..core.errors import GeometryError, MissingRuleError, ValidationError
from .primitives import Point2, geom_parts

# Arc resolution for distance zones. Buffer arcs are inscribed polygons, so at reflex
# corners the buildable boundary may be closer than d by at most d*(1-cos(pi/(4*QUAD_SEGS))):
# for d = 10 m that is 0.0047 mm. Straight offsets are exact.
QUAD_SEGS = 256

METHOD_DISTANCE_ZONE = "distance_zone"   # TD-06: parcel − ⋃ buffer(edge_i, d_i); arcs at reflex corners
METHOD_OFFSET_CORNER = "offset_corner"   # TD-26: individual inward offsets, trim/extend, sharp corners
SETBACK_METHODS = (METHOD_OFFSET_CORNER, METHOD_DISTANCE_ZONE)

# Consecutive edges turning by less than this angle and having different distances are joined
# by a perpendicular step at their shared vertex instead of a far-away line intersection.
JOG_ANGLE_DEG = 0.5
# Corners whose intersection point lies further than this multiple of the largest setback from
# their boundary vertex are listed for review (very acute or very sharp reflex corners).
MITRE_REVIEW_FACTOR = 5.0
# Smallest area inside a setback strip treated as real (smaller parts are floating-point noise
# along the offset lines).
EXCESS_MIN_M2 = 1e-4


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
    method: str = METHOD_DISTANCE_ZONE
    corners: list[Point2] = field(default_factory=list)  # offset-corner method: trimmed/extended corner points

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
    edges = _classified_edges(parcel, edge_types, distances)
    zones = [LineString([e.start, e.end]).buffer(e.distance_m, quad_segs=QUAD_SEGS)
             for e in edges if e.distance_m > 0]
    zone_union = unary_union(zones) if zones else Polygon()
    setback_zone = parcel.intersection(zone_union) if zones else Polygon()
    buildable = parcel.difference(zone_union) if zones else parcel
    return _result(parcel, buildable, setback_zone, edges, sliver_tolerance_m2)


def _classified_edges(parcel: Polygon, edge_types: Sequence[str], distances: Mapping[str, float]
                      ) -> list[BoundaryEdge]:
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
    return edges


def _result(parcel: Polygon, buildable: BaseGeometry, setback_zone: BaseGeometry, edges: list[BoundaryEdge],
            sliver_tolerance_m2: float, warnings: list[str] | None = None) -> SetbackResult:
    warnings = list(warnings or [])
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
        warnings.append("Setback is 0 m for edge(s) " + ", ".join(zero) + ".")
    return SetbackResult(status, parcel, buildable, setback_zone, edges, part_areas, warnings)


def compute_setbacks_offset(parcel: Polygon, edge_types: Sequence[str], distances: Mapping[str, float],
                            sliver_tolerance_m2: float = 0.01) -> SetbackResult:
    """Surveyor workflow: explode → individual inward offsets → trim/extend → sharp corners.

    Same inputs, validation and outcomes (ok / multipart / empty) as ``compute_setbacks``.
    1. Each edge *i* is offset inward, parallel to itself, by its own distance d_i.
    2. Consecutive offset lines are trimmed/extended to their intersection, giving a sharp
       corner at convex and reflex vertices alike. Nearly collinear edges (turn below
       ``JOG_ANGLE_DEG``) with different distances get a perpendicular step at their vertex.
    3. Where a short edge's offset collapses, or offsets of opposite edges cross (narrow
       sections), the offset ring loops over itself. The ring is noded and only faces it winds
       around in the parcel's own direction are kept: the neighbouring offsets then meet at
       their own intersection, exactly as a trim in CAD would leave them.
    4. The result is clipped to the parcel and checked against every edge's inward strip (the
       band of width d_i between the edge and its offset, square-ended); any part inside a
       strip is removed and reported. Unlike ``compute_setbacks`` there are no arcs: a reflex
       corner excludes the full d×d corner, and at a shallow convex corner between different
       setbacks no round cap is drawn around the vertex (trim/extend convention).
    """
    edges = _classified_edges(parcel, edge_types, distances)
    if all(e.distance_m == 0 for e in edges):
        return _result(parcel, parcel, Polygon(), edges, sliver_tolerance_m2)
    orient = 1.0 if parcel.exterior.is_ccw else -1.0
    n = len(edges)
    units, normals = [], []
    for e in edges:
        ux, uy = (e.end[0] - e.start[0]) / e.length, (e.end[1] - e.start[1]) / e.length
        units.append((ux, uy))
        normals.append((-uy * orient, ux * orient))  # inward: left of a CCW ring, right of a CW ring
    jog_sin = math.sin(math.radians(JOG_ANGLE_DEG))
    d_max = max(e.distance_m for e in edges)
    ring: list[Point2] = []
    corners: list[Point2] = []
    jogs, far = [], []
    for i in range(n):
        a, b = edges[i - 1], edges[i]
        ua, ub, na, nb = units[i - 1], units[i], normals[i - 1], normals[i]
        v = b.start
        cross = ua[0] * ub[1] - ua[1] * ub[0]
        pa = (v[0] + na[0] * a.distance_m, v[1] + na[1] * a.distance_m)
        pb = (v[0] + nb[0] * b.distance_m, v[1] + nb[1] * b.distance_m)
        if abs(cross) < 1e-12 or (abs(cross) < jog_sin and abs(a.distance_m - b.distance_m) > 1e-9):
            # Parallel (or nearly collinear with different distances): a step at the vertex,
            # perpendicular to the edge with the larger setback (along that edge's strip end).
            nn = na[0] * nb[0] + na[1] * nb[1]
            if math.dist(pa, pb) < 1e-9:
                pts = [pa]
            elif nn <= 0:
                pts = [pa, pb]
            elif a.distance_m >= b.distance_m:
                t = b.distance_m / nn
                pts = [pa, (v[0] + na[0] * t, v[1] + na[1] * t)]
            else:
                t = a.distance_m / nn
                pts = [(v[0] + nb[0] * t, v[1] + nb[1] * t), pb]
            if len(pts) == 2:
                jogs.append(str(i + 1))
        else:
            # Trim/extend: intersection of offset line a (through pa, along ua) and b (through pb, along ub).
            t = ((pb[0] - pa[0]) * ub[1] - (pb[1] - pa[1]) * ub[0]) / cross
            pts = [(pa[0] + ua[0] * t, pa[1] + ua[1] * t)]
            if d_max > 0 and math.dist(pts[0], v) > MITRE_REVIEW_FACTOR * d_max:
                far.append(f"{i + 1} ({math.dist(pts[0], v):.2f} m)")
        corners += pts
        for q in pts:
            if not ring or math.dist(ring[-1], q) > 1e-9:
                ring.append(q)
    if len(ring) > 1 and math.dist(ring[0], ring[-1]) <= 1e-9:
        ring.pop()
    # Inward strip of each edge: its band of width d_i between the edge and its offset line,
    # with square ends (no arcs). Nothing buildable may lie inside any strip.
    strip_list = [(e, LineString([e.start, e.end]).buffer(orient * e.distance_m, single_sided=True))
                  for e in edges if e.distance_m > 0]
    strips = unary_union([g for _, g in strip_list])
    kept: list[BaseGeometry] = []
    if len(ring) >= 3:
        noded = unary_union(LineString(ring + [ring[0]]))
        faces = [f for f in polygonize(noded) if f.area > 0]
        if faces:
            rps = [f.representative_point() for f in faces]
            xs, ys = np.array([q.x for q in rps]), np.array([q.y for q in rps])
            # Loops of collapsed edges wind the wrong way; a fully inverted offset keeps the
            # winding but lies inside the strips of the edges that produced it.
            ok = (_winding(ring, xs, ys) * orient > 0) & ~shapely.contains_xy(strips, xs, ys)
            kept = [f for f, k in zip(faces, ok) if k]
    buildable: BaseGeometry = unary_union(kept).intersection(parcel) if kept else Polygon()
    warnings: list[str] = []
    # Parts of the result inside a strip. The boundary runs exactly along the strips' inner sides,
    # so floating-point noise leaves zero-area slivers there; only parts of at least
    # EXCESS_MIN_M2 are real and cut away.
    excess = [g for g in geom_parts(buildable.intersection(strips)) if g.area >= EXCESS_MIN_M2]
    if excess:
        tree = STRtree([g for _, g in strip_list])
        cut_idx = sorted({int(j) for x in excess for j in tree.query(x, predicate="intersects")
                          if x.intersection(strip_list[int(j)][1]).area >= EXCESS_MIN_M2})
        buildable = buildable.difference(unary_union(excess))
        warnings.append(f"Trim/extend corners left {sum(g.area for g in excess):.2f} m² inside the setback strip of "
                        "edge(s) " + ", ".join(str(strip_list[j][0].index + 1) for j in cut_idx)
                        + "; it was cut along those strips (perpendicular step where the larger setback begins). "
                        "Review these edges.")
    buildable = _polygonal(buildable)
    if jogs:
        warnings.append("Different setbacks on nearly collinear edges: perpendicular step at vertex(es) "
                        + ", ".join(jogs) + ".")
    if far:
        warnings.append("Sharp-corner intersection lies far from the boundary vertex at vertex(es) "
                        + ", ".join(far) + "; review these corners.")
    setback_zone = _polygonal(parcel.difference(buildable))
    res = _result(parcel, buildable, setback_zone, edges, sliver_tolerance_m2, warnings)
    res.method, res.corners = METHOD_OFFSET_CORNER, corners
    return res


def _winding(ring: Sequence[Point2], xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Winding numbers of the closed ring around points (xs, ys) (positive = counter-clockwise)."""
    r = np.asarray(ring, dtype=float)
    x0, y0 = r[:, 0], r[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    out = np.zeros(len(xs), dtype=int)
    for start in range(0, len(xs), 256):  # bounded memory: 256 points × ring vertices per block
        px, py = xs[start:start + 256, None], ys[start:start + 256, None]
        is_left = (x1 - x0) * (py - y0) - (px - x0) * (y1 - y0)
        up = (y0 <= py) & (py < y1) & (is_left > 0)
        down = (y1 <= py) & (py < y0) & (is_left < 0)
        out[start:start + 256] = up.sum(axis=1) - down.sum(axis=1)
    return out


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Drop the zero-area lines/points that clipping can leave next to the polygons."""
    parts = geom_parts(geom)
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else unary_union(parts)


def setback_function(method: str):  # type: ignore[no-untyped-def]
    if method == METHOD_OFFSET_CORNER:
        return compute_setbacks_offset
    if method == METHOD_DISTANCE_ZONE:
        return compute_setbacks
    raise ValidationError(f"Unknown setback method '{method}'.")


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
