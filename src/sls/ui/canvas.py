"""CAD viewport: world/screen transform (Y up), cached painting, grid, snapping, selection."""
from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QTransform, QWheelEvent
from PySide6.QtWidgets import QWidget

from ..cad import ops
from ..cad.ops import fast_bbox
from ..cad.commands import UndoStack
from ..cad.model import Drawing, Entity, arc_points, flatten_bulge_polyline
from . import theme

PICK_PX = 7
SNAP_PX = 12
FILL_ROLES = {"setback_violation": 90, "licensed_area": 60}


def display_color(hex_color: str) -> QColor:
    """Screen colour for the dark canvas: very dark colours are lightened (data is unchanged),
    like AutoCAD's handling of black on a dark background."""
    c = QColor(hex_color)
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    if lum < 0.18:
        h, s_, light, a = c.getHsl()
        c.setHsl(max(h, 0), s_, max(light, 165), a)
    return c


class CadCanvas(QWidget):
    cursorMoved = Signal(float, float)
    selectionChanged = Signal()
    drawingEdited = Signal()
    message = Signal(str)
    toolChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(400, 300)
        self.drawing: Drawing | None = None
        self.stack: UndoStack | None = None
        self.scale = 1.0  # pixels per drawing unit
        self.cx = 0.0
        self.cy = 0.0
        self.ox = 0.0  # local origin for cached paths (precision with large coordinates)
        self.oy = 0.0
        self._cache: dict[str, tuple[int, QPainterPath]] = {}
        self._index = None  # (STRtree, [entity ids]) rebuilt lazily after edits
        self.selection: set[str] = set()
        self.hover_id: str | None = None
        self.grid_on = True
        self.snap_on = True
        self.grid_snap = False
        self.ortho_on = False
        self.show_lineweights = False
        self.snap_marker: tuple[float, float, str] | None = None
        self.cursor_world: tuple[float, float] = (0.0, 0.0)
        self._pan_last: QPointF | None = None
        self._drag_start: QPointF | None = None
        self._drag_now: QPointF | None = None
        self.tool = None  # set by set_tool
        self.highlight_ids: set[str] = set()
        self.annotations: list[tuple[float, float, str]] = []
        self.cadastre_pick = None  # optional callable(x, y) -> bool, set by the main window  # transient labels (e.g. parcel edge numbers)
        self.can_edit: Callable[[], bool] = lambda: True

    # -- document ----------------------------------------------------------------------

    def set_drawing(self, drawing: Drawing | None, stack: UndoStack | None) -> None:
        self.drawing = drawing
        self.stack = stack
        self.selection.clear()
        self._cache.clear()
        self._index = None
        ext = drawing.extents(only_visible=False) if drawing else None
        if ext:
            self.ox, self.oy = math.floor(ext[0]), math.floor(ext[1])
        self.zoom_extents()
        self.selectionChanged.emit()

    def invalidate(self) -> None:
        self._cache.clear()
        self.update()

    def edited(self) -> None:
        self._cache.clear()  # entities are replaced by copies on edit/undo; rebuild paths
        self._index = None
        if self.drawing:
            self.selection &= set(self.drawing.entities)
        self.update()
        self.drawingEdited.emit()
        self.selectionChanged.emit()

    # -- transforms --------------------------------------------------------------------

    def to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(self.width() / 2 + (x - self.cx) * self.scale, self.height() / 2 - (y - self.cy) * self.scale)

    def to_world(self, p: QPointF) -> tuple[float, float]:
        return (self.cx + (p.x() - self.width() / 2) / self.scale, self.cy - (p.y() - self.height() / 2) / self.scale)

    def view_extents(self) -> tuple[float, float, float, float]:
        """World window currently shown (x0, y0, x1, y1)."""
        x0, y1 = self.to_world(QPointF(0, 0))
        x1, y0 = self.to_world(QPointF(self.width(), self.height()))
        return (x0, y0, x1, y1)

    def _local_transform(self) -> QTransform:
        s = self.scale
        t = QTransform()
        t.translate(self.width() / 2 + (self.ox - self.cx) * s, self.height() / 2 - (self.oy - self.cy) * s)
        t.scale(s, -s)
        return t

    def zoom_extents(self) -> None:
        ext = self.drawing.extents() if self.drawing else None
        if not ext:
            self.scale, self.cx, self.cy = 10.0, self.ox, self.oy
        else:
            w = max(ext[2] - ext[0], 1e-6)
            h = max(ext[3] - ext[1], 1e-6)
            self.scale = 0.9 * min(max(self.width(), 100) / w, max(self.height(), 100) / h)
            self.cx, self.cy = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2
        self.update()

    def zoom_at(self, factor: float, screen_pt: QPointF | None = None) -> None:
        screen_pt = screen_pt or QPointF(self.width() / 2, self.height() / 2)
        wx, wy = self.to_world(screen_pt)
        self.scale = min(max(self.scale * factor, 1e-6), 1e7)
        nx, ny = self.to_world(screen_pt)
        self.cx += wx - nx
        self.cy += wy - ny
        self.update()

    # -- painting ------------------------------------------------------------------------

    def _path(self, e: Entity) -> QPainterPath:
        key = id(e)
        hit = self._cache.get(e.id)
        if hit and hit[0] == key:
            return hit[1]
        path = QPainterPath()
        ox, oy = self.ox, self.oy
        d = e.data

        def poly(pts: list[tuple[float, float]], closed: bool) -> None:
            if not pts:
                return
            path.moveTo(pts[0][0] - ox, pts[0][1] - oy)
            for x, y in pts[1:]:
                path.lineTo(x - ox, y - oy)
            if closed:
                path.closeSubpath()

        if e.kind == "line":
            poly([tuple(d["p1"]), tuple(d["p2"])], False)
        elif e.kind == "polyline":
            poly(flatten_bulge_polyline(d["points"], d.get("closed", False), 96), bool(d.get("closed")))
        elif e.kind == "circle":
            cx, cy = d["center"]
            r = d["radius"]
            path.addEllipse(QPointF(cx - ox, cy - oy), r, r)
        elif e.kind == "arc":
            poly(arc_points(d["center"], d["radius"], d["start_angle"], d["end_angle"], 96), False)
        elif e.kind == "dimension":
            (x1, y1), (x2, y2) = d["p1"], d["p2"]
            L = math.hypot(x2 - x1, y2 - y1) or 1.0
            nx, ny = -(y2 - y1) / L, (x2 - x1) / L
            off = d.get("offset", 1.0)
            a = (x1 + nx * off, y1 + ny * off)
            b = (x2 + nx * off, y2 + ny * off)
            poly([(x1, y1), a], False)
            poly([(x2, y2), b], False)
            poly([a, b], False)
        self._cache[e.id] = (key, path)
        return path

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.CANVAS_BG))
        if self.grid_on:
            self._paint_grid(p)
        if self.drawing is None:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, self.tr_empty())
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        xf = self._local_transform()
        view_w = QRectF(QPointF(*self._local(self.to_world(QPointF(0, self.height())))),
                        QPointF(*self._local(self.to_world(QPointF(self.width(), 0))))).normalized()
        texts: list[tuple[Entity, QColor]] = []
        by_layer: dict[str, list[Entity]] = {}
        for e in self.drawing.entities.values():
            by_layer.setdefault(e.layer, []).append(e)
        for layer in self.drawing.layers:
            if not layer.visible:
                continue
            base = display_color(layer.color)
            base.setAlphaF(max(0.05, 1.0 - layer.transparency))
            dashed = layer.linetype.upper().startswith("DASH") or layer.linetype.upper().startswith("HIDDEN")
            width = max(1.0, layer.lineweight * 3.78) if self.show_lineweights else 1.0
            fill_alpha = FILL_ROLES.get(layer.role)
            for e in by_layer.get(layer.name, ()):
                color = display_color(e.color) if e.color else base
                if e.kind == "text":
                    texts.append((e, color))
                    continue
                if e.kind == "point":
                    sp = self.to_screen(*e.data["position"])
                    p.setPen(QPen(color, 1))
                    p.drawLine(sp + QPointF(-3, 0), sp + QPointF(3, 0))
                    p.drawLine(sp + QPointF(0, -3), sp + QPointF(0, 3))
                    continue
                path = self._path(e)
                if not view_w.intersects(path.controlPointRect()) and path.controlPointRect().width() > 0:
                    continue
                selected = e.id in self.selection
                pen = QPen(QColor(theme.SELECT) if selected else color, width + (1.5 if selected else 0))
                pen.setCosmetic(True)
                if dashed or selected:
                    pen.setStyle(Qt.DashLine)
                if e.id == self.hover_id and not selected:
                    pen.setColor(color.lighter(160))
                    pen.setWidthF(width + 1)
                if e.id in self.highlight_ids:
                    pen = QPen(QColor(theme.WARN), 3)
                    pen.setCosmetic(True)
                p.setTransform(xf)
                if fill_alpha and e.is_closed_shape():
                    fill = QColor(color)
                    fill.setAlpha(fill_alpha)
                    p.fillPath(path, QBrush(fill))
                p.setPen(pen)
                p.drawPath(path)
                p.resetTransform()
                if e.kind == "dimension":
                    self._dim_text(p, e, color)
        for e, color in texts:
            self._paint_text(p, e, QColor(theme.SELECT) if e.id in self.selection else color)
        p.resetTransform()
        for ax, ay, label in self.annotations:
            sp = self.to_screen(ax, ay)
            r = QRectF(sp.x() - 11, sp.y() - 11, 22, 22)
            p.setBrush(QColor(224, 160, 64, 220))
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
            p.setPen(QColor("#111"))
            p.drawText(r, Qt.AlignCenter, label)
        p.setBrush(Qt.NoBrush)
        if self.tool is not None:
            self.tool.paint(p)
        if self._drag_start is not None and self._drag_now is not None:
            r = QRectF(self._drag_start, self._drag_now).normalized()
            crossing = self._drag_now.x() < self._drag_start.x()
            col = QColor(80, 200, 120, 40) if crossing else QColor(66, 165, 245, 40)
            p.fillRect(r, col)
            pen = QPen(col.darker(10).lighter(250), 1, Qt.DashLine if crossing else Qt.SolidLine)
            p.setPen(pen)
            p.drawRect(r)
        if self.snap_marker:
            sp = self.to_screen(self.snap_marker[0], self.snap_marker[1])
            p.setPen(QPen(QColor("#FFEB3B"), 1.5))
            p.setBrush(Qt.NoBrush)
            kind = self.snap_marker[2]
            if kind == "mid":
                p.drawPolygon([sp + QPointF(0, -6), sp + QPointF(6, 5), sp + QPointF(-6, 5)])
            elif kind in ("center", "quad"):
                p.drawEllipse(sp, 6, 6)
            else:
                p.drawRect(QRectF(sp.x() - 5, sp.y() - 5, 10, 10))
        self._paint_crosshair(p)

    def tr_empty(self) -> str:
        from ..core.i18n import tr

        return tr("canvas.empty")

    def _local(self, w: tuple[float, float]) -> tuple[float, float]:
        return w[0] - self.ox, w[1] - self.oy

    def _paint_text(self, p: QPainter, e: Entity, color: QColor) -> None:
        d = e.data
        px = d.get("height", 1.0) * self.scale
        if px < 3:
            return
        sp = self.to_screen(*d["position"])
        p.save()
        p.translate(sp)
        p.rotate(-d.get("rotation", 0.0))
        f = QFont(self.font())
        f.setPixelSize(max(1, int(px)))
        p.setFont(f)
        p.setPen(color)
        p.drawText(QPointF(0, 0), d.get("text", ""))
        p.restore()

    def _dim_text(self, p: QPainter, e: Entity, color: QColor) -> None:
        d = e.data
        (x1, y1), (x2, y2) = d["p1"], d["p2"]
        L = math.hypot(x2 - x1, y2 - y1)
        if L == 0:
            return
        factor = self._unit_factor()
        txt = d.get("text_override") or (f"{L * factor:.2f} m" if factor else f"{L:.3f}")
        nx, ny = -(y2 - y1) / L, (x2 - x1) / L
        off = d.get("offset", 1.0)
        mid = self.to_screen((x1 + x2) / 2 + nx * off, (y1 + y2) / 2 + ny * off)
        ang = -math.degrees(math.atan2(y2 - y1, x2 - x1))
        if ang > 90 or ang < -90:
            ang += 180
        p.save()
        p.translate(mid)
        p.rotate(ang)
        f = QFont(self.font())
        f.setPixelSize(max(9, min(16, int(d.get("text_height", 0.25) * self.scale))))
        p.setFont(f)
        p.setPen(color)
        w = p.fontMetrics().horizontalAdvance(txt)
        p.drawText(QPointF(-w / 2, -3), txt)
        p.restore()

    def _unit_factor(self) -> float | None:
        if self.drawing is None:
            return None
        try:
            return self.drawing.units.unit.to_meter if self.drawing.units.verified else None
        except Exception:  # noqa: BLE001 - unknown units: show raw drawing units
            return None

    def grid_spacing(self) -> float:
        target_px = 18.0
        raw = target_px / self.scale
        exp = math.floor(math.log10(raw))
        for m in (1, 2, 5, 10):
            if m * 10 ** exp >= raw:
                return m * 10 ** exp
        return 10 ** (exp + 1)

    def _paint_grid(self, p: QPainter) -> None:
        if self.drawing is None:
            return
        step = self.grid_spacing()
        x0, y0 = self.to_world(QPointF(0, self.height()))
        x1, y1 = self.to_world(QPointF(self.width(), 0))
        minor = QPen(QColor(255, 255, 255, 14), 1)
        major = QPen(QColor(255, 255, 255, 30), 1)
        i0, i1 = math.floor(x0 / step), math.ceil(x1 / step)
        j0, j1 = math.floor(y0 / step), math.ceil(y1 / step)
        if (i1 - i0) > 400 or (j1 - j0) > 400:
            return
        for i in range(i0, i1 + 1):
            p.setPen(major if i % 5 == 0 else minor)
            sx = self.to_screen(i * step, 0).x()
            p.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
        for j in range(j0, j1 + 1):
            p.setPen(major if j % 5 == 0 else minor)
            sy = self.to_screen(0, j * step).y()
            p.drawLine(QPointF(0, sy), QPointF(self.width(), sy))

    def _paint_crosshair(self, p: QPainter) -> None:
        if not self.underMouse():
            return
        sp = self.to_screen(*self.cursor_world)
        p.setPen(QPen(QColor(255, 255, 255, 120), 1))
        p.drawLine(QPointF(sp.x() - 14, sp.y()), QPointF(sp.x() + 14, sp.y()))
        p.drawLine(QPointF(sp.x(), sp.y() - 14), QPointF(sp.x(), sp.y() + 14))
        p.drawRect(QRectF(sp.x() - 3, sp.y() - 3, 6, 6))

    # -- picking and snapping --------------------------------------------------------------

    def visible_entities(self) -> list[Entity]:
        if self.drawing is None:
            return []
        vis = {layer.name for layer in self.drawing.layers if layer.visible}
        return [e for e in self.drawing.entities.values() if e.layer in vis]

    def _ensure_index(self):  # type: ignore[no-untyped-def]
        if self._index is None and self.drawing is not None:
            from shapely import STRtree, box

            ids, boxes = [], []
            for e in self.drawing.entities.values():
                try:
                    b = fast_bbox(e)
                except (KeyError, ValueError, IndexError):
                    continue
                ids.append(e.id)
                boxes.append(box(*b))
            self._index = (STRtree(boxes) if boxes else None, ids)
        return self._index

    def entities_in(self, x0: float, y0: float, x1: float, y1: float) -> list[Entity]:
        """Visible entities whose bounding box touches the world rectangle (spatial index)."""
        if self.drawing is None:
            return []
        tree, ids = self._ensure_index()
        if tree is None:
            return []
        from shapely import box

        vis = {layer.name for layer in self.drawing.layers if layer.visible}
        out = []
        for i in tree.query(box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))):
            e = self.drawing.entities.get(ids[int(i)])
            if e is not None and e.layer in vis:
                out.append(e)
        return out

    def pick(self, screen_pt: QPointF) -> Entity | None:
        x, y = self.to_world(screen_pt)
        tol = PICK_PX / self.scale
        best, best_d = None, tol
        for e in self.entities_in(x - tol, y - tol, x + tol, y + tol):
            dist = ops.hit_distance(e, x, y)
            if dist <= best_d:
                best, best_d = e, dist
        return best

    def window_select(self, a: QPointF, b: QPointF) -> list[str]:
        (x0, y0), (x1, y1) = self.to_world(a), self.to_world(b)
        xmin, xmax, ymin, ymax = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        crossing = b.x() < a.x()
        out = []
        for e in self.entities_in(xmin, ymin, xmax, ymax):
            bx0, by0, bx1, by1 = ops.bbox(e)
            inside = bx0 >= xmin and bx1 <= xmax and by0 >= ymin and by1 <= ymax
            if inside or (crossing and self._crosses(e, xmin, ymin, xmax, ymax)):
                out.append(e.id)
        return out

    @staticmethod
    def _crosses(e: Entity, xmin: float, ymin: float, xmax: float, ymax: float) -> bool:
        from shapely.geometry import box

        return ops.shapely_of(e).intersects(box(xmin, ymin, xmax, ymax))

    def snap(self, screen_pt: QPointF, base: tuple[float, float] | None = None) -> tuple[float, float]:
        """Snap to object points, then grid; apply ortho relative to ``base``."""
        x, y = self.to_world(screen_pt)
        self.snap_marker = None
        if self.snap_on:
            tol = SNAP_PX / self.scale
            best = None
            for e in self.entities_in(x - tol, y - tol, x + tol, y + tol):
                for sx, sy, kind in ops.snap_points(e):
                    dd = math.hypot(sx - x, sy - y)
                    if dd <= tol and (best is None or dd < best[0]):
                        best = (dd, sx, sy, kind)
            if best:
                self.snap_marker = (best[1], best[2], best[3])
                return best[1], best[2]
        if self.grid_snap and self.grid_on:
            step = self.grid_spacing()
            x, y = round(x / step) * step, round(y / step) * step
        if self.ortho_on and base is not None:
            if abs(x - base[0]) >= abs(y - base[1]):
                y = base[1]
            else:
                x = base[0]
        return x, y

    # -- tools & input ---------------------------------------------------------------------------

    def set_tool(self, tool) -> None:  # type: ignore[no-untyped-def]
        if self.tool is not None:
            self.tool.deactivate()
        self.tool = tool
        tool.activate(self)
        self.toolChanged.emit(tool.name)
        self.update()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        self.setFocus()
        if ev.button() == Qt.MiddleButton or (ev.button() == Qt.LeftButton and ev.modifiers() & Qt.AltModifier):
            self._pan_last = ev.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() == Qt.RightButton:
            if self.tool is not None:
                self.tool.on_enter()
            return
        if ev.button() == Qt.LeftButton and self.tool is not None:
            if self.tool.wants_raw_press:
                self.tool.on_press(ev)
            else:
                self.tool.on_point(self.snap(ev.position(), self.tool.base_point()))
            self.update()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._pan_last is not None:
            delta = ev.position() - self._pan_last
            self._pan_last = ev.position()
            self.cx -= delta.x() / self.scale
            self.cy += delta.y() / self.scale
            self.update()
            return
        if self.tool is not None and self.tool.snaps_on_move:
            self.cursor_world = self.snap(ev.position(), self.tool.base_point())
        else:
            self.snap_marker = None
            self.cursor_world = self.to_world(ev.position())
        if self.tool is not None:
            self.tool.on_move(ev)
        self.cursorMoved.emit(*self.cursor_world)
        self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if self._pan_last is not None and ev.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._pan_last = None
            self.unsetCursor()
            return
        if self.tool is not None and self.tool.wants_raw_press:
            self.tool.on_release(ev)
            self.update()

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MiddleButton:
            self.zoom_extents()

    def wheelEvent(self, ev: QWheelEvent) -> None:
        factor = 1.2 if ev.angleDelta().y() > 0 else 1 / 1.2
        self.zoom_at(factor, ev.position())

    def keyPressEvent(self, ev) -> None:  # type: ignore[no-untyped-def]
        if ev.key() == Qt.Key_Escape and self.tool is not None:
            self.tool.on_escape()
            self.update()
            return
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and self.tool is not None:
            self.tool.on_enter()
            self.update()
            return
        super().keyPressEvent(ev)

    def resizeEvent(self, ev) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:  # type: ignore[no-untyped-def]
        self.snap_marker = None
        self.update()
