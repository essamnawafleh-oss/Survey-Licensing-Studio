"""Regression tests for defects confirmed by the code audit (see AUDIT_REPORT.md for IDs).

Each test reproduces the original failure; synthetic data only.
"""
from __future__ import annotations

import math
import threading

import pytest

from conftest import ADMIN

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def window(qtbot, full_case, qapp):
    from sls.core.i18n import set_language
    from sls.ui.main_window import MainWindow

    set_language("en")
    win = MainWindow(full_case["ws"])
    win.show()
    win.open_case_id(full_case["case"].id)
    yield win
    if win.stack:
        win.stack.mark_clean()
    win.close()


# P1 -- background work used the SQLite connection from another thread ----------------------------

def test_p1_database_usable_from_worker_thread(workspace):
    out = {}

    def work():
        try:
            with workspace.db.transaction():
                workspace.audit.record(ADMIN, "thread_test", "test")
            out["n"] = workspace.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action='thread_test'")[0]
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert "error" not in out, out.get("error")
    assert out["n"] == 1


def test_p1_drawing_import_through_run_busy(window, sample_dxf):
    case_id = window.case_id
    n = len(window.ws.drawings.for_case(case_id, "survey"))
    window.run_busy("import", lambda: window.service.import_drawing(case_id, sample_dxf))
    assert len(window.ws.drawings.for_case(case_id, "survey")) == n + 1


def test_p1_dataset_staging_through_run_task(window, tmp_path):
    from test_gis_datasets import parcel_rows, write_layer

    from sls.gis.shapefile_layer import inspect, suggest_mapping

    path = write_layer(tmp_path / "P.shp", parcel_rows())
    ws = window.ws
    v = window.run_task("stage", lambda control: ws.datasets.stage("cadastre", ADMIN, path,
                                                                   suggest_mapping(inspect(path).fields),
                                                                   "Parcels", control=control))
    assert v.status == "staged" and v.record_count == 6


# A1-A3, A9 -- offset method overlay robustness ---------------------------------------------------

def _offset(pts, types, d):
    from shapely.geometry import Polygon

    from sls.geometry.setbacks import compute_setbacks_offset

    return compute_setbacks_offset(Polygon(pts), types, d)


def test_a1_convex_parcel_no_false_outside_parcel_error():
    from sls.geometry.primitives import MetricFrame, build_polygon
    from sls.geometry.setbacks import compute_setbacks_offset

    pts = [(353016.2047432774, 355001.2051278928), (352978.96490969957, 355006.7108264842),
           (352984.19670122943, 354972.39220109663), (353025.07701983163, 354996.6173482978)]
    frame = MetricFrame.for_points(pts, 1.0)
    parcel, _ = build_polygon(frame.to_local(pts), True)
    res = compute_setbacks_offset(parcel, ["side", "front", "side", "front"], {"front": 4.0, "side": 0.0})
    assert res.construction.valid, [i.message for i in res.construction.issues if i.severity == "error"]
    assert res.buildable_area == pytest.approx(634.4005, abs=1e-3)


def test_a2_result_does_not_depend_on_start_vertex():
    pts = [(27.84490575556904, 2.3374075732360073), (-18.54706402339504, 10.019443140945924),
           (13.370961205606601, -9.03103007086633)]
    types = ["rear", "rear", "zero"]
    d = {"rear": 2.5, "zero": 0.0}
    areas = {round(_offset(pts[k:] + pts[:k], types[k:] + types[:k], d).buildable_area, 6) for k in range(3)}
    assert len(areas) == 1 and areas.pop() == pytest.approx(143.4993, abs=1e-3)


def test_a3_setback_zone_is_valid_and_conserves_area():
    from shapely.geometry import Polygon

    pts = [(7.221182461577457, 31.938297218019642), (3.108403590790077, 14.288984295917217),
           (-12.224787471684225, 12.6027119984211), (-8.951285145422691, 8.210689612797164),
           (-8.180022200064931, -0.9847269975443529), (7.778165254360861, -16.250615993442995)]
    r = _offset(pts, ["front", "side", "front", "rear", "rear", "front"], {"front": 4.0, "side": 3.0, "rear": 2.5})
    assert r.setback_zone.is_valid
    assert r.buildable_area + r.zone_area == pytest.approx(Polygon(pts).area, abs=1e-4)
    footprint = r.buildable.representative_point().buffer(1.0)
    assert footprint.intersection(r.setback_zone).area == pytest.approx(0, abs=1e-6)


