"""Surveyor setback construction: explode → individual inward offsets → trim/extend → sharp corners.

Reproduces the CAD workflow step by step and keeps every intermediate result inspectable:

1. The parcel is never modified; the construction works on its vertex list.
2. The closed boundary is exploded into ordered edges (vertex order and adjacency kept).
3. Each edge is offset inward by its own setback (the raw OFFSET segment, same length as the
   edge). The inward side follows the ring orientation, so clockwise input works the same.
4. At every vertex the two adjacent offset lines are joined:
   - ``intersection``: the unique intersection point (trim at convex, extend at reflex corners);
   - ``collinear``: parallel, same direction, same setback → the offsets are one line;
   - ``step`` / ``parallel_step``: (nearly) parallel with different setbacks → no usable unique
     intersection; a perpendicular step is drawn at the vertex along the end of the strip of
     the edge with the larger setback. Reported for review;
   - ``antiparallel``: the offsets run in opposite directions; joined at the vertex. Review.
   No arcs, fillets or bevels are ever added.
5. Each offset is trimmed/extended to its two joins; trimmed and extended portions are kept.
6. The joined ring is noded. Faces the ring winds around in the parcel's own direction and
   outside every edge's setback strip are kept; every other face is listed as discarded with
   its area and reason (loops of collapsed offsets, crossing offsets of narrow sections,
   inverted offsets). Nothing is dropped silently.
7. The kept area is clipped to the parcel and cut by the setback strips; both are reported
   when they change the result by at least ``AREA_TOL_M2``.
8. The final polygon(s) are validated before any area is reported.

All coordinates are in the local metric frame (metres, origin near the parcel), so the
tolerances below are absolute metric values. Coordinates are never rounded here.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.validation import explain_validity

from .primitives import Point2, geom_parts

if TYPE_CHECKING:
    from .setbacks import BoundaryEdge

CONSTRUCTION_VERSION = "2"

LINEAR_TOL_M = 1e-6        # points closer than this coincide; segments shorter are zero-length
PARALLEL_SIN = 1e-9        # |sin(angle between edges)| below this: parallel, no unique intersection
JOG_ANGLE_DEG = 0.5        # nearly collinear edges with different setbacks: step instead of a remote intersection
REMOTE_FACTOR = 5.0        # join further than this × the larger adjacent setback from its vertex: review
AREA_TOL_M2 = 1e-4         # area changes smaller than this (1 cm²) are floating-point noise

INFO, REVIEW, ERROR = "info", "review", "error"


@dataclass
class ConstructionIssue:
    code: str
    severity: str           # info | review | error
    message: str
    edges: list[int] = field(default_factory=list)      # 1-based edge numbers
    vertices: list[int] = field(default_factory=list)   # 1-based vertex numbers (start vertex of edge k)
    area_m2: float | None = None

    @property
    def key(self) -> str:
        """Stable identifier used when a reviewer resolves this item."""
        return f"{self.code}|e{','.join(map(str, self.edges))}|v{','.join(map(str, self.vertices))}"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "edges": self.edges,
                "vertices": self.vertices, "area_m2": self.area_m2, "key": self.key}


@dataclass
class VertexJoin:
    vertex: int             # 1-based; joins edge vertex-1 (previous) and edge vertex
    point: Point2           # boundary vertex
    kind: str               # intersection | collinear | step | parallel_step | antiparallel | zero
    points: list[Point2]    # join point(s): first on the previous edge's offset, last on the next one's
    distance_m: float       # largest distance of a join point from the vertex


@dataclass
class OffsetEdge:
    index: int              # 0-based
    start: Point2
    end: Point2
    boundary_type: str
    distance_m: float
    normal: Point2          # inward unit normal
    raw: tuple[Point2, Point2] = ((0.0, 0.0), (0.0, 0.0))        # OFFSET result (same length as the edge)
    trimmed_extended: tuple[Point2, Point2] = ((0.0, 0.0), (0.0, 0.0))  # after joining both neighbours
    start_action: str = "none"   # trim | extend | none
    end_action: str = "none"
    trimmed: list[tuple[Point2, Point2]] = field(default_factory=list)
    extended: list[tuple[Point2, Point2]] = field(default_factory=list)
    collapsed: bool = False
    used_length_m: float = 0.0   # length of the final boundary lying on this offset
    outcome: str = ""            # used | partly_used | collapsed | eliminated

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass
class OffsetConstruction:
    orientation: str                         # ccw | cw (as drawn)
    edges: list[OffsetEdge]
    joins: list[VertexJoin]
    ring: list[Point2]                       # joined offset ring before noding
    kept_faces: list[Polygon]
    discarded_faces: list[tuple[Polygon, str]]
    clipped: BaseGeometry                    # kept area outside the parcel, removed by clipping
    strip_cut: BaseGeometry                  # kept area inside a setback strip, removed
    buildable: BaseGeometry
    issues: list[ConstructionIssue]
    valid: bool

    @property
    def review_keys(self) -> list[str]:
        return sorted(i.key for i in self.issues if i.severity == REVIEW)

    def by_severity(self, severity: str) -> list[ConstructionIssue]:
        return [i for i in self.issues if i.severity == severity]

    def to_dict(self, pt: Callable[[Point2], Point2], wkt: Callable[[BaseGeometry], str]) -> dict[str, Any]:
        """JSON-ready, with coordinates converted to drawing units by ``pt`` / ``wkt``."""
        seg = lambda s: [list(pt(s[0])), list(pt(s[1]))]  # noqa: E731
        return {
            "version": CONSTRUCTION_VERSION, "orientation": self.orientation, "valid": self.valid,
            "edges": [{"index": e.index + 1, "boundary_type": e.boundary_type, "setback_m": e.distance_m,
                       "length_m": e.length, "raw_offset": seg(e.raw), "trimmed_extended": seg(e.trimmed_extended),
                       "start_action": e.start_action, "end_action": e.end_action,
                       "trimmed": [seg(s) for s in e.trimmed], "extended": [seg(s) for s in e.extended],
                       "collapsed": e.collapsed, "used_length_m": e.used_length_m, "outcome": e.outcome}
                      for e in self.edges],
            "joins": [{"vertex": j.vertex, "kind": j.kind, "points": [list(pt(p)) for p in j.points],
                       "distance_m": j.distance_m} for j in self.joins],
            "discarded": [{"wkt": wkt(f), "area_m2": float(f.area), "reason": r} for f, r in self.discarded_faces],
            "clipped_wkt": wkt(self.clipped), "strip_cut_wkt": wkt(self.strip_cut),
            "buildable_wkt": wkt(self.buildable),
            "issues": [i.to_dict() for i in self.issues],
        }


def build_offset_construction(parcel: Polygon, edges: Sequence["BoundaryEdge"]) -> OffsetConstruction:
    orient = 1.0 if parcel.exterior.is_ccw else -1.0
    issues: list[ConstructionIssue] = []
    oes: list[OffsetEdge] = []
    for e in edges:
        ux, uy = (e.end[0] - e.start[0]) / e.length, (e.end[1] - e.start[1]) / e.length
        n = (-uy * orient, ux * orient)  # inward: left of a counter-clockwise ring, right of a clockwise one
        d = e.distance_m
        raw = ((e.start[0] + n[0] * d, e.start[1] + n[1] * d), (e.end[0] + n[0] * d, e.end[1] + n[1] * d))
        oes.append(OffsetEdge(e.index, e.start, e.end, e.boundary_type, d, n, raw))
    zero = [e.index + 1 for e in oes if e.distance_m == 0]
    if zero:
        issues.append(ConstructionIssue("zero_setback", INFO, "Setback is 0 m for edge(s) "
                                        + ", ".join(map(str, zero)) + ".", zero))

    # -- joins (trim/extend points) ------------------------------------------------------------
    nn = len(oes)
    jog_sin = math.sin(math.radians(JOG_ANGLE_DEG))
    joins: list[VertexJoin] = []
    for i in range(nn):
        a, b = oes[i - 1], oes[i]
        v = b.start
        ua = _unit(a.start, a.end)
        ub = _unit(b.start, b.end)
        cross = ua[0] * ub[1] - ua[1] * ub[0]
        dot = ua[0] * ub[0] + ua[1] * ub[1]
        pa = (v[0] + a.normal[0] * a.distance_m, v[1] + a.normal[1] * a.distance_m)
        pb = (v[0] + b.normal[0] * b.distance_m, v[1] + b.normal[1] * b.distance_m)
        same_d = abs(a.distance_m - b.distance_m) <= LINEAR_TOL_M
        vtx, eds = [i + 1], [a.index + 1, b.index + 1]
        if abs(cross) <= PARALLEL_SIN and dot < 0:
            kind, pts = "antiparallel", [pa, pb]
            issues.append(ConstructionIssue(
                "antiparallel_edges", REVIEW, f"Edges {eds[0]} and {eds[1]} reverse direction at vertex {i + 1}: their "
                "offsets are parallel and opposite, with no intersection. They were joined at the vertex.", eds, vtx))
        elif abs(cross) <= PARALLEL_SIN and same_d:
            kind, pts = ("zero" if a.distance_m == 0 else "collinear"), [pa]
            if a.distance_m > 0:
                issues.append(ConstructionIssue(
                    "collinear_edges", INFO, f"Edges {eds[0]} and {eds[1]} are collinear with the same setback: "
                    "their offsets form one line (no separate intersection needed).", eds, vtx))
        elif abs(cross) < jog_sin and not same_d:
            kind = "parallel_step" if abs(cross) <= PARALLEL_SIN else "step"
            pts = _step(v, a.normal, b.normal, a.distance_m, b.distance_m, pa, pb)
            if kind == "step":
                t = ((pb[0] - pa[0]) * ub[1] - (pb[1] - pa[1]) * ub[0]) / cross
                remote = math.dist((pa[0] + ua[0] * t, pa[1] + ua[1] * t), v)
                why = f"nearly parallel (turn {math.degrees(math.asin(min(1.0, abs(cross)))):.3f}°); their " \
                      f"intersection would lie {remote:.1f} m from the vertex"
            else:
                why = "parallel, so their offsets have no unique intersection"
            issues.append(ConstructionIssue(
                "parallel_offsets", REVIEW, f"Edges {eds[0]} ({a.distance_m:g} m) and {eds[1]} ({b.distance_m:g} m) are "
                f"{why}. A perpendicular step was drawn at vertex {i + 1} where the larger setback begins.", eds, vtx))
        elif a.distance_m == 0 and b.distance_m == 0:
            kind, pts = "zero", [v]
        else:
            t = ((pb[0] - pa[0]) * ub[1] - (pb[1] - pa[1]) * ub[0]) / cross
            p = (pa[0] + ua[0] * t, pa[1] + ua[1] * t)
            kind, pts = "intersection", [p]
            dist = math.dist(p, v)
            limit = REMOTE_FACTOR * max(a.distance_m, b.distance_m)
            if dist > limit:
                issues.append(ConstructionIssue(
                    "remote_intersection", REVIEW, f"The offsets of edges {eds[0]} and {eds[1]} intersect {dist:.2f} m "
                    f"from vertex {i + 1} (more than {REMOTE_FACTOR:g} × the setback): very acute or very sharp corner. "
                    "Review this corner.", eds, vtx))
        joins.append(VertexJoin(i + 1, v, kind, pts, max(math.dist(q, v) for q in pts)))

    # -- trim/extend each offset to its joins ---------------------------------------------------
    for i, e in enumerate(oes):
        c0, c1 = joins[i].points[-1], joins[(i + 1) % nn].points[0]
        e.trimmed_extended = (c0, c1)
        u = _unit(e.start, e.end)
        s0 = e.raw[0]
        length = e.length
        t0 = (c0[0] - s0[0]) * u[0] + (c0[1] - s0[1]) * u[1]
        t1 = (c1[0] - s0[0]) * u[0] + (c1[1] - s0[1]) * u[1]
        at = lambda t: (s0[0] + u[0] * t, s0[1] + u[1] * t)  # noqa: E731,B023
        e.collapsed = t1 < t0 - LINEAR_TOL_M
        if e.collapsed:
            e.trimmed = [e.raw]
            e.start_action = e.end_action = "trim"
            continue
        e.start_action = "trim" if t0 > LINEAR_TOL_M else "extend" if t0 < -LINEAR_TOL_M else "none"
        e.end_action = "trim" if t1 < length - LINEAR_TOL_M else "extend" if t1 > length + LINEAR_TOL_M else "none"
        e.trimmed = [(at(x), at(y)) for x, y in _minus((0.0, length), (t0, t1))]
        e.extended = [(at(x), at(y)) for x, y in _minus((t0, t1), (0.0, length))]
    collapsed = [e.index + 1 for e in oes if e.collapsed and e.distance_m > 0]
    if collapsed:
        issues.append(ConstructionIssue(
            "collapsed_edge", REVIEW, "The offset of edge(s) " + ", ".join(map(str, collapsed)) + " collapses: the "
            "neighbouring offsets cross before reaching it (edge shorter than its setbacks allow). It is trimmed away "
            "and the neighbouring offsets meet each other.", collapsed))

    # -- ring, noding, faces --------------------------------------------------------------------
    ring: list[Point2] = []
    for j in joins:
        for q in j.points:
            if not ring or math.dist(ring[-1], q) > LINEAR_TOL_M:
                ring.append(q)
    if len(ring) > 1 and math.dist(ring[0], ring[-1]) <= LINEAR_TOL_M:
        ring.pop()
    strip_list = [(e, LineString([e.start, e.end]).buffer(orient * e.distance_m, single_sided=True))
                  for e in oes if e.distance_m > 0]
    strips = unary_union([g for _, g in strip_list]) if strip_list else Polygon()
    kept: list[Polygon] = []
    discarded: list[tuple[Polygon, str]] = []
    if len(ring) >= 3:
        ring_line = LineString(ring + [ring[0]])
        faces = [f for f in polygonize(unary_union(ring_line)) if f.area > 0]
        if faces:
            rps = [f.representative_point() for f in faces]
            xs, ys = np.array([q.x for q in rps]), np.array([q.y for q in rps])
            wind = _winding(ring, xs, ys) * int(orient)
            in_strip = shapely.contains_xy(strips, xs, ys) if strip_list else np.zeros(len(faces), dtype=bool)
            for f, w, s in zip(faces, wind, in_strip):
                if w <= 0:
                    discarded.append((f, "offset loop wound against the parcel (collapsed or crossing offsets)"))
                elif s:
                    discarded.append((f, "lies inside a setback strip (inverted offsets)"))
                else:
                    kept.append(f)
        area = sum(f.area for f, _ in discarded)
        if not ring_line.is_simple:
            issues.append(ConstructionIssue(
                "self_intersection", REVIEW, f"The joined offsets intersect themselves; noding gave {len(faces)} faces, "
                f"{len(kept)} kept and {len(discarded)} discarded ({area:.2f} m²), each listed with its reason.",
                area_m2=area))
        elif discarded:
            issues.append(ConstructionIssue(
                "inverted_offset", REVIEW, f"The joined offsets enclose {area:.2f} m² that lies inside the setback strips "
                "(the offsets passed each other); it was discarded.", area_m2=area))
    kept_union = unary_union(kept) if kept else Polygon()
    buildable: BaseGeometry = kept_union.intersection(parcel) if kept else Polygon()
    clipped = _polygonal(kept_union.difference(parcel)) if kept else Polygon()
    if clipped.area >= AREA_TOL_M2:
        issues.append(ConstructionIssue(
            "outside_parcel", REVIEW, f"{clipped.area:.2f} m² of the joined offsets lay outside the parcel and was "
            "clipped to the parcel boundary.", area_m2=float(clipped.area)))
    else:
        clipped = Polygon()
    # Parts of the result inside a strip. The boundary runs exactly along the strips' inner sides,
    # so floating-point noise leaves zero-area slivers there; only parts of AREA_TOL_M2 or more are real.
    cut_parts = [g for g in geom_parts(buildable.intersection(strips)) if g.area >= AREA_TOL_M2] if strip_list else []
    strip_cut: BaseGeometry = Polygon()
    if cut_parts:
        strip_cut = unary_union(cut_parts)
        tree = STRtree([g for _, g in strip_list])
        cut_idx = sorted({int(j) for x in cut_parts for j in tree.query(x, predicate="intersects")
                          if x.intersection(strip_list[int(j)][1]).area >= AREA_TOL_M2})
        cut_edges = [strip_list[j][0].index + 1 for j in cut_idx]
        buildable = buildable.difference(strip_cut)
        issues.append(ConstructionIssue(
            "strip_cut", REVIEW, f"Trim/extend corners left {strip_cut.area:.2f} m² inside the setback strip of edge(s) "
            + ", ".join(map(str, cut_edges)) + "; it was cut along those strips (perpendicular step where the larger "
            "setback begins).", cut_edges, area_m2=float(strip_cut.area)))
    buildable = _polygonal(buildable)

    # -- per-edge use in the final boundary ----------------------------------------------------
    _edge_usage(oes, buildable)
    eliminated = [e.index + 1 for e in oes if e.outcome == "eliminated" and e.distance_m > 0 and not buildable.is_empty]
    if eliminated:
        issues.append(ConstructionIssue(
            "edge_eliminated", REVIEW, "The offset of edge(s) " + ", ".join(map(str, eliminated)) + " does not appear in "
            "the final buildable boundary (removed by other offsets).", eliminated))

    # -- outcome and validation ----------------------------------------------------------------
    parts = geom_parts(buildable)
    if not parts:
        issues.append(ConstructionIssue("empty_result", REVIEW, "The setbacks leave no buildable area.", area_m2=0.0))
    elif len(parts) > 1:
        issues.append(ConstructionIssue(
            "multiple_polygons", REVIEW, f"The buildable area is split into {len(parts)} separate polygons: "
            + ", ".join(f"{p.area:.2f} m²" for p in sorted(parts, key=lambda p: -p.area)) + ".",
            area_m2=float(buildable.area)))
    problems = []
    if not buildable.is_empty and not buildable.is_valid:
        problems.append(explain_validity(buildable))
    if buildable.difference(parcel).area >= AREA_TOL_M2:
        problems.append("buildable area extends outside the parcel")
    if strip_list:
        # same per-piece tolerance as the strip cut: noise along many offsets may add up, a real overlap cannot hide
        overlap = max((g.area for g in geom_parts(buildable.intersection(strips))), default=0.0)
        if overlap >= AREA_TOL_M2:
            problems.append(f"buildable area overlaps a setback strip ({overlap:.4f} m²)")
    if problems:
        issues.append(ConstructionIssue("validation_failed", ERROR, "The constructed buildable area failed validation: "
                                        + "; ".join(problems) + ". Correct the boundary or classification."))
    return OffsetConstruction("ccw" if orient > 0 else "cw", oes, joins, ring, kept, discarded, clipped, strip_cut,
                              buildable, issues, not problems)


def _unit(a: Point2, b: Point2) -> Point2:
    length = math.dist(a, b)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _step(v: Point2, na: Point2, nb: Point2, da: float, db: float, pa: Point2, pb: Point2) -> list[Point2]:
    """Perpendicular step at vertex v, along the strip end of the edge with the larger setback."""
    nn = na[0] * nb[0] + na[1] * nb[1]
    if da >= db:
        t = db / nn
        return [pa, (v[0] + na[0] * t, v[1] + na[1] * t)]
    t = da / nn
    return [(v[0] + nb[0] * t, v[1] + nb[1] * t), pb]


def _minus(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    """Parts of interval a not covered by interval b (both as (lo, hi)), longer than the tolerance."""
    out = []
    if b[0] - a[0] > LINEAR_TOL_M:
        out.append((a[0], min(a[1], b[0])))
    if a[1] - b[1] > LINEAR_TOL_M:
        out.append((max(a[0], b[1]), a[1]))
    return [(x, y) for x, y in out if y - x > LINEAR_TOL_M]


def _edge_usage(oes: list[OffsetEdge], buildable: BaseGeometry) -> None:
    """Length of the final boundary lying on each trimmed/extended offset."""
    segs: list[tuple[Point2, Point2]] = []
    for p in geom_parts(buildable):
        for r in [p.exterior, *p.interiors]:
            cs = list(r.coords)
            segs += [(cs[k], cs[k + 1]) for k in range(len(cs) - 1)]
    lines = [LineString(e.trimmed_extended) if not e.collapsed and
             math.dist(*e.trimmed_extended) > LINEAR_TOL_M else None for e in oes]
    idx = [k for k, g in enumerate(lines) if g is not None]
    if segs and idx:
        tree = STRtree([lines[k] for k in idx])
        mids = shapely.points([((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in segs])
        pairs = tree.query(mids, predicate="dwithin", distance=1e-5)
        for s, j in zip(*pairs):
            e = oes[idx[int(j)]]
            a, b = segs[int(s)]
            u = _unit(e.start, e.end)
            length = math.dist(a, b)
            if length > 0 and abs(((b[0] - a[0]) * u[1] - (b[1] - a[1]) * u[0]) / length) < 1e-6:
                e.used_length_m += length
    for e in oes:
        full = 0.0 if e.collapsed else math.dist(*e.trimmed_extended)
        if e.collapsed:
            e.outcome = "collapsed"
        elif e.used_length_m <= LINEAR_TOL_M:
            e.outcome = "eliminated"
        elif e.used_length_m < full - 1e-4:
            e.outcome = "partly_used"
        else:
            e.outcome = "used"


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
    """Polygonal part only: clipping can leave zero-area lines/points next to the polygons."""
    parts = geom_parts(geom)
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else unary_union(parts)
