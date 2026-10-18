"""Polygon/line construction and validation in a local metric frame.

Survey coordinates are large (national grid eastings/northings). All calculations are
done in a *local metric frame*: coordinates are translated by a fixed origin and scaled
from drawing units to metres. Areas and lengths are invariant under the translation, so
results are exact while floating-point error stays small.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from ..core.errors import GeometryError

Point2 = tuple[float, float]

DEFAULT_CLOSURE_TOLERANCE_M = 0.001  # 1 mm


@dataclass(frozen=True)
class MetricFrame:
    """Maps drawing coordinates to local metres: ((x - ox) * f, (y - oy) * f)."""

    origin_x: float
    origin_y: float
    factor: float  # drawing unit -> metre

    def to_local(self, pts: Sequence[Sequence[float]]) -> list[Point2]:
        f = self.factor
        return [((p[0] - self.origin_x) * f, (p[1] - self.origin_y) * f) for p in pts]

    def to_drawing_geom(self, geom: BaseGeometry) -> BaseGeometry:
        inv = 1.0 / self.factor
        g = affinity.scale(geom, xfact=inv, yfact=inv, origin=(0, 0))
        return affinity.translate(g, xoff=self.origin_x, yoff=self.origin_y)

    def to_dict(self) -> dict[str, float]:
        return {"origin_x": self.origin_x, "origin_y": self.origin_y, "factor": self.factor}

    @classmethod
    def for_points(cls, pts: Sequence[Sequence[float]], factor: float) -> "MetricFrame":
        if not pts:
            return cls(0.0, 0.0, factor)
        # Round the origin so the frame is stable and reproducible across runs.
        ox = math.floor(min(p[0] for p in pts))
        oy = math.floor(min(p[1] for p in pts))
        return cls(float(ox), float(oy), factor)


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    entity_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "entity_id": self.entity_id}


def _dedupe_consecutive(pts: Sequence[Point2], tol: float) -> list[Point2]:
    out: list[Point2] = []
    for p in pts:
        if not out or math.dist(out[-1], p) > tol:
            out.append((float(p[0]), float(p[1])))
    return out


def build_polygon(points: Sequence[Point2], closed_flag: bool, entity_id: str = "",
                  closure_tol: float = DEFAULT_CLOSURE_TOLERANCE_M,
                  min_area: float = 1e-6) -> tuple[Polygon | None, list[Issue]]:
    """Build a polygon from local-metre points and report every problem found.

    A ring is accepted as closed if the entity is flagged closed, or if its first and
    last vertices coincide within ``closure_tol``. Otherwise it is reported as open and
    no polygon is returned; open rings are never closed silently.
    """
    issues: list[Issue] = []
    pts = _dedupe_consecutive(points, closure_tol)
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) <= closure_tol:
        pts = pts[:-1]
    elif not closed_flag:
        gap = math.dist(pts[0], pts[-1]) if len(pts) >= 2 else 0.0
        issues.append(Issue("error", "open_polygon",
                            f"Boundary is not closed (gap {gap:.4f} m).", entity_id))
        return None, issues
    if len(pts) < 3:
        issues.append(Issue("error", "degenerate", "Polygon has fewer than 3 distinct vertices.", entity_id))
        return None, issues
    poly = Polygon(pts)
    if not poly.is_valid:
        issues.append(Issue("error", "invalid_polygon",
                            f"Invalid polygon geometry: {explain_validity(poly)}", entity_id))
        return None, issues
    if poly.area <= min_area:
        issues.append(Issue("error", "zero_area", "Polygon has zero (or negligible) area.", entity_id))
        return None, issues
    return poly, issues


def build_linestring(points: Sequence[Point2], closed: bool, entity_id: str = "",
                     tol: float = DEFAULT_CLOSURE_TOLERANCE_M) -> tuple[LineString | None, list[Issue]]:
    pts = _dedupe_consecutive(points, tol)
    if closed and len(pts) >= 2 and math.dist(pts[0], pts[-1]) > tol:
        pts.append(pts[0])
    if len(pts) < 2:
        return None, [Issue("error", "degenerate", "Line has fewer than 2 distinct vertices.", entity_id)]
    line = LineString(pts)
    if line.length <= tol:
        return None, [Issue("error", "zero_length", "Line has zero length.", entity_id)]
    return line, []


def require_polygon(points: Sequence[Point2], closed_flag: bool, entity_id: str = "") -> Polygon:
    poly, issues = build_polygon(points, closed_flag, entity_id)
    if poly is None:
        raise GeometryError("; ".join(i.message for i in issues), issues=[i.to_dict() for i in issues])
    return poly


def ring_coords(poly: Polygon) -> list[Point2]:
    """Exterior vertices (without the repeated closing vertex), counter-clockwise."""
    from shapely.geometry.polygon import orient

    coords = list(orient(poly, sign=1.0).exterior.coords)[:-1]
    return [(float(x), float(y)) for x, y in coords]


def geom_parts(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
