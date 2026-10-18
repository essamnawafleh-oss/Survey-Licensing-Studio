"""Offscreen UI tests: the real main window driven through commands and actions."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPointF, Qt  # noqa: E402

from sls.core.i18n import set_language  # noqa: E402
from sls.ui.theme import apply_theme  # noqa: E402


@pytest.fixture
def window(qtbot, full_case, qapp):
    set_language("en")
    apply_theme(qapp)
    from sls.ui.main_window import MainWindow

    win = MainWindow(full_case["ws"])  # not registered with qtbot: closed below after marking clean
    win.show()
    win.open_case_id(full_case["case"].id)
    yield win
    if win.stack:
        win.stack.mark_clean()  # avoid the modal "unsaved changes" prompt on close
    win.close()


def test_open_case_shows_drawing_and_layers(window):
    assert window.canvas.drawing is not None
    assert window.layers.table.rowCount() >= 10
    assert "SYN-2026-042" in window.windowTitle()


def test_draw_line_by_command_line_and_undo(window):
    d = window.canvas.drawing
    n = len(d.entities)
    d.active_layer = "WALLS"
    window.run_command("L")
    window.run_command("200000,1000000")
    window.run_command("@10,0")
    window.run_command("")
    assert len(d.entities) == n + 1
    new = [e for e in d.entities.values() if e.meta.get("source") == "user"][0]
    assert new.data["p2"] == pytest.approx([200010, 1000000])
    window.undo()
    assert len(d.entities) == n
    window.redo()
    assert len(d.entities) == n + 1


def test_polyline_rectangle_circle_offset_text(window):
    d = window.canvas.drawing
    d.active_layer = "BUILDINGS"
    n = len(d.entities)
    for cmd in ("PL", "0,0", "10,0", "10,10", "C"):
        window.run_command(cmd)
    for cmd in ("REC", "20,0", "@5,5"):
        window.run_command(cmd)
    for cmd in ("C", "40,0", "3"):
        window.run_command(cmd)
    for cmd in ("T", "50,0", "1.5", "نص عربي"):
        window.run_command(cmd)
    assert len(d.entities) == n + 4
    texts = [e for e in d.entities.values() if e.kind == "text" and e.data["text"] == "نص عربي"]
    assert texts


def test_move_selection_and_locked_layer(window):
    d = window.canvas.drawing
    wall = next(e for e in d.entities.values() if e.layer == "WALL")
    window.canvas.selection = {wall.id}
    before = list(wall.data["p1"])
    for cmd in ("M", "0,0", "5,0"):
        window.run_command(cmd)
    assert d.entities[wall.id].data["p1"][0] == pytest.approx(before[0] + 5)
    d.layer("WALL").locked = True
    window.canvas.selection = {wall.id}
    for cmd in ("M", "0,0", "5,0"):
        window.run_command(cmd)
    assert d.entities[wall.id].data["p1"][0] == pytest.approx(before[0] + 5)  # unchanged: locked


def test_mouse_selection_and_zoom(window, qtbot):
    c = window.canvas
    c.resize(900, 700)
    c.zoom_extents()
    parcel = next(e for e in c.drawing.entities.values() if e.layer == "PARCEL")
    x, y = parcel.data["points"][0]
    qtbot.mouseClick(c, Qt.LeftButton, pos=c.to_screen(x + 10, y).toPoint())
    assert parcel.id in c.selection or c.selection  # something picked at the parcel edge
    s0 = c.scale
    c.zoom_at(2.0, QPointF(450, 350))
    assert c.scale == pytest.approx(s0 * 2)


def test_layer_panel_operations_are_undoable(window):
    d = window.canvas.drawing
    window.layers._apply(lambda dr: dr.rename_layer("WALL", "WALL_X"), "rename")
    assert d.has_layer("WALL_X")
    window.undo()
    assert d.layer("WALL").name == "WALL"


def test_calculate_results_and_report_preview(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.calculate()
    calc = window.ws.calculations.latest(window.case_id)
    assert calc.results["status"] == "ok"
    window.apply_derived(calc.id)
    derived = [e for e in window.canvas.drawing.entities.values() if e.meta.get("source") == "derived"]
    assert derived
    from sls.ui.dialogs.report_dialog import ReportDialog

    dlg = ReportDialog(window.ws, window.case_id, window)
    qtbot.addWidget(dlg)
    dlg.lang.setCurrentIndex(1)
    dlg._preview()
    assert dlg.last is not None and dlg.last.page_count >= 6


def test_dialogs_construct(window, qtbot):
    from sls.ui.dialogs.case_dialogs import CaseBrowser, CaseDialog
    from sls.ui.dialogs.licenses_dialog import LicensesDialog
    from sls.ui.dialogs.media_dialog import MediaDialog
    from sls.ui.dialogs.rules_dialog import RulesDialog

    ws, cid = window.ws, window.case_id
    for dlg in (CaseDialog(ws, ws.cases.get(cid)), CaseBrowser(ws), RulesDialog(ws), LicensesDialog(ws, cid),
                MediaDialog(ws, cid)):
        qtbot.addWidget(dlg)
    lic = LicensesDialog(ws, cid)
    qtbot.addWidget(lic)
    assert lic.list.count() == 1


def test_admin_and_transform_dialogs_construct(window, qtbot, sample_dxf):
    from sls.ui.dialogs.admin_dialogs import LoginDialog, RestoreDialog, SettingsDialog, UsersDialog
    from sls.ui.dialogs.fields_dialog import FieldsDialog
    from sls.ui.dialogs.transform_dialog import TransformDialog

    ws = window.ws
    reg = window.service.import_drawing(window.case_id, sample_dxf, "regulatory")
    tf = TransformDialog(window.service, reg.record.id)
    for dlg in (SettingsDialog(ws.settings), UsersDialog(ws.users), LoginDialog(ws.users), RestoreDialog(ws.backup_dir),
                FieldsDialog(ws), tf):
        qtbot.addWidget(dlg)
    from PySide6.QtWidgets import QTableWidgetItem

    for c, v in enumerate(["0", "0", "1", "1"]):
        tf.table.setItem(0, c, QTableWidgetItem(v))
    tf._compute()
    assert tf.apply_btn.isEnabled() and "RMS" in tf.result.text()
