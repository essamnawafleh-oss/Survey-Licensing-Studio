"""Vector rendering of the drawing model onto a ReportLab canvas at a true 1:N scale."""
from __future__ import annotations

import math
from dataclasses import dataclass

from reportlab.lib.units import mm

from ..cad.model import Drawing, Entity, arc_points, flatten_bulge_polyline
from ..core.errors import ReportError
from .arabic import draw_text

STANDARD_SCALES = (50, 100, 200, 250, 500, 750, 1000, 1250, 1500, 2000, 2500, 5000, 10000, 20000, 25000, 50000)
FILLED_ROLES = {"setback_violation": 0.35, "licensed_area": 0.25}


@dataclass
class Viewport:
    """Maps world (drawing-unit) coordinates into a paper rectangle (points)."""

    x0: float
    y0: float
    width: float
    height: float
    world_cx: float
    world_cy: float
    pt_per_unit: float

    def map(self, x: float, y: float) -> tuple[float, float]:
        return (self.x0 + self.width / 2 + (x - self.world_cx) * self.pt_per_unit,
                self.y0 + self.height / 2 + (y - self.world_cy) * self.pt_per_unit)


def paper_points_per_unit(unit_to_m: float, scale_n: int) -> float:
    """Paper points per drawing unit at 1:N (1 m on the ground = 1000/N mm on paper)."""
    return unit_to_m * 1000.0 / scale_n * mm


def choose_scale(extents: tuple[float, float, float, float], unit_to_m: float, width_pt: float,
                 height_pt: float, requested: int = 0) -> int:
    w_m = max((extents[2] - extents[0]) * unit_to_m, 1e-9)
    h_m = max((extents[3] - extents[1]) * unit_to_m, 1e-9)

    def fits(n: int) -> bool:
        k = paper_points_per_unit(1.0, n)
        return w_m * k <= width_pt and h_m * k <= height_pt

    if requested:
        if not fits(requested):
            raise ReportError(f"The drawing does not fit the page at 1:{requested}. Choose a smaller scale "
                              "(larger N), a larger page, or landscape orientation.", scale=requested)
        return requested
    for n in STANDARD_SCALES:
        if fits(n):
            return n
    raise ReportError("The drawing is too large for the selected page even at 1:50000.")


