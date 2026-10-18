"""Interactive CAD tools. Each tool receives points (snapped clicks or typed coordinates),
numbers and Enter/Escape from the canvas or the command line, and commits undoable
commands to the drawing's undo stack.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from ..cad import ops
from ..cad.commands import AddEntities, DeleteEntities, ReplaceEntities
from ..cad.model import Entity, new_entity_id
from ..core.i18n import tr

Pt = tuple[float, float]
PREVIEW = QColor("#80DEEA")


class Tool:
    name = "tool"
    wants_raw_press = False
    snaps_on_move = True

    def __init__(self) -> None:
        self.canvas = None  # type: ignore[assignment]
        self.points: list[Pt] = []

    # lifecycle ----------------------------------------------------------------------
    def activate(self, canvas) -> None:  # type: ignore[no-untyped-def]
        self.canvas = canvas
        self.points = []
        self.prompt(self.start_prompt())

    def deactivate(self) -> None:
        self.points = []

    def start_prompt(self) -> str:
        return ""

    def prompt(self, text: str) -> None:
        if text and self.canvas is not None:
            self.canvas.message.emit(text)

    # input ----------------------------------------------------------------------------
    def base_point(self) -> Pt | None:
        return self.points[-1] if self.points else None

    def on_point(self, pt: Pt) -> None:
        pass

    def on_number(self, value: float) -> bool:
        return False

    def on_text(self, text: str) -> bool:
        return False

    def on_enter(self) -> None:
        self.finish()

    def on_escape(self) -> None:
        self.points = []
        self.canvas.set_tool(SelectTool())

    def on_move(self, ev) -> None:  # type: ignore[no-untyped-def]
        pass

    def on_press(self, ev) -> None:  # type: ignore[no-untyped-def]
        pass

    def on_release(self, ev) -> None:  # type: ignore[no-untyped-def]
        pass

    def finish(self) -> None:
        self.canvas.set_tool(SelectTool())

    def paint(self, p: QPainter) -> None:
        pass

    # helpers ------------------------------------------------------------------------------
    @property
    def drawing(self):  # type: ignore[no-untyped-def]
        return self.canvas.drawing

    def layer_ok(self, name: str | None = None) -> bool:
        if self.drawing is None:
            self.prompt(tr("tool.no_drawing"))
            return False
        if not self.canvas.can_edit():
            self.prompt(tr("tool.read_only"))
            return False
        layer = self.drawing.layer(name or self.drawing.active_layer)
        if layer.locked:
            self.prompt(tr("tool.layer_locked", layer=layer.name))
            return False
        return True

    def commit(self, cmd) -> None:  # type: ignore[no-untyped-def]
        self.canvas.stack.push(cmd)
        self.canvas.edited()

    def new_entity(self, kind: str, data: dict) -> Entity:
        return Entity(kind, self.drawing.active_layer, data, meta={"source": "user"})

    def editable(self, ids) -> list[Entity]:  # type: ignore[no-untyped-def]
        out = []
        for i in ids:
            e = self.drawing.entities.get(i)
            if e is None:
                continue
            if self.drawing.layer(e.layer).locked:
                self.prompt(tr("tool.layer_locked", layer=e.layer))
                continue
            out.append(e)
        return out

    def fmt_len(self, v: float) -> str:
        f = self.canvas._unit_factor()
        return f"{v * f:.3f} m" if f else f"{v:.4f} {tr('tool.drawing_units')}"

    def fmt_area(self, v: float) -> str:
        f = self.canvas._unit_factor()
        return f"{v * f * f:.3f} m²" if f else f"{v:.4f} {tr('tool.drawing_units')}²"

    def rubber(self, p: QPainter, pts: list[Pt], closed: bool = False) -> None:
        if not pts:
            return
        pen = QPen(PREVIEW, 1, Qt.DashLine)
        p.setPen(pen)
        sp = [self.canvas.to_screen(*q) for q in pts]
        for a, b in zip(sp, sp[1:]):
            p.drawLine(a, b)
        if closed and len(sp) > 2:
            p.drawLine(sp[-1], sp[0])


class SelectTool(Tool):
    name = "select"
    wants_raw_press = True
    snaps_on_move = False

    def start_prompt(self) -> str:
        return ""

    def on_press(self, ev) -> None:  # type: ignore[no-untyped-def]
        self.canvas._drag_start = ev.position()
        self.canvas._drag_now = None
        self._shift = bool(ev.modifiers() & Qt.ShiftModifier)

    def on_move(self, ev) -> None:  # type: ignore[no-untyped-def]
        c = self.canvas
        if c._drag_start is not None:
            if (ev.position() - c._drag_start).manhattanLength() > 4:
                c._drag_now = ev.position()
        else:
            hit = c.pick(ev.position())
            c.hover_id = hit.id if hit else None

    def on_release(self, ev) -> None:  # type: ignore[no-untyped-def]
        c = self.canvas
        if c._drag_start is None:
            return
        if c._drag_now is not None:
            ids = c.window_select(c._drag_start, c._drag_now)
            if not self._shift:
                c.selection.clear()
            c.selection.update(ids)
        else:
            hit = c.pick(ev.position())
            if hit is None:
                if not self._shift:
                    c.selection.clear()
            elif self._shift and hit.id in c.selection:
                c.selection.discard(hit.id)
            else:
                if not self._shift:
                    c.selection.clear()
                c.selection.add(hit.id)
        c._drag_start = c._drag_now = None
        c.selectionChanged.emit()

    def on_escape(self) -> None:
        self.canvas.selection.clear()
        self.canvas.selectionChanged.emit()

    def on_enter(self) -> None:
        pass


class LineTool(Tool):
    name = "line"

    def start_prompt(self) -> str:
        return tr("tool.line.first")

    def on_point(self, pt: Pt) -> None:
        if not self.layer_ok():
            return
        if self.points:
            self.commit(AddEntities([self.new_entity("line", {"p1": list(self.points[-1]), "p2": list(pt)})],
                                    "line"))
        self.points.append(pt)
        self.prompt(tr("tool.line.next"))

    def on_text(self, text: str) -> bool:
        if text.upper() == "C" and len(self.points) >= 3:
            self.on_point(self.points[0])
            self.finish()
            return True
        return False

    def paint(self, p: QPainter) -> None:
        if self.points:
            self.rubber(p, [self.points[-1], self.canvas.cursor_world])


class PolylineTool(Tool):
    name = "polyline"

    def start_prompt(self) -> str:
        return tr("tool.pline.first")

    def on_point(self, pt: Pt) -> None:
        self.points.append(pt)
        self.prompt(tr("tool.pline.next"))

    def on_text(self, text: str) -> bool:
        if text.upper() == "C":
            self._commit(True)
            return True
        return False

    def on_enter(self) -> None:
        self._commit(False)

    def _commit(self, closed: bool) -> None:
        if len(self.points) >= (3 if closed else 2) and self.layer_ok():
            self.commit(AddEntities([self.new_entity("polyline", {"points": [list(q) for q in self.points],
                                                                   "closed": closed})], "polyline"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            self.rubber(p, self.points + [self.canvas.cursor_world])


class RectTool(Tool):
    name = "rectangle"

    def start_prompt(self) -> str:
        return tr("tool.rect.first")

    def on_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.rect.second"))
            return
        (x0, y0), (x1, y1) = self.points[0], pt
        if x0 != x1 and y0 != y1 and self.layer_ok():
            self.commit(AddEntities([self.new_entity("polyline", {"points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                                                                   "closed": True})], "rectangle"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            (x0, y0), (x1, y1) = self.points[0], self.canvas.cursor_world
            self.rubber(p, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True)


class CircleTool(Tool):
    name = "circle"

    def start_prompt(self) -> str:
        return tr("tool.circle.center")

    def on_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.circle.radius"))
            return
        self._make(math.dist(self.points[0], pt))

    def on_number(self, value: float) -> bool:
        if self.points:
            self._make(value)
            return True
        return False

    def _make(self, r: float) -> None:
        if r > 0 and self.layer_ok():
            self.commit(AddEntities([self.new_entity("circle", {"center": list(self.points[0]), "radius": r})],
                                    "circle"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            c = self.canvas.to_screen(*self.points[0])
            r = math.dist(self.points[0], self.canvas.cursor_world) * self.canvas.scale
            p.setPen(QPen(PREVIEW, 1, Qt.DashLine))
            p.drawEllipse(c, r, r)


class _SelectionFirstTool(Tool):
    """Modify tools: use the current selection, or let the user pick objects then press Enter."""

    def activate(self, canvas) -> None:  # type: ignore[no-untyped-def]
        self.canvas = canvas
        self.points = []
        self.picking = not canvas.selection
        self.prompt(tr("tool.select_objects") if self.picking else self.after_selection_prompt())

    def after_selection_prompt(self) -> str:
        return tr("tool.base_point")

    def on_point(self, pt: Pt) -> None:
        if self.picking:
            hit = self.canvas.pick(self.canvas.to_screen(*pt))
            if hit is not None:
                self.canvas.selection.symmetric_difference_update({hit.id})
                self.canvas.selectionChanged.emit()
            return
        self.on_selected_point(pt)

    def on_enter(self) -> None:
        if self.picking:
            if not self.canvas.selection:
                self.finish()
                return
            self.picking = False
            self.selected_ready()
            return
        self.finish()

    def selected_ready(self) -> None:
        self.prompt(self.after_selection_prompt())

    def base_point(self) -> Pt | None:
        return None if self.picking else super().base_point()

    def on_selected_point(self, pt: Pt) -> None:
        pass


class MoveTool(_SelectionFirstTool):
    name = "move"
    copy_mode = False

    def on_selected_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.second_point"))
            return
        self._apply(pt[0] - self.points[0][0], pt[1] - self.points[0][1])

    def on_number(self, value: float) -> bool:
        return False

    def _apply(self, dx: float, dy: float) -> None:
        ents = self.editable(self.canvas.selection) if not self.copy_mode else \
            [self.drawing.entities[i] for i in self.canvas.selection if i in self.drawing.entities]
        if self.copy_mode:
            if not self.layer_ok():
                return
            copies = []
            for e in ents:
                c = ops.translate(e, dx, dy)
                c.id = new_entity_id()
                c.meta = {**c.meta, "source": "user", "copied_from": e.id}
                copies.append(c)
            self.commit(AddEntities(copies, "copy"))
            self.prompt(tr("tool.second_point"))
            return
        if ents:
            self.commit(ReplaceEntities([e.clone() for e in ents], [ops.translate(e, dx, dy) for e in ents], "move"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points and not self.picking:
            self.rubber(p, [self.points[0], self.canvas.cursor_world])


class CopyTool(MoveTool):
    name = "copy"
    copy_mode = True


class RotateTool(_SelectionFirstTool):
    name = "rotate"

    def on_selected_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.rotate.angle"))
            return
        b = self.points[0]
        self._apply(math.degrees(math.atan2(pt[1] - b[1], pt[0] - b[0])))

    def on_number(self, value: float) -> bool:
        if self.points and not self.picking:
            self._apply(value)
            return True
        return False

    def _apply(self, angle: float) -> None:
        ents = self.editable(self.canvas.selection)
        if ents:
            self.commit(ReplaceEntities([e.clone() for e in ents], [ops.rotate(e, self.points[0], angle) for e in ents],
                                        "rotate"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points and not self.picking:
            self.rubber(p, [self.points[0], self.canvas.cursor_world])


class ScaleTool(_SelectionFirstTool):
    name = "scale"

    def on_selected_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.scale.factor"))
            return

    def on_number(self, value: float) -> bool:
        if self.points and not self.picking:
            if value <= 0:
                self.prompt(tr("tool.scale.positive"))
                return True
            ents = self.editable(self.canvas.selection)
            if ents:
                self.commit(ReplaceEntities([e.clone() for e in ents],
                                            [ops.scale(e, self.points[0], value) for e in ents], "scale"))
            self.finish()
            return True
        return False


class EraseTool(_SelectionFirstTool):
    name = "erase"

    def activate(self, canvas) -> None:  # type: ignore[no-untyped-def]
        super().activate(canvas)
        if not self.picking:
            self.selected_ready()

    def selected_ready(self) -> None:
        if not self.canvas.can_edit():
            self.prompt(tr("tool.read_only"))
            self.finish()
            return
        ents = self.editable(self.canvas.selection)
        if ents:
            self.commit(DeleteEntities([e.id for e in ents], "erase"))
            self.prompt(tr("tool.erased", n=len(ents)))
        self.canvas.selection.clear()
        self.finish()


class OffsetTool(Tool):
    name = "offset"

    def activate(self, canvas) -> None:  # type: ignore[no-untyped-def]
        super().activate(canvas)
        self.distance: float | None = None
        self.target: Entity | None = None

    def start_prompt(self) -> str:
        return tr("tool.offset.distance")

    def on_number(self, value: float) -> bool:
        if value <= 0:
            self.prompt(tr("tool.offset.positive"))
        else:
            self.distance = value
            self.prompt(tr("tool.offset.pick"))
        return True

    def on_point(self, pt: Pt) -> None:
        if self.distance is None:
            if not self.points:
                self.points.append(pt)
                self.prompt(tr("tool.second_point"))
            else:
                self.on_number(math.dist(self.points[0], pt))
                self.points = []
            return
        if self.target is None:
            hit = self.canvas.pick(self.canvas.to_screen(*pt))
            if hit is None:
                return
            self.target = hit
            self.prompt(tr("tool.offset.side"))
            return
        if not self.layer_ok(self.target.layer):
            self.target = None
            return
        try:
            new = ops.offset(self.target, self.distance, pt)
        except ValueError as exc:
            self.prompt(str(exc))
        else:
            new.meta = {**new.meta, "source": "user", "offset_from": self.target.id}
            self.commit(AddEntities([new], "offset"))
        self.target = None
        self.prompt(tr("tool.offset.pick"))


class TrimTool(Tool):
    name = "trim"
    extend = False

    def start_prompt(self) -> str:
        return tr("tool.extend.pick") if self.extend else tr("tool.trim.pick")

    def on_point(self, pt: Pt) -> None:
        hit = self.canvas.pick(self.canvas.to_screen(*pt))
        if hit is None:
            return
        if not self.layer_ok(hit.layer):
            return
        others = [e for e in self.canvas.visible_entities() if e.kind not in ("text", "point") and e.id != hit.id]
        try:
            if self.extend:
                new = [ops.extend_line(hit, pt, others)]
            else:
                new = ops.trim_line(hit, pt, others)
        except ValueError as exc:
            self.prompt(str(exc))
            return
        self.commit(ReplaceEntities([hit.clone()], new, "extend" if self.extend else "trim"))


class ExtendTool(TrimTool):
    name = "extend"
    extend = True


class DistTool(Tool):
    name = "dist"

    def start_prompt(self) -> str:
        return tr("tool.line.first")

    def on_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.second_point"))
            return
        a = self.points[0]
        dx, dy = pt[0] - a[0], pt[1] - a[1]
        ang = math.degrees(math.atan2(dy, dx)) % 360
        self.prompt(tr("tool.dist.result", d=self.fmt_len(math.hypot(dx, dy)), dx=self.fmt_len(dx),
                       dy=self.fmt_len(dy), a=f"{ang:.4f}"))
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            self.rubber(p, [self.points[0], self.canvas.cursor_world])


class AreaTool(Tool):
    name = "area"

    def start_prompt(self) -> str:
        return tr("tool.area.start")

    def on_point(self, pt: Pt) -> None:
        self.points.append(pt)

    def on_text(self, text: str) -> bool:
        if text.upper() == "O":
            ids = list(self.canvas.selection)
            ents = [self.drawing.entities[i] for i in ids if i in self.drawing.entities]
            if len(ents) != 1:
                self.prompt(tr("tool.area.select_one"))
                return True
            a = ops.entity_area(ents[0])
            per = ops.entity_length(ents[0])
            if a is None:
                self.prompt(tr("tool.area.not_closed"))
            else:
                self.prompt(tr("tool.area.result", a=self.fmt_area(a), p=self.fmt_len(per or 0)))
            self.finish()
            return True
        return False

    def on_enter(self) -> None:
        if len(self.points) >= 3:
            area, valid = ops.polygon_area(self.points)
            per = sum(math.dist(self.points[i], self.points[(i + 1) % len(self.points)])
                      for i in range(len(self.points)))
            msg = tr("tool.area.result", a=self.fmt_area(area), p=self.fmt_len(per))
            if not valid:
                msg += " " + tr("tool.area.self_intersecting")
            self.prompt(msg)
        self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            self.rubber(p, self.points + [self.canvas.cursor_world], True)


class TextTool(Tool):
    name = "text"

    def activate(self, canvas) -> None:  # type: ignore[no-untyped-def]
        super().activate(canvas)
        self.height: float | None = None

    def start_prompt(self) -> str:
        return tr("tool.text.insert")

    def on_point(self, pt: Pt) -> None:
        if not self.points:
            self.points.append(pt)
            self.prompt(tr("tool.text.height"))

    def on_number(self, value: float) -> bool:
        if self.points and self.height is None and value > 0:
            self.height = value
            self.prompt(tr("tool.text.content"))
            return True
        return False

    def on_text(self, text: str) -> bool:
        if self.points and self.height is None:
            return False
        if self.points and self.height is not None and text.strip():
            if self.layer_ok():
                self.commit(AddEntities([self.new_entity("text", {"position": list(self.points[0]), "text": text,
                                                                  "height": self.height, "rotation": 0.0})], "text"))
            self.finish()
            return True
        return False


class DimTool(Tool):
    name = "dimension"

    def start_prompt(self) -> str:
        return tr("tool.dim.first")

    def on_point(self, pt: Pt) -> None:
        self.points.append(pt)
        if len(self.points) == 1:
            self.prompt(tr("tool.dim.second"))
        elif len(self.points) == 2:
            self.prompt(tr("tool.dim.offset"))
        else:
            (x1, y1), (x2, y2), (px, py) = self.points
            L = math.hypot(x2 - x1, y2 - y1)
            if L > 0 and self.layer_ok():
                nx, ny = -(y2 - y1) / L, (x2 - x1) / L
                off = (px - x1) * nx + (py - y1) * ny
                self.commit(AddEntities([self.new_entity("dimension", {"p1": [x1, y1], "p2": [x2, y2], "offset": off,
                                                                       "text_height": max(L / 40, 0.15)})],
                                        "dimension"))
            self.finish()

    def paint(self, p: QPainter) -> None:
        if self.points:
            self.rubber(p, self.points + [self.canvas.cursor_world])


class ParcelPickTool(Tool):
    """Copy a parcel polygon from the regulatory/cadastral layers as the property-boundary candidate."""

    name = "parcel"

    def start_prompt(self) -> str:
        return tr("tool.parcel.pick")

    def on_point(self, pt: Pt) -> None:
        d = self.drawing
        if d is None or not self.layer_ok("PROPERTY_BOUNDARY" if d.has_layer("PROPERTY_BOUNDARY") else None):
            return
        hook = getattr(self.canvas, "cadastre_pick", None)
        if hook is not None and hook(pt[0], pt[1]):  # the parcel register answered (attributes included)
            self.finish()
            return
        roles = {layer.name for layer in d.layers if layer.role in ops.PARCEL_LAYER_ROLES}
        near = self.canvas.entities_in(pt[0] - 1e-6, pt[1] - 1e-6, pt[0] + 1e-6, pt[1] + 1e-6)
        polygons = [e for e in near if e.layer in roles and e.meta.get("source") != "derived"]
        poly, _ = ops.find_parcel(polygons, [], pt)
        if poly is None:
            self.prompt(tr("tool.parcel.none"))
            return
        x0, y0, x1, y1 = ops.fast_bbox(poly)
        texts = [e for e in self.canvas.entities_in(x0, y0, x1, y1) if e.kind == "text"]
        same_source = [t for t in texts if t.meta.get("source") == poly.meta.get("source")]
        texts = same_source or texts  # parcel numbers come from the same plan as the polygon
        _, labels = ops.find_parcel([poly], texts, pt)
        d.ensure_layer("PROPERTY_BOUNDARY", role="property_boundary", color="#4FC3F7", lineweight=0.5)
        copy_ = Entity("polyline", "PROPERTY_BOUNDARY", {"points": [list(p) for p in poly.data["points"]],
                                                          "closed": True},
                       meta={"source": poly.meta.get("source", "regulatory"), "copied_from": poly.id,
                             "parcel_labels": labels, **({"parcel_number": labels[0]} if len(labels) == 1 else {})})
        self.commit(AddEntities([copy_], "pick parcel"))
        self.canvas.selection = {copy_.id}
        self.canvas.selectionChanged.emit()
        if len(labels) == 1:
            self.prompt(tr("tool.parcel.done", number=labels[0]))
        else:
            self.prompt(tr("tool.parcel.done_labels", labels=", ".join(labels) or "—"))
        self.finish()


TOOLS: dict[str, type[Tool]] = {
    "select": SelectTool, "line": LineTool, "polyline": PolylineTool, "rectangle": RectTool, "circle": CircleTool,
    "move": MoveTool, "copy": CopyTool, "rotate": RotateTool, "scale": ScaleTool, "offset": OffsetTool,
    "trim": TrimTool, "extend": ExtendTool, "erase": EraseTool, "dist": DistTool, "area": AreaTool,
    "text": TextTool, "dimension": DimTool, "parcel": ParcelPickTool,
}

ALIASES: dict[str, str] = {
    "SELECT": "select", "L": "line", "LINE": "line", "PL": "polyline", "PLINE": "polyline", "REC": "rectangle",
    "RECTANG": "rectangle", "RECTANGLE": "rectangle", "C": "circle", "CIRCLE": "circle", "M": "move", "MOVE": "move",
    "CO": "copy", "CP": "copy", "COPY": "copy", "RO": "rotate", "ROTATE": "rotate", "SC": "scale", "SCALE": "scale",
    "O": "offset", "OFFSET": "offset", "TR": "trim", "TRIM": "trim", "EX": "extend", "EXTEND": "extend",
    "E": "erase", "ERASE": "erase", "DEL": "erase", "DI": "dist", "DIST": "dist", "AA": "area", "AREA": "area",
    "PP": "parcel", "PARCEL": "parcel", "T": "text", "DT": "text", "TEXT": "text", "DAL": "dimension", "DIM": "dimension", "DIMALIGNED": "dimension",
}


def parse_point(text: str, last: Pt | None) -> Pt | None:
    """Parse 'x,y', '@dx,dy' or '@dist<angle' (degrees). Returns None if not a point."""
    s = text.strip().replace(" ", "")
    rel = s.startswith("@")
    if rel:
        s = s[1:]
        if last is None:
            return None
    try:
        if "<" in s:
            dist, ang = (float(v) for v in s.split("<", 1))
            dx, dy = dist * math.cos(math.radians(ang)), dist * math.sin(math.radians(ang))
            base = last if rel and last else (0.0, 0.0)
            return base[0] + dx, base[1] + dy
        if "," in s:
            x, y = (float(v) for v in s.split(",", 1))
            if rel and last:
                return last[0] + x, last[1] + y
            return x, y
    except ValueError:
        return None
    return None


def screen_point(canvas, pt: Pt) -> QPointF:  # type: ignore[no-untyped-def]
    return canvas.to_screen(*pt)
