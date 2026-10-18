"""Main application window: CAD workspace + licensing case workflow."""
from __future__ import annotations

import copy
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QLabel, QMainWindow, QMessageBox, QToolBar,
                               QWidget)

from ..cad.commands import ReplaceEntities, UndoStack
from ..cad.dxf_io import export_dxf
from ..cad.model import Drawing
from ..core.errors import SlsError
from ..core.i18n import current_language, tr
from ..core.settings import save_settings
from ..persistence.workspace import Workspace
from ..services.case_service import CaseService, LoadedDrawing
from .canvas import CadCanvas
from .dialogs.common import show_error
from .inspector import CommandLine, Inspector
from .layer_panel import LayerPanel
from .tools import ALIASES, TOOLS, SelectTool, parse_point


class MainWindow(QMainWindow):
    def __init__(self, ws: Workspace) -> None:
        super().__init__()
        self.ws = ws
        self.service = CaseService(ws)
        self.case_id: str | None = None
        self.loaded: LoadedDrawing | None = None
        self.stack: UndoStack | None = None
        self.resize(1500, 920)

        self.canvas = CadCanvas()
        self.canvas.can_edit = lambda: self.case_id is not None and self.ws.user.can("edit_drawing")
        self.canvas.cadastre_pick = self._pick_from_register
        self.setCentralWidget(self.canvas)
        self.layers = LayerPanel()
        self.layers.set_canvas(self.canvas)
        self.inspector = Inspector()
        self.inspector.props.set_canvas(self.canvas)
        self.cmd = CommandLine()
        self._dock(tr("dock.layers"), self.layers, Qt.LeftDockWidgetArea)
        self._dock(tr("dock.inspector"), self.inspector, Qt.RightDockWidgetArea)
        self._dock(tr("dock.command"), self.cmd, Qt.BottomDockWidgetArea)

        self.status_coords = QLabel()
        self._cursor_crs = ""
        self.status_tool = QLabel()
        self.status_layer = QLabel()
        self.status_units = QLabel()
        self.status_modes = QLabel()
        self.status_user = QLabel()
        for w in (self.status_coords, self.status_tool, self.status_layer, self.status_units, self.status_modes,
                  self.status_user):
            self.statusBar().addPermanentWidget(w)

        self._build_actions()
        self.canvas.cursorMoved.connect(self._on_cursor)
        self.canvas.selectionChanged.connect(self.inspector.props.refresh)
        self.canvas.drawingEdited.connect(self._on_edited)
        self.canvas.message.connect(lambda m: self.cmd.message(m))
        self.canvas.toolChanged.connect(self._on_tool)
        self.cmd.submitted.connect(self.run_command)
        self.layers.layersChanged.connect(self._update_status)
        self.canvas.set_tool(SelectTool())
        self._update_title()
        self._update_status()
        self._refresh_case_summary()
        self.cmd.message(tr("app.welcome"))

    # ------------------------------------------------------------------ construction

    def _dock(self, title: str, widget: QWidget, area) -> QDockWidget:  # type: ignore[no-untyped-def]
        d = QDockWidget(title, self)
        d.setObjectName(title)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(area, d)
        return d

    def _act(self, key: str, slot, shortcut: str | None = None, checkable: bool = False,  # type: ignore[no-untyped-def]
             needs_case: bool = False) -> QAction:
        a = QAction(tr(key).replace("&", "&&"), self)  # literal '&', not a mnemonic marker
        a.setToolTip(tr(key) + (f" ({shortcut})" if shortcut else ""))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        if needs_case:
            self._case_actions.append(a)
        return a

    def _build_actions(self) -> None:
        self._case_actions: list[QAction] = []
        mb = self.menuBar()
        m_file = mb.addMenu(tr("menu.file"))
        m_file.addAction(self._act("act.new_case", self.new_case, "Ctrl+N"))
        m_file.addAction(self._act("act.open_case", self.open_case, "Ctrl+O"))
        m_file.addAction(self._act("act.save", self.save_drawing, "Ctrl+S", needs_case=True))
        m_file.addSeparator()
        m_file.addAction(self._act("act.import_drawing", self.import_drawing, "Ctrl+I", needs_case=True))
        m_file.addAction(self._act("act.new_drawing", self.new_drawing, needs_case=True))
        m_file.addAction(self._act("act.export_dxf", self.export_dxf, needs_case=True))
        m_file.addSeparator()
        m_file.addAction(self._act("act.demo_case", self.create_demo_case))
        m_file.addSeparator()
        m_file.addAction(self._act("act.backup", self.backup_now))
        m_file.addAction(self._act("act.restore", self.restore_backup))
        m_file.addAction(self._act("act.settings", self.open_settings))
        m_file.addAction(self._act("act.users", self.manage_users))
        m_file.addAction(self._act("act.case_fields", self.manage_fields))
        m_file.addAction(self._act("act.cadastre_import", self.import_cadastre))
        m_file.addSeparator()
        m_file.addAction(self._act("act.exit", self.close, "Ctrl+Q"))

        m_edit = mb.addMenu(tr("menu.edit"))
        self.a_undo = self._act("act.undo", self.undo, "Ctrl+Z", needs_case=True)
        self.a_redo = self._act("act.redo", self.redo, "Ctrl+Y", needs_case=True)
        m_edit.addAction(self.a_undo)
        m_edit.addAction(self.a_redo)
        m_edit.addAction(self._act("act.erase", lambda: self.activate_tool("erase"), "Delete", needs_case=True))
        m_edit.addAction(self._act("act.select_all", self.select_all, "Ctrl+A", needs_case=True))

        m_view = mb.addMenu(tr("menu.view"))
        m_view.addAction(self._act("act.zoom_extents", self.canvas.zoom_extents, "Ctrl+E"))
        self.a_grid = self._act("act.grid", self._toggle("grid_on"), "F7", checkable=True)
        self.a_snap = self._act("act.osnap", self._toggle("snap_on"), "F3", checkable=True)
        self.a_gsnap = self._act("act.grid_snap", self._toggle("grid_snap"), "F9", checkable=True)
        self.a_ortho = self._act("act.ortho", self._toggle("ortho_on"), "F8", checkable=True)
        self.a_lwt = self._act("act.lineweights", self._toggle("show_lineweights"), checkable=True)
        for a, on in ((self.a_grid, True), (self.a_snap, True), (self.a_gsnap, False), (self.a_ortho, False),
                      (self.a_lwt, False)):
            a.setChecked(on)
            m_view.addAction(a)

        m_case = mb.addMenu(tr("menu.case"))
        for key, slot in (("act.case_details", self.edit_case), ("act.rules", self.open_rules),
                          ("act.boundary", self.open_boundary), ("act.setback_override", self.open_override),
                          ("act.calculate", self.calculate), ("act.results", self.show_results), ("act.licenses", self.open_licenses),
                          ("act.media", self.open_media), ("act.report", self.open_report),
                          ("act.find_parcel", self.find_parcel),
                          ("act.regulatory_check", self.regulatory_check),
                          ("act.regulatory_transform", self.regulatory_transform)):
            m_case.addAction(self._act(key, slot, needs_case=key != "act.rules"))

        m_help = mb.addMenu(tr("menu.help"))
        m_help.addAction(self._act("act.limitations", self.show_limitations))
        m_help.addAction(self._act("act.about", self.show_about))

        tb_file = QToolBar(tr("tb.case"))
        tb_file.setObjectName("tb_case")
        for key, slot in (("act.new_case", self.new_case), ("act.open_case", self.open_case),
                          ("act.save", self.save_drawing), ("act.import_drawing", self.import_drawing)):
            tb_file.addAction(self._find_or_make(key, slot))
        self.addToolBar(tb_file)

        tb_draw = QToolBar(tr("tb.draw"))
        tb_draw.setObjectName("tb_draw")
        self.tool_group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for name in ("select", "line", "polyline", "rectangle", "circle", "text", "dimension"):
            tb_draw.addAction(self._tool_action(name))
        self.addToolBar(tb_draw)
        tb_mod = QToolBar(tr("tb.modify"))
        tb_mod.setObjectName("tb_modify")
        for name in ("move", "copy", "rotate", "scale", "offset", "trim", "extend", "erase"):
            tb_mod.addAction(self._tool_action(name))
        tb_mod.addSeparator()
        tb_mod.addAction(self.a_undo)
        tb_mod.addAction(self.a_redo)
        self.addToolBar(tb_mod)
        tb_meas = QToolBar(tr("tb.measure"))
        tb_meas.setObjectName("tb_measure")
        for name in ("dist", "area", "parcel"):
            tb_meas.addAction(self._tool_action(name))
        tb_meas.addAction(self._find_or_make("act.zoom_extents", self.canvas.zoom_extents))
        self.addToolBar(tb_meas)
        self.addToolBarBreak()
        tb_lic = QToolBar(tr("tb.licensing"))
        tb_lic.setObjectName("tb_licensing")
        for key, slot in (("act.case_details", self.edit_case), ("act.find_parcel", self.find_parcel),
                          ("act.rules", self.open_rules), ("act.boundary", self.open_boundary),
                          ("act.calculate", self.calculate), ("act.licenses", self.open_licenses),
                          ("act.media", self.open_media), ("act.report", self.open_report)):
            tb_lic.addAction(self._find_or_make(key, slot))
        self.addToolBar(tb_lic)
        self._enable_case_actions()

    def _find_or_make(self, key: str, slot) -> QAction:  # type: ignore[no-untyped-def]
        for a in self.findChildren(QAction):
            if a.text() == tr(key).replace("&", "&&"):
                return a
        return self._act(key, slot)

    def _tool_action(self, name: str) -> QAction:
        a = QAction(tr(f"tool_name.{name}"), self)
        alias = next((k for k, v in ALIASES.items() if v == name and len(k) <= 3), "")
        a.setToolTip(f"{tr('tool_name.' + name)}  [{alias}]" if alias else tr(f"tool_name.{name}"))
        a.setCheckable(True)
        a.triggered.connect(lambda _=False, n=name: self.activate_tool(n))
        self.tool_group.addAction(a)
        self.tool_actions[name] = a
        if name != "select":
            self._case_actions.append(a)
        return a

    def _toggle(self, attr: str):  # type: ignore[no-untyped-def]
        def fn(checked: bool) -> None:
            setattr(self.canvas, attr, checked)
            self.canvas.update()
            self._update_status()
        return fn

    def _enable_case_actions(self) -> None:
        has_case = self.case_id is not None
        for a in self._case_actions:
            a.setEnabled(has_case)

    # ------------------------------------------------------------------ status / title

    def _update_title(self) -> None:
        title = tr("app.title")
        if self.case_id:
            case = self.ws.cases.get(self.case_id)
            title = f"{case.case_number} — {title}"
            if self.loaded:
                title = f"{self.loaded.record.name} r{self.loaded.revision.revision}" + \
                        ("*" if self.stack and self.stack.dirty else "") + " | " + title
        self.setWindowTitle(title)

    def _update_status(self) -> None:
        d = self.canvas.drawing
        self._cursor_crs = ""
        if self.case_id:
            u = self.ws.cases.get(self.case_id).units
            if u.crs_id and u.crs_kind.value == "projected" and u.unit.value == "m":
                self._cursor_crs = u.crs_id
        self.status_layer.setText(f"{tr('status.layer')}: {d.active_layer}" if d else "")
        if self.case_id:
            u = self.ws.cases.get(self.case_id).units
            txt = f"{tr('status.units')}: {tr('unit_name.' + u.unit.value)}" + (f" · {u.crs_id}" if u.crs_id else "")
            txt += " ✓" if u.verified else f" ({tr('props.units_unverified')})"
            self.status_units.setText(txt)
        else:
            self.status_units.setText("")
        modes = [m for m, on in (("GRID", self.canvas.grid_on), ("OSNAP", self.canvas.snap_on),
                                 ("SNAP", self.canvas.grid_snap), ("ORTHO", self.canvas.ortho_on)) if on]
        self.status_modes.setText(" ".join(modes))
        self.status_user.setText(f"{self.ws.user.display_name} ({tr('users.role.' + self.ws.user.role)})")

    def _on_cursor(self, x: float, y: float) -> None:
        text = f"E {x:,.3f}   N {y:,.3f}"
        code = self._cursor_crs
        if code:
            try:
                from ..core.crs import to_lonlat

                lon, lat = to_lonlat(code, x, y)
                if -90 <= lat <= 90:
                    text += f"   ≈ {lat:.6f}°N {lon:.6f}°E (WGS 84)"
            except Exception:  # noqa: BLE001 - display only; never affects data
                pass
        self.status_coords.setText(text)

    def _on_tool(self, name: str) -> None:
        self.status_tool.setText(f"{tr('status.tool')}: {tr('tool_name.' + name)}")
        if name in self.tool_actions:
            self.tool_actions[name].setChecked(True)

    def _on_edited(self) -> None:
        self._update_title()
        self._update_status()
        self.layers.refresh()
        self.inspector.props.refresh()

    # ------------------------------------------------------------------ case lifecycle

    def _confirm_discard(self) -> bool:
        if self.stack and self.stack.dirty:
            r = QMessageBox.question(self, tr("app.title"), tr("app.unsaved"),
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return False
            if r == QMessageBox.Save:
                return self.save_drawing()
        return True

    def open_case_id(self, case_id: str) -> None:
        self.case_id = case_id
        self.loaded = self.service.survey_drawing(case_id)
        self._set_loaded(self.loaded)
        self._enable_case_actions()
        case = self.ws.cases.get(case_id)
        self.cmd.message(tr("app.case_opened", number=case.case_number), "ok")
        if self.loaded is None:
            self.cmd.message(tr("app.no_drawing_hint"), "warn")

    def _set_loaded(self, loaded: LoadedDrawing | None) -> None:
        self.loaded = loaded
        if loaded is not None:
            drawing = loaded.drawing
            drawing.units = self.ws.cases.get(self.case_id).units  # case units are authoritative
            self.stack = UndoStack(drawing)
            self.canvas.set_drawing(drawing, self.stack)
        else:
            self.stack = None
            self.canvas.set_drawing(None, None)
        self.canvas.set_tool(SelectTool())
        self.layers.refresh()
        self._update_title()
        self._update_status()
        self._refresh_case_summary()

    def new_case(self) -> None:
        if not self._confirm_discard():
            return
        from .dialogs.case_dialogs import CaseDialog

        dlg = CaseDialog(self.ws, None, self)
        if dlg.exec():
            self.open_case_id(dlg.case.id)

    def open_case(self) -> None:
        if not self._confirm_discard():
            return
        from .dialogs.case_dialogs import CaseBrowser

        dlg = CaseBrowser(self.ws, self)
        if dlg.exec() and dlg.selected_id:
            self.open_case_id(dlg.selected_id)

    def edit_case(self) -> None:
        from .dialogs.case_dialogs import CaseDialog

        dlg = CaseDialog(self.ws, self.ws.cases.get(self.case_id), self)
        if dlg.exec():
            if self.canvas.drawing is not None:
                self.canvas.drawing.units = dlg.case.units
                self.canvas.invalidate()
            self._update_title()
            self._update_status()
            self._refresh_case_summary()
            self.inspector.props.refresh()

    def save_drawing(self) -> bool:
        if self.loaded is None or self.canvas.drawing is None:
            return True
        try:
            rev = self.ws.drawings.save_revision(self.ws.user, self.loaded.record.id, self.canvas.drawing,
                                                 tr("app.saved_from_editor"))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return False
        self.loaded = LoadedDrawing(self.loaded.record, rev, self.canvas.drawing)
        self.stack.mark_clean()
        self._update_title()
        self.cmd.message(tr("app.saved", rev=rev.revision), "ok")
        return True

    # ------------------------------------------------------------------ drawings

    def import_drawing(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("act.import_drawing"), "",
                                              "CAD (*.dxf *.dwg);;DXF (*.dxf);;DWG (*.dwg)")
        if not path:
            return
        from .dialogs.import_dialog import ImportPreviewDialog

        try:
            drawing, report = self.run_busy(tr("app.importing", name=Path(path).name),
                                            lambda: self.service.preview_import(Path(path), self.case_id))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        dlg = ImportPreviewDialog(drawing, report, self)
        if not dlg.exec():
            return
        dlg.apply_mapping()
        role = dlg.role.currentData()
        if role == "survey" and not self._confirm_discard():
            return
        try:
            loaded = self.run_busy(tr("app.saving_import"), lambda: self.service.import_drawing(
                self.case_id, Path(path), role, drawing, report, dlg.keep.isChecked()))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.cmd.message(tr("app.imported", name=Path(path).name, n=len(loaded.drawing.entities)), "ok")
        for w in report.warnings:
            self.cmd.message(w, "warn")
        if role == "survey":
            self._set_loaded(loaded)
            self.cmd.message(tr("app.verify_units_hint"), "warn")
        else:
            self.cmd.message(tr("app.regulatory_imported"), "info")

    def run_busy(self, label: str, fn):  # type: ignore[no-untyped-def]
        """Run ``fn`` in a worker thread with a modal busy dialog; re-raises its exception."""
        import threading

        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QProgressDialog

        result: dict[str, object] = {}

        def work() -> None:
            try:
                result["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the UI thread below
                result["error"] = exc

        dlg = QProgressDialog(label, "", 0, 0, self)
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while thread.is_alive():
            QApplication.processEvents(QEventLoop.AllEvents, 50)
            thread.join(0.05)
        dlg.close()
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        return result.get("value")

    def new_drawing(self) -> None:
        if not self._confirm_discard():
            return
        self._set_loaded(self.service.new_blank_drawing(self.case_id))

    def export_dxf(self) -> None:
        if self.canvas.drawing is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("act.export_dxf"), "drawing.dxf", "DXF (*.dxf)")
        if not path:
            return
        try:
            warnings = export_dxf(self.canvas.drawing, Path(path))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.cmd.message(tr("app.exported", path=path), "ok")
        for w in warnings:
            self.cmd.message(w, "warn")

    def regulatory_check(self) -> None:
        """Compare survey and regulatory drawing extents. No transformation is ever applied silently."""
        regs = self.ws.drawings.for_case(self.case_id, "regulatory")
        if not regs or self.canvas.drawing is None:
            QMessageBox.information(self, tr("act.regulatory_check"), tr("reg.none"))
            return
        reg = self.service.load_drawing(regs[-1].id)
        a = self.canvas.drawing.extents(only_visible=False)
        b = reg.drawing.extents(only_visible=False)
        if not a or not b:
            return
        overlap = not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
        dx = (b[0] + b[2]) / 2 - (a[0] + a[2]) / 2
        dy = (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2
        msg = tr("reg.report", a=f"{a[0]:,.2f}, {a[1]:,.2f} … {a[2]:,.2f}, {a[3]:,.2f}",
                 b=f"{b[0]:,.2f}, {b[1]:,.2f} … {b[2]:,.2f}, {b[3]:,.2f}", dx=f"{dx:,.3f}", dy=f"{dy:,.3f}",
                 units=reg.drawing.units.unit.value)
        msg += "\n\n" + (tr("reg.overlap") if overlap else tr("reg.no_overlap"))
        if not overlap:
            QMessageBox.warning(self, tr("act.regulatory_check"), msg)
            return
        if QMessageBox.question(self, tr("act.regulatory_check"), msg + "\n\n" + tr("reg.insert_question")) \
                != QMessageBox.Yes:
            return
        added = []
        margin = max(a[2] - a[0], a[3] - a[1], 50.0)  # include the surroundings (at least 50 drawing units)
        window = (a[0] - margin, a[1] - margin, a[2] + margin, a[3] + margin)
        subset = self.service.entities_in_window(reg.drawing, window)
        used_layers = {e.layer for e in subset}
        for layer in reg.drawing.layers:
            if layer.name not in used_layers:
                continue
            name = f"REG_{layer.name}"
            if not self.canvas.drawing.has_layer(name):
                nl = copy.deepcopy(layer)
                nl.name = name
                nl.role = layer.role if layer.role in ("road", "neighbor_parcel", "property_boundary") else "reference"
                nl.source_name = f"{regs[-1].name}:{layer.name}"
                self.canvas.drawing.add_layer(nl)
        for e in subset:
            c = e.clone(new_id=True)
            c.layer = f"REG_{e.layer}"
            c.meta = {**c.meta, "source": "regulatory", "regulatory_drawing_id": regs[-1].id,
                      "regulatory_entity_id": e.id}
            added.append(c)
        self.cmd.message(tr("reg.subset", n=len(subset), total=len(reg.drawing.entities)), "info")
        from ..cad.commands import AddEntities

        self.stack.push(AddEntities(added, "insert regulatory reference"))
        self.canvas.edited()
        self.cmd.message(tr("reg.inserted", n=len(added)), "ok")

    # ------------------------------------------------------------------ tools / commands

    def activate_tool(self, name: str) -> None:
        if name != "select" and self.canvas.drawing is None:
            self.cmd.message(tr("tool.no_drawing"), "warn")
            self.tool_actions["select"].setChecked(True)
            return
        self.canvas.set_tool(TOOLS[name]())
        self.canvas.setFocus()

    def undo(self) -> None:
        if self.stack and (cmd := self.stack.undo()):
            self.canvas.edited()
            self.cmd.message(tr("app.undone", what=cmd.label))

    def redo(self) -> None:
        if self.stack and (cmd := self.stack.redo()):
            self.canvas.edited()
            self.cmd.message(tr("app.redone", what=cmd.label))

    def select_all(self) -> None:
        self.canvas.selection = {e.id for e in self.canvas.visible_entities()}
        self.canvas.selectionChanged.emit()
        self.canvas.update()

    def run_command(self, text: str) -> None:
        t = text.strip()
        tool = self.canvas.tool
        if not t:
            if tool is not None:
                tool.on_enter()
            self.canvas.update()
            return
        upper = t.upper()
        if tool is not None and not isinstance(tool, SelectTool):
            pt = parse_point(t, tool.base_point() or (tool.points[-1] if tool.points else None))
            if pt is not None:
                tool.on_point(pt)
                self.canvas.update()
                return
            try:
                value = float(t.replace("٫", "."))
            except ValueError:
                value = None
            if value is not None and tool.on_number(value):
                self.canvas.update()
                return
            if tool.on_text(t):
                self.canvas.update()
                return
        builtin = {"U": self.undo, "UNDO": self.undo, "REDO": self.redo, "ZE": self.canvas.zoom_extents,
                   "Z E": self.canvas.zoom_extents, "ZOOM E": self.canvas.zoom_extents,
                   "ZOOM EXTENTS": self.canvas.zoom_extents, "CALC": self.calculate, "REPORT": self.open_report,
                   "SAVE": self.save_drawing, "QSAVE": self.save_drawing,
                   "GRID": lambda: self.a_grid.trigger(), "OSNAP": lambda: self.a_snap.trigger(),
                   "ORTHO": lambda: self.a_ortho.trigger(), "LA": lambda: self.layers.search.setFocus(),
                   "LAYER": lambda: self.layers.search.setFocus()}
        if upper in builtin:
            builtin[upper]()
            return
        if upper in ALIASES:
            self.activate_tool(ALIASES[upper])
            return
        self.cmd.message(tr("cmd.unknown", cmd=t), "error")

    # ------------------------------------------------------------------ licensing workflow

    def open_rules(self) -> None:
        from .dialogs.rules_dialog import RulesDialog

        RulesDialog(self.ws, self).exec()
        self._refresh_case_summary()

    def open_boundary(self) -> None:
        if self.canvas.drawing is None:
            self.cmd.message(tr("app.no_drawing_hint"), "warn")
            return
        from .dialogs.boundary_dialog import BoundaryDialog

        rule, _vid, _ver, _err = self.service.applied_rule(self.case_id)
        extra = list(rule.setbacks) if rule else []
        suggestions = None
        latest = self.ws.calculations.latest(self.case_id)
        if latest is not None:
            suggestions = latest.results.get("edge_type_suggestions")
        dlg = BoundaryDialog(self.ws, self.case_id, self.canvas.drawing, set(self.canvas.selection), extra,
                             suggestions, self.canvas, self)
        dlg.setModal(False)
        dlg.exec()
        self._refresh_case_summary()

    def open_override(self) -> None:
        from .dialogs.override_dialog import OverrideDialog

        OverrideDialog(self.ws, self.case_id, self).exec()
        self._refresh_case_summary()

    def calculate(self) -> None:
        if self.stack and self.stack.dirty:
            if QMessageBox.question(self, tr("act.calculate"), tr("calc.save_first")) != QMessageBox.Yes:
                return
            if not self.save_drawing():
                return
        try:
            calc = self.service.run_calculation(self.case_id)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        status = calc.results.get("status")
        self.cmd.message(tr("calc.done", rev=calc.revision, status=tr(f"calc.status.{status}")),
                         {"ok": "ok", "partial": "warn"}.get(status, "error"))
        for e in calc.results.get("errors", []):
            self.cmd.message(f"[{e['section']}] {e['message']}", "error")
        self._refresh_case_summary()
        self._show_results(calc)

    def show_results(self) -> None:
        calc = self.ws.calculations.latest(self.case_id)
        if calc is None:
            self.cmd.message(tr("rpt.no_calculation"), "warn")
            return
        self._show_results(calc)

    def _show_results(self, calc) -> None:  # type: ignore[no-untyped-def]
        from .dialogs.results_dialog import ResultsDialog

        dlg = ResultsDialog(self.ws, calc, self)
        dlg.applyGeometry.connect(self.apply_derived)
        dlg.show()

    def apply_derived(self, calc_id: str) -> None:
        if self.canvas.drawing is None:
            return
        calc = self.ws.calculations.get(calc_id)
        d = self.canvas.drawing
        before = [e.clone() for e in d.entities.values() if e.meta.get("source") == "derived"]
        trial = Drawing.from_dict(d.to_dict())
        self.service.apply_derived_geometry(trial, calc.results, calc_id)
        after = [e for e in trial.entities.values() if e.meta.get("source") == "derived"]
        for layer in trial.layers:
            if not d.has_layer(layer.name):
                d.add_layer(copy.deepcopy(layer))
        cmd = ReplaceEntities(before, after, "generated setback geometry")
        self.stack.push(cmd)
        self.canvas.edited()
        self.layers.refresh()
        self.cmd.message(tr("calc.applied", n=len(after)), "ok")

    def open_licenses(self) -> None:
        from .dialogs.licenses_dialog import LicensesDialog

        LicensesDialog(self.ws, self.case_id, self).exec()
        self._refresh_case_summary()

    def open_media(self) -> None:
        from .dialogs.media_dialog import MediaDialog

        MediaDialog(self.ws, self.case_id, self).exec()

    def open_report(self) -> None:
        if self.stack and self.stack.dirty and QMessageBox.question(
                self, tr("act.report"), tr("report.unsaved_drawing")) == QMessageBox.Yes:
            self.save_drawing()
        from .dialogs.report_dialog import ReportDialog

        ReportDialog(self.ws, self.case_id, self).exec()

    def _refresh_case_summary(self) -> None:
        if not self.case_id:
            self.inspector.show_case_summary(f"<p style='color:#8C96A3'>{escape(tr('inspector.no_case'))}</p>")
            return
        ws = self.ws
        case = ws.cases.get(self.case_id)
        rule, vid, verified, err = self.service.applied_rule(self.case_id)
        parcel = ws.drawings.parcel(self.case_id)
        lics = ws.licenses.list(self.case_id)
        calc = ws.calculations.latest(self.case_id)

        def flag(ok: bool, text: str) -> str:
            return f"<span style='color:{'#5CB85C' if ok else '#E0A040'}'>{'✓' if ok else '!'} {escape(text)}</span>"

        h = [f"<h3>{escape(case.case_number)}</h3><p>{escape(case.parcel_identifier)} · "
             f"{escape(tr('case_status.' + case.status))}</p><p>"]
        h.append(flag(case.units.verified, tr("summary.units", unit=tr("unit_name." + case.units.unit.value),
                                               crs=case.units.crs_id or "—")) + "<br>")
        h.append(flag(bool(rule) and verified, (rule.label(current_language()) if rule else
                                                 (err or tr("case.rule_none"))) +
                      ("" if verified or not rule else f" ({tr('rules.unverified')})")) + "<br>")
        if rule:
            h.append("&nbsp;&nbsp;" + escape(" · ".join(f"{k}: {v:g} m" for k, v in rule.setbacks.items())) + "<br>")
        h.append(flag(parcel.verified, tr("summary.boundary_ok") if parcel.verified else tr("summary.boundary_no"))
                 + "<br>")
        counts = {s: sum(1 for x in lics if x.verification_status == s) for s in ("verified", "unverified", "disputed")}
        h.append(flag(counts["unverified"] == 0, tr("summary.licenses", **counts)) + "</p>")
        if calc:
            from .dialogs.results_dialog import summary_html

            h.append("<hr>" + summary_html(calc, ws.settings.number_format))
        else:
            h.append(f"<p style='color:#8C96A3'>{escape(tr('rpt.no_calculation'))}</p>")
        self.inspector.show_case_summary("".join(h))

    # ------------------------------------------------------------------ administration

    def backup_now(self) -> None:
        try:
            path = self.ws.backup_now("manual")
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.cmd.message(tr("backup.done", path=str(path)), "ok")

    def restore_backup(self) -> None:
        try:
            self.ws.user.require("restore_backup")
        except SlsError as exc:
            show_error(self, exc)
            return
        from .dialogs.admin_dialogs import RestoreDialog

        dlg = RestoreDialog(self.ws.backup_dir, self)
        if not dlg.exec() or dlg.selected is None:
            return
        from ..persistence.backup import restore_backup

        settings, user = self.ws.settings, self.ws.user
        db_path = self.ws.db_path
        self.ws.close()
        try:
            preserved = restore_backup(dlg.selected, db_path)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            preserved = None
        self.ws = Workspace(settings, user=user, auto_backup=False)
        self.service = CaseService(self.ws)
        self.case_id = None
        self._set_loaded(None)
        self._enable_case_actions()
        if preserved:
            QMessageBox.information(self, tr("backup.restore_title"), tr("backup.restored", path=str(preserved)))

    def open_settings(self) -> None:
        from .dialogs.admin_dialogs import SettingsDialog

        dlg = SettingsDialog(self.ws.settings, self)
        if dlg.exec():
            save_settings(dlg.apply())
            self.cmd.message(tr("settings.saved"), "ok")

    def manage_users(self) -> None:
        try:
            self.ws.user.require("manage_users")
        except SlsError as exc:
            show_error(self, exc)
            return
        from .dialogs.admin_dialogs import UsersDialog

        UsersDialog(self.ws.users, self).exec()

    # ------------------------------------------------------------------ parcel register

    def import_cadastre(self) -> None:
        from .dialogs.cadastre_dialogs import CadastreImportDialog

        CadastreImportDialog(self.ws, self.run_busy, self).exec()

    def find_parcel(self) -> None:
        if self.ws.cadastre.active_layer() is None:
            QMessageBox.information(self, tr("act.find_parcel"), tr("cad.none"))
            return
        from .dialogs.cadastre_dialogs import FindParcelDialog

        dlg = FindParcelDialog(self.ws, self)
        if dlg.exec() and dlg.selected_ids:
            self.use_register_parcels(dlg.selected_ids)

    def _pick_from_register(self, x: float, y: float) -> bool:
        """Pick-parcel tool hook: use the register when it has a parcel at the point."""
        if self.ws.cadastre.active_layer() is None or self.case_id is None:
            return False
        hits = self.ws.cadastre.at_point(x, y)
        if not hits:
            return False
        if len(hits) > 1:
            self.cmd.message(tr("cad.overlap", keys=", ".join(h.dls_key or h.label for h in hits[1:])), "warn")
        self.use_register_parcels([hits[0].id])  # smallest polygon containing the point
        return True

    def use_register_parcels(self, parcel_ids: list[int]) -> None:
        if self.canvas.drawing is None:
            self._set_loaded(self.service.new_blank_drawing(self.case_id))
        try:
            entities, boundary_ids, warnings = self.service.cadastre_entities(self.case_id, parcel_ids)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        from ..cad.commands import AddEntities
        from ..services.case_service import CADASTRE_LAYERS

        d = self.canvas.drawing
        for name, props in CADASTRE_LAYERS.items():
            d.ensure_layer(name, **props)
        # Replace earlier register geometry so the drawing never holds two candidate boundaries.
        old = [e.id for e in d.entities.values() if e.meta.get("source") == "cadastre"]
        if old:
            from ..cad.commands import DeleteEntities

            self.stack.push(DeleteEntities(old, "replace register parcels"))
        self.stack.push(AddEntities(entities, "register parcel"))
        self.canvas.edited()
        self.layers.refresh()
        main = next(e for e in entities if e.id in boundary_ids) if boundary_ids else None
        single = main if len(boundary_ids) == 1 else None
        self.save_drawing()
        try:
            self.service.link_case_to_parcel(self.case_id, parcel_ids[0], single.id if single else None,
                                             [[p[0], p[1]] for p in single.data["points"]] if single else None)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        if main is not None:
            self.canvas.selection = {main.id}
            xs = [p[0] for p in main.vertices()]
            ys = [p[1] for p in main.vertices()]
            self.canvas.cx, self.canvas.cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0) * 3
            self.canvas.scale = min(self.canvas.width(), self.canvas.height()) / span
            self.canvas.selectionChanged.emit()
            self.canvas.update()
        p = self.ws.cadastre.get(parcel_ids[0])
        self.cmd.message(tr("cad.used", key=p.dls_key or p.label, area=f"{p.area_m2 or 0:,.2f}"), "ok")
        for w in warnings:
            self.cmd.message(w, "warn")
        self._update_title()
        self._refresh_case_summary()

    def manage_fields(self) -> None:
        try:
            self.ws.user.require("manage_users")
        except SlsError as exc:
            show_error(self, exc)
            return
        from .dialogs.fields_dialog import FieldsDialog

        FieldsDialog(self.ws, self).exec()

    def regulatory_transform(self) -> None:
        regs = self.ws.drawings.for_case(self.case_id, "regulatory")
        if not regs:
            QMessageBox.information(self, tr("act.regulatory_transform"), tr("reg.none"))
            return
        from .dialogs.transform_dialog import TransformDialog

        TransformDialog(self.service, regs[-1].id, self).exec()

    def create_demo_case(self) -> None:
        """Synthetic demonstration case. Verification steps are deliberately left to the user."""
        if not self._confirm_discard():
            return
        from ..core.units import CrsKind, DrawingUnits, LengthUnit
        from ..persistence.cases import Case
        from ..rules.excel_rules import ColumnMapping
        from ..samples import SAMPLE_RULE_MAPPING, assign_sample_roles, write_sample_dxf, write_sample_rules_xlsx

        try:
            tmp = Path(tempfile.mkdtemp(prefix="sls_demo_"))
            dxf = write_sample_dxf(tmp / "synthetic_survey.dxf")
            xlsx = write_sample_rules_xlsx(tmp / "synthetic_rules.xlsx")
            version = None
            if self.ws.user.can("import_rules"):
                version = self.ws.rules.import_version(self.ws.user, "Synthetic demo rules", xlsx, "Setbacks", 2,
                                                       ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
            number = "DEMO-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            case = self.ws.cases.save(self.ws.user, Case(
                case_number=number, parcel_identifier="SYN-PLOT-123", surveyor=self.ws.user.display_name,
                notes=tr("demo.notes"), rule_version_id=version.id if version else None,
                units=DrawingUnits(LengthUnit.METER, False, self.ws.settings.default_crs, CrsKind.PROJECTED),
                land_use_code="SYN-B" if version else ""))
            drawing, report = self.service.preview_import(dxf)
            boundary = assign_sample_roles(drawing)
            self.service.import_drawing(case.id, dxf, "survey", drawing, report)
            parcel = self.ws.drawings.parcel(case.id)
            parcel.boundary_entity_id = boundary
            parcel.edge_types = ["front", "side", "rear", "side"]
            self.ws.drawings.save_parcel(self.ws.user, parcel)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.open_case_id(case.id)
        QMessageBox.information(self, tr("act.demo_case"), tr("demo.steps"))

    def show_limitations(self) -> None:
        from ..cad.dwg import dwg_status

        QMessageBox.information(self, tr("act.limitations"), tr("help.limitations") + "\n\n" +
                                dwg_status(self.ws.settings.oda_converter_path, self.ws.settings.libredwg_path))

    def show_about(self) -> None:
        from .. import __version__

        QMessageBox.about(self, tr("act.about"), tr("help.about", version=__version__, data=str(self.ws.root)))

    def closeEvent(self, ev) -> None:  # type: ignore[no-untyped-def]
        if not self._confirm_discard():
            ev.ignore()
            return
        self.ws.close()
        ev.accept()