def test_a9_noise_parts_are_reported_not_counted_as_polygons():
    pts = [(0.43649044066772485, 17.80828652802995), (-1.4879662349506013, 25.77198999058813),
           (-6.007002513103506, 16.98620756118607), (-17.085430304125673, -2.1071251465686154),
           (-23.3681792333621, -10.517547845621584), (-18.0692931249739, -12.255983549576117),
           (-2.2359831772343997, -27.295337720682582), (5.8903297511791095, -25.59493796541897),
           (14.282322808200975, -3.434565754424282)]
    r = _offset(pts, ["side", "front", "side", "front", "side", "side", "zero", "zero", "rear"],
                {"front": 4.0, "zero": 0.0, "side": 3.0, "rear": 2.5})
    assert r.status == "ok" and len(r.part_areas) == 1
    assert "multiple_polygons" not in {i.code for i in r.construction.issues}


# P2-P4, P18, P19 -- backup / restore ------------------------------------------------------------

def _ws(tmp_path, name="d"):
    from sls.core.settings import Settings
    from sls.persistence.workspace import Workspace

    return Workspace(Settings(data_dir=str(tmp_path / name)), user=ADMIN, auto_backup=False)


def test_p2_restore_keeps_committed_wal_data_in_preserved_copy(tmp_path):
    import sqlite3
    import subprocess
    import sys
    import textwrap

    from sls.persistence.backup import restore_backup

    ws = _ws(tmp_path)
    base = ws.backup_now("base")
    db = ws.db_path
    ws.close()
    src = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    child = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {src!r})
        from sls.core.settings import Settings
        from sls.persistence.cases import Case
        from sls.persistence.users import User
        from sls.persistence.workspace import Workspace
        admin = User("u-admin", "admin", "Admin", "admin")
        ws = Workspace(Settings(data_dir={str(tmp_path / 'd')!r}), user=admin, auto_backup=False)
        ws.cases.save(admin, Case(case_number="COMMITTED-BEFORE-CRASH"))
        os._exit(1)  # crash: the connection is never closed, the commit is only in the -wal file
    """)
    subprocess.run([sys.executable, "-c", child], check=False)
    assert db.with_name(db.name + "-wal").exists()
    preserved = restore_backup(base, db)
    conn = sqlite3.connect(preserved)
    try:
        assert [r[0] for r in conn.execute("SELECT case_number FROM cases")] == ["COMMITTED-BEFORE-CRASH"]
    finally:
        conn.close()


def test_p3_preserved_copies_are_never_overwritten(tmp_path):
    from sls.persistence.backup import restore_backup

    ws = _ws(tmp_path)
    b = ws.backup_now("base")
    db = ws.db_path
    ws.close()
    first = restore_backup(b, db)
    second = restore_backup(b, db)
    assert first != second and first.exists() and second.exists()


def test_p4_newer_schema_and_foreign_files_are_refused(tmp_path):
    import sqlite3

    from sls.core.errors import StorageError
    from sls.persistence.backup import restore_backup

    ws = _ws(tmp_path, "future")
    ws.db.execute("INSERT INTO schema_migrations VALUES (99,'future','x')")
    newer = ws.backup_now("future")
    ws.close()
    target = _ws(tmp_path, "target")
    db = target.db_path
    target.close()
    with pytest.raises(StorageError, match="newer version"):
        restore_backup(newer, db)
    junk = tmp_path / "junk.sqlite3"
    c = sqlite3.connect(junk)
    c.execute("CREATE TABLE foo(x)")
    c.commit()
    c.close()
    with pytest.raises(StorageError, match="not a Survey"):
        restore_backup(junk, db)
    # opening a newer database directly is refused too, without changing it
    with pytest.raises(StorageError, match="newer version"):
        _ws(tmp_path, "future")


def test_p18_p19_backups_leave_no_sidecars_and_prune_keeps_safety_backups(tmp_path):
    from sls.persistence.backup import create_backup, list_backups, prune_backups

    ws = _ws(tmp_path)
    pre = create_backup(ws.db, ws.backup_dir, "pre-migration-v5")
    dailies = [create_backup(ws.db, ws.backup_dir, "daily") for _ in range(3)]
    assert not [p for p in ws.backup_dir.iterdir() if p.name.endswith(("-wal", "-shm"))]
    prune_backups(ws.backup_dir, keep=1)
    left = list_backups(ws.backup_dir)
    assert pre in left and dailies[-1] in left and dailies[0] not in left
    ws.close()


# C1-C15 -- CAD editing and DXF/DWG -----------------------------------------------------------------

def test_c1_copied_entities_with_the_same_stored_id_are_all_kept(tmp_path):
    import ezdxf

    from sls.cad.dxf_io import export_dxf, import_dxf
    from sls.cad.model import Drawing, Entity

    d = Drawing.with_default_layers(None) if hasattr(Drawing, "with_default_layers") else Drawing()
    d.add(Entity("line", "0", {"p1": [0, 0], "p2": [10, 0]}))
    export_dxf(d, tmp_path / "a.dxf")
    doc = ezdxf.readfile(str(tmp_path / "a.dxf"))
    line = doc.modelspace().query("LINE")[0]
    copy = line.copy()                      # AutoCAD COPY keeps the XDATA (same stored id)
    copy.translate(0, 5, 0)
    doc.modelspace().add_entity(copy)
    doc.saveas(str(tmp_path / "b.dxf"))
    drawing, report = import_dxf(tmp_path / "b.dxf")
    lines = [e for e in drawing.entities.values() if e.kind == "line"]
    assert len(lines) == 2 and report.duplicate_ids == 1
    assert any("duplicate_of" in e.meta for e in lines)


def test_c2_dirty_after_undo_then_new_edit():
    from sls.cad.commands import AddEntities, UndoStack
    from sls.cad.model import Drawing, Entity

    d = Drawing()
    st = UndoStack(d)
    st.push(AddEntities([Entity("line", "0", {"p1": [0, 0], "p2": [1, 0]})]))
    st.push(AddEntities([Entity("line", "0", {"p1": [0, 1], "p2": [1, 1]})]))
    st.mark_clean()
    st.undo()
    st.push(AddEntities([Entity("line", "0", {"p1": [0, 2], "p2": [1, 2]})]))
    assert st.dirty


def _dxf(tmp_path, build, name="t.dxf"):
    import ezdxf

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    build(doc.modelspace())
    doc.saveas(str(tmp_path / name))
    return tmp_path / name


def test_c3_truncated_dxf_is_reported_not_imported_empty(tmp_path):
    from sls.cad.dxf_io import import_dxf
    from sls.core.errors import CadImportError

    full = _dxf(tmp_path, lambda m: [m.add_line((i, 0), (i, 1)) for i in range(50)])
    data = full.read_bytes()
    for frac in (0.1, 0.5):
        part = tmp_path / f"trunc_{frac}.dxf"
        part.write_bytes(data[: int(len(data) * frac)])
        with pytest.raises(CadImportError, match="damaged|not a valid"):
            import_dxf(part)


def test_c4_c10_text_alignment_and_multiline_round_trip(tmp_path):
    from ezdxf.enums import TextEntityAlignment

    from sls.cad.dxf_io import export_dxf, import_dxf

    def build(m):
        m.add_text("ALIGNED", height=1).set_placement((300, 300), (320, 300), align=TextEntityAlignment.ALIGNED)
        m.add_text("MID", height=1).set_placement((200, 200), align=TextEntityAlignment.MIDDLE_CENTER)
        m.add_mtext("line1\\Pline2", dxfattribs={"insert": (100, 100), "char_height": 1, "attachment_point": 5})

    d, _ = import_dxf(_dxf(tmp_path, build))
    texts = {e.data["text"]: e.data for e in d.entities.values() if e.kind == "text"}
    assert texts["ALIGNED"]["position"] == [300.0, 300.0] and texts["ALIGNED"]["align_point"] == [320.0, 300.0]
    assert texts["MID"]["position"] == [200.0, 200.0] and texts["MID"]["halign"] == 1
    export_dxf(d, tmp_path / "out.dxf")
    d2, _ = import_dxf(tmp_path / "out.dxf")
    t2 = {e.data["text"]: e.data for e in d2.entities.values() if e.kind == "text"}
    assert t2["ALIGNED"]["align_point"] == [320.0, 300.0] and t2["MID"]["halign"] == 1
    assert t2["line1\nline2"]["attachment_point"] == 5


def test_c5_polyline_with_too_few_vertices_is_damaged(tmp_path):
    from sls.cad.dxf_io import import_dxf

    def build(m):
        m.add_polyline2d([(0, 0)])
        m.add_line((0, 0), (1, 1))

    d, report = import_dxf(_dxf(tmp_path, build))
    assert not [e for e in d.entities.values() if e.kind == "polyline"]
    assert any("damaged" in k for k in report.skipped) and report.damaged_handles


def test_c6_bulge_arc_bbox_covers_the_arc():
    from sls.cad import ops
    from sls.cad.model import Entity

    b = math.tan(math.radians(350) / 4)
    e = Entity("polyline", "0", {"points": [[0, 0, b], [2, 0]], "closed": False})
    x0, y0, x1, y1 = ops.fast_bbox(e)
    assert y0 < -20 and x0 < -10 and x1 > 12


def test_c7_c9_layer_case_and_illegal_names():
    from sls.cad.model import Drawing, Entity, Layer

    d = Drawing()
    d.add_layer(Layer("Property_Boundary"))
    e = d.add(Entity("line", "PROPERTY_BOUNDARY", {"p1": [0, 0], "p2": [1, 0]}))
    assert e.layer == "Property_Boundary"
    d.rename_layer("property_boundary", "PB")
    assert e.layer == "PB"
    for bad in ("WALL<1>", "A/B", "ROAD:EDGE", "طبقة*"):
        with pytest.raises(ValueError, match="not allowed"):
            d.rename_layer("PB", bad)


def test_c8_c13_offset_and_extend_refuse_invalid_input():
    from sls.cad import ops
    from sls.cad.model import Entity

    bow = Entity("polyline", "0", {"points": [[0, 0], [10, 10], [10, 0], [0, 10]], "closed": True})
    with pytest.raises(ValueError, match="not a valid polygon"):
        ops.offset(bow, 1, (2, 5))
    zero = Entity("line", "0", {"p1": [1, 1], "p2": [1, 1]})
    with pytest.raises(ValueError, match="zero length"):
        ops.offset(zero, 1, (0, 0))
    with pytest.raises(ValueError, match="zero length"):
        ops.extend_line(zero, (1, 1), [])


def test_c12_non_finite_coordinates_are_not_points():
    from sls.ui.tools import parse_point

    assert parse_point("nan,5", None) is None
    assert parse_point("@1e999<0", (0, 0)) is None
    assert parse_point("3,4", None) == (3.0, 4.0)


def test_c14_c15_dwg_converter_failures_are_actionable(tmp_path, monkeypatch):
    from sls.cad import dwg
    from sls.cad.model import Drawing

    junk = tmp_path / "notes.txt"
    junk.write_text("x")
    assert dwg.find_libredwg(str(junk)) is None          # not a converter
    fake = tmp_path / "dwg2dxf"
    fake.write_text("#!/bin/sh\nexit 0\n")              # not executable
    assert dwg.find_libredwg(str(fake)) is None
    oda = tmp_path / "ODAFileConverter"
    oda.write_text("")
    monkeypatch.setattr(dwg, "find_oda", lambda configured="": oda)
    monkeypatch.setattr(dwg, "_convert_oda", lambda *a, **k: [])  # "succeeds" but writes nothing
    with pytest.raises(dwg.DwgUnavailableError, match="wrote no DWG"):
        dwg.export_dwg(Drawing(), tmp_path / "out.dwg", str(oda))