def print_color(hex_color: str) -> tuple[float, float, float]:
    """Layer colours are chosen for a dark screen; very light colours print as dark grey."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum > 0.72:
        k = 0.55 / max(lum, 1e-6)
        return (r * k * 0.5, g * k * 0.5, b * k * 0.5)
    return (r, g, b)


def render_drawing(c, drawing: Drawing, vp: Viewport, unit_to_m: float, scale_n: int,  # type: ignore[no-untyped-def]
                   monochrome: bool = False) -> None:
    c.saveState()
    p = c.beginPath()
    p.rect(vp.x0, vp.y0, vp.width, vp.height)
    c.clipPath(p, stroke=0, fill=0)
    for layer in drawing.layers:
        if not layer.visible:
            continue
        ents = [e for e in drawing.entities.values() if e.layer == layer.name]
        if not ents:
            continue
        lw = max(layer.lineweight, 0.13) * mm
        for e in ents:
            col = (0, 0, 0) if monochrome else print_color(e.color or layer.color)
            c.setStrokeColorRGB(*col)
            c.setFillColorRGB(*col)
            c.setLineWidth(lw)
            c.setDash(*((3 * mm * 0.6, 1.5 * mm * 0.6) if layer.linetype.upper().startswith("DASH") else ()))
            fill_alpha = FILLED_ROLES.get(layer.role)
            _entity(c, e, vp, unit_to_m, scale_n, fill_alpha)
    c.restoreState()


def _path(c, pts, vp: Viewport, closed: bool):  # type: ignore[no-untyped-def]
    path = c.beginPath()
    x, y = vp.map(*pts[0])
    path.moveTo(x, y)
    for px, py in pts[1:]:
        path.lineTo(*vp.map(px, py))
    if closed:
        path.close()
    return path


def _entity(c, e: Entity, vp: Viewport, unit_to_m: float, scale_n: int, fill_alpha: float | None) -> None:  # type: ignore[no-untyped-def]
    d = e.data
    if e.kind == "line":
        x1, y1 = vp.map(*d["p1"])
        x2, y2 = vp.map(*d["p2"])
        c.line(x1, y1, x2, y2)
    elif e.kind == "polyline":
        pts = flatten_bulge_polyline(d["points"], d.get("closed", False), 128)
        if len(pts) < 2:
            return
        closed = bool(d.get("closed"))
        path = _path(c, pts, vp, closed)
        if closed and fill_alpha:
            c.saveState()
            c.setFillAlpha(fill_alpha)
            c.drawPath(path, stroke=0, fill=1)
            c.restoreState()
        c.drawPath(path, stroke=1, fill=0)
    elif e.kind == "circle":
        x, y = vp.map(*d["center"])
        c.circle(x, y, d["radius"] * vp.pt_per_unit, stroke=1, fill=0)
    elif e.kind == "arc":
        pts = arc_points(d["center"], d["radius"], d["start_angle"], d["end_angle"], 128)
        c.drawPath(_path(c, pts, vp, False), stroke=1, fill=0)
    elif e.kind == "point":
        x, y = vp.map(*d["position"])
        c.circle(x, y, 0.4 * mm, stroke=0, fill=1)
    elif e.kind == "text":
        x, y = vp.map(*d["position"])
        size = max(d.get("height", 1.0) * vp.pt_per_unit, 4.0)  # keep text readable at small scales
        c.saveState()
        c.translate(x, y)
        c.rotate(d.get("rotation", 0.0))
        draw_text(c, 0, 0, d["text"], size)
        c.restoreState()
    elif e.kind == "dimension":
        _dimension(c, e, vp, unit_to_m)


def _dimension(c, e: Entity, vp: Viewport, unit_to_m: float) -> None:  # type: ignore[no-untyped-def]
    d = e.data
    (x1, y1), (x2, y2) = d["p1"], d["p2"]
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    nx, ny = -(y2 - y1) / length, (x2 - x1) / length
    off = d.get("offset", 1.0)
    a = vp.map(x1 + nx * off, y1 + ny * off)
    b = vp.map(x2 + nx * off, y2 + ny * off)
    c.line(*vp.map(x1, y1), *a)
    c.line(*vp.map(x2, y2), *b)
    c.line(*a, *b)
    for px, py in (a, b):
        c.line(px - 1.2, py - 1.2, px + 1.2, py + 1.2)
    text = d.get("text_override") or f"{length * unit_to_m:.2f}"
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    if angle > 90 or angle < -90:
        angle += 180
    c.saveState()
    c.translate((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 1)
    c.rotate(angle)
    draw_text(c, 0, 1.5, text, 6.5, align="center", rtl_default=False)
    c.restoreState()


def draw_scale_bar(c, x: float, y: float, scale_n: int) -> None:  # type: ignore[no-untyped-def]
    """Scale bar with a 'nice' ground length of about 40-60 mm on paper."""
    ground_per_mm = scale_n / 1000.0  # metres per paper mm
    target_m = 50 * ground_per_mm
    nice = min((v for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)),
               key=lambda v: abs(v - target_m))
    length_pt = nice / ground_per_mm * mm
    c.setLineWidth(0.6)
    c.setStrokeColorRGB(0, 0, 0)
    for i in range(4):
        seg = length_pt / 4
        c.setFillColorRGB(*((0, 0, 0) if i % 2 == 0 else (1, 1, 1)))
        c.rect(x + i * seg, y, seg, 2 * mm, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    draw_text(c, x, y + 3 * mm, "0", 6.5, rtl_default=False)
    draw_text(c, x + length_pt, y + 3 * mm, f"{nice:g} m", 6.5, align="center", rtl_default=False)


def draw_north_arrow(c, x: float, y: float, size: float = 10 * mm) -> None:  # type: ignore[no-untyped-def]
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    p = c.beginPath()
    p.moveTo(x, y + size)
    p.lineTo(x - size * 0.3, y)
    p.lineTo(x, y + size * 0.3)
    p.close()
    c.drawPath(p, fill=1, stroke=1)
    p2 = c.beginPath()
    p2.moveTo(x, y + size)
    p2.lineTo(x + size * 0.3, y)
    p2.lineTo(x, y + size * 0.3)
    p2.close()
    c.drawPath(p2, fill=0, stroke=1)
    draw_text(c, x, y + size + 1.5 * mm, "N", 8, align="center", bold=True, rtl_default=False)
