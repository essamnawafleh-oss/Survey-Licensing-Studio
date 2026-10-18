"""Pure geometric edit operations and measurements on drawing entities.

All functions return new entity objects (callers wrap them in undoable commands).
Coordinates and lengths are in drawing units.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .model import Entity, flatten_bulge_polyline

Pt = tuple[float, float]


# ----------------------------------------------------------------------- geometry


def shapely_of(e: Entity) -> BaseGeometry:
    d = e.data
    if e.kind in ("line", "dimension"):
        return LineString([d["p1"], d["p2"]])
    if e.kind == "polyline":
        pts = flatten_bulge_polyline(d["points"], d.get("closed", False), 64)
        if len(pts) < 2:
            return Point(pts[0]) if pts else Point(0, 0)
        if d.get("closed") and len(pts) >= 3:
            return LineString(pts + [pts[0]] if pts[0] != pts[-1] else pts)
        return LineString(pts)
    if e.kind in ("circle", "arc"):
        return LineString(e.flattened(96))
    if e.kind in ("point", "text"):
        return Point(d["position"])
    raise ValueError(e.kind)


def bbox(e: Entity) -> tuple[float, float, float, float]:
    if e.kind == "text":
        x, y = e.data["position"]
        h = e.data.get("height", 1.0)
        w = h * 0.6 * max(len(e.data.get("text", "")), 1)
        return x, y, x + w, y + h
    return shapely_of(e).bounds


def hit_distance(e: Entity, x: float, y: float) -> float:
    if e.kind == "text":
        x0, y0, x1, y1 = bbox(e)
        if x0 <= x <= x1 and y0 <= y <= y1:
            return 0.0
        return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]).exterior.distance(Point(x, y))
    return shapely_of(e).distance(Point(x, y))


def entity_length(e: Entity) -> float | None:
    if e.kind in ("line", "polyline", "arc", "dimension"):
        return float(shapely_of(e).length)
    if e.kind == "circle":
        return 2 * math.pi * e.data["radius"]
    return None


def entity_area(e: Entity) -> float | None:
    if e.kind == "circle":
        return math.pi * e.data["radius"] ** 2
    if e.kind == "polyline" and e.data.get("closed"):
        pts = flatten_bulge_polyline(e.data["points"], True, 256)
        if len(pts) >= 3:
            return float(Polygon(pts).area)
    return None


def snap_points(e: Entity) -> list[tuple[float, float, str]]:
    d = e.data
    out: list[tuple[float, float, str]] = []
    if e.kind in ("line", "dimension"):
        (x1, y1), (x2, y2) = d["p1"], d["p2"]
        out += [(x1, y1, "end"), (x2, y2, "end"), ((x1 + x2) / 2, (y1 + y2) / 2, "mid")]
    elif e.kind == "polyline":
        pts = [(p[0], p[1]) for p in d["points"]]
        out += [(x, y, "end") for x, y in pts]
        n = len(pts)
        segs = n if d.get("closed") else n - 1
        for i in range(max(segs, 0)):
            a, b = pts[i], pts[(i + 1) % n]
            if len(d["points"][i]) < 3 or abs(d["points"][i][2]) < 1e-12:
                out.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, "mid"))
    elif e.kind in ("circle", "arc"):
        cx, cy = d["center"]
        out.append((cx, cy, "center"))
        if e.kind == "arc":
            pts = e.flattened(16)
            out += [(pts[0][0], pts[0][1], "end"), (pts[-1][0], pts[-1][1], "end")]
        else:
            r = d["radius"]
            out += [(cx + r, cy, "quad"), (cx - r, cy, "quad"), (cx, cy + r, "quad"), (cx, cy - r, "quad")]
    elif e.kind in ("point", "text"):
        out.append((d["position"][0], d["position"][1], "node"))
    return out


# ---------------------------------------------------------------------- transforms


def _map_points(e: Entity, fn) -> Entity:  # type: ignore[no-untyped-def]
    n = e.clone()
    d = n.data
    if e.kind in ("line", "dimension"):
        d["p1"] = list(fn(*d["p1"]))
        d["p2"] = list(fn(*d["p2"]))
    elif e.kind == "polyline":
        d["points"] = [list(fn(p[0], p[1])) + ([p[2]] if len(p) > 2 else []) for p in d["points"]]
    elif e.kind in ("circle", "arc"):
        d["center"] = list(fn(*d["center"]))
    elif e.kind in ("point", "text"):
        d["position"] = list(fn(*d["position"]))
    return n


def translate(e: Entity, dx: float, dy: float) -> Entity:
    return _map_points(e, lambda x, y: (x + dx, y + dy))


def rotate(e: Entity, base: Pt, angle_deg: float) -> Entity:
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    bx, by = base

    def fn(x: float, y: float) -> Pt:
        return bx + (x - bx) * ca - (y - by) * sa, by + (x - bx) * sa + (y - by) * ca

    n = _map_points(e, fn)
    if e.kind == "arc":
        n.data["start_angle"] = (e.data["start_angle"] + angle_deg) % 360
        n.data["end_angle"] = (e.data["end_angle"] + angle_deg) % 360
    if e.kind == "text":
        n.data["rotation"] = (e.data.get("rotation", 0.0) + angle_deg) % 360
    return n


def scale(e: Entity, base: Pt, factor: float) -> Entity:
    if factor <= 0:
        raise ValueError("Scale factor must be positive.")
    bx, by = base
    n = _map_points(e, lambda x, y: (bx + (x - bx) * factor, by + (y - by) * factor))
    if e.kind in ("circle", "arc"):
        n.data["radius"] = e.data["radius"] * factor
    if e.kind == "text":
        n.data["height"] = e.data.get("height", 1.0) * factor
    if e.kind == "dimension":
        n.data["offset"] = e.data.get("offset", 1.0) * factor
    return n


def offset(e: Entity, distance: float, side: Pt) -> Entity:
    """Parallel copy at ``distance`` toward ``side``. Bulged polylines become flagged approximations."""
    if distance <= 0:
        raise ValueError("Offset distance must be positive.")
    d = e.data
    if e.kind == "line":
        (x1, y1), (x2, y2) = d["p1"], d["p2"]
        L = math.hypot(x2 - x1, y2 - y1)
        nx, ny = -(y2 - y1) / L, (x2 - x1) / L
        sgn = 1 if (side[0] - x1) * nx + (side[1] - y1) * ny >= 0 else -1
        n = e.clone(new_id=True)
        n.data["p1"] = [x1 + nx * distance * sgn, y1 + ny * distance * sgn]
        n.data["p2"] = [x2 + nx * distance * sgn, y2 + ny * distance * sgn]
        return n
    if e.kind in ("circle", "arc"):
        cx, cy = d["center"]
        inside = math.hypot(side[0] - cx, side[1] - cy) < d["radius"]
        r = d["radius"] - distance if inside else d["radius"] + distance
        if r <= 0:
            raise ValueError("Offset distance is larger than the radius.")
        n = e.clone(new_id=True)
        n.data["radius"] = r
        return n
    if e.kind == "polyline":
        pts = flatten_bulge_polyline(d["points"], d.get("closed", False), 128)
        closed = bool(d.get("closed"))
        if closed:
            poly = Polygon(pts)
            grown = poly.buffer(distance, join_style="mitre", mitre_limit=10)
            shrunk = poly.buffer(-distance, join_style="mitre", mitre_limit=10)
            result = shrunk if poly.contains(Point(side)) else grown
            if result.is_empty or result.geom_type != "Polygon":
                raise ValueError("Offset produced no single closed outline (distance too large?).")
            coords = [list(c) for c in list(result.exterior.coords)[:-1]]
        else:
            line = LineString(pts)
            left = line.offset_curve(distance, join_style="mitre", mitre_limit=10)
            right = line.offset_curve(-distance, join_style="mitre", mitre_limit=10)
            chosen = left if left.distance(Point(side)) <= right.distance(Point(side)) else right
            if chosen.is_empty or chosen.geom_type != "LineString":
                raise ValueError("Offset produced a split or empty result; reduce the distance.")
            coords = [list(c) for c in chosen.coords]
        n = e.clone(new_id=True)
        n.data = {"points": coords, "closed": closed}
        if e.has_curves():
            n.meta["approximated_from"] = "offset of curved polyline"
        return n
    raise ValueError(f"Offset is not supported for {e.kind}.")


# -------------------------------------------------------------------- trim / extend


def _cut_params(line: LineString, cutters: Iterable[BaseGeometry]) -> list[float]:
    params: list[float] = []
    for g in cutters:
        inter = line.intersection(g)
        if inter.is_empty:
            continue
        geoms = getattr(inter, "geoms", [inter])
        for gg in geoms:
            if gg.geom_type == "Point":
                params.append(line.project(gg))
            elif gg.geom_type in ("LineString", "LinearRing"):
                params += [line.project(Point(c)) for c in (gg.coords[0], gg.coords[-1])]
    return sorted(set(round(p, 12) for p in params))


def trim_line(e: Entity, pick: Pt, cutters: Sequence[Entity]) -> list[Entity]:
    """Remove the portion of a LINE between the cutting edges that contains ``pick``."""
    if e.kind != "line":
        raise ValueError("Trim currently supports LINE entities only.")
    line = shapely_of(e)
    params = [p for p in _cut_params(line, [shapely_of(c) for c in cutters if c.id != e.id])
              if 1e-9 < p < line.length - 1e-9]
    if not params:
        raise ValueError("No cutting edge intersects this line.")
    t = line.project(Point(pick))
    bounds = [0.0] + params + [line.length]
    keep = [(0.0, 0.0), (line.length, line.length)]
    for a, b in zip(bounds, bounds[1:]):
        if a <= t <= b:
            keep = [(0.0, a), (b, line.length)]
            break
    out: list[Entity] = []
    for a, b in keep:
        if b - a > 1e-9:
            n = e.clone(new_id=bool(out))  # first piece keeps the original ID
            p1, p2 = line.interpolate(a), line.interpolate(b)
            n.data["p1"], n.data["p2"] = [p1.x, p1.y], [p2.x, p2.y]
            out.append(n)
    return out


def extend_line(e: Entity, pick: Pt, boundaries: Sequence[Entity], max_factor: float = 1000.0) -> Entity:
    """Extend the end of a LINE nearest ``pick`` to the closest boundary along its direction."""
    if e.kind != "line":
        raise ValueError("Extend currently supports LINE entities only.")
    (x1, y1), (x2, y2) = e.data["p1"], e.data["p2"]
    L = math.hypot(x2 - x1, y2 - y1)
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    from_end = math.hypot(pick[0] - x2, pick[1] - y2) <= math.hypot(pick[0] - x1, pick[1] - y1)
    sx, sy, dx, dy = (x2, y2, ux, uy) if from_end else (x1, y1, -ux, -uy)
    ray = LineString([(sx, sy), (sx + dx * L * max_factor, sy + dy * L * max_factor)])
    others = unary_union([shapely_of(b) for b in boundaries if b.id != e.id])
    params = [p for p in _cut_params(ray, [others]) if p > 1e-9]
    if not params:
        raise ValueError("No boundary found in the extension direction.")
    hit = ray.interpolate(params[0])
    n = e.clone()
    if from_end:
        n.data["p2"] = [hit.x, hit.y]
    else:
        n.data["p1"] = [hit.x, hit.y]
    return n


def polygon_area(points: Sequence[Pt]) -> tuple[float, bool]:
    """Shoelace area of picked points and whether the outline is a valid simple polygon."""
    n = len(points)
    if n < 3:
        return 0.0, False
    area = abs(0.5 * sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
                         for i in range(n)))
    return area, Polygon(points).is_valid
