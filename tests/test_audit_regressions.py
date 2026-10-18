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


# A4-A8, A10, A12, engine version -------------------------------------------------------------------

def _calc_drawing(units=None):
    from sls.cad.model import Drawing
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit

    units = units or DrawingUnits(LengthUnit.METER, True, "synthetic-local", CrsKind.LOCAL, "tester")
    d = Drawing.with_default_layers(units)
    d.ensure_layer("PB", role="property_boundary")
    d.ensure_layer("BLD_G", role="building", floor_key="G")
    d.ensure_layer("FENCE", role="fence")
    return d, units


def _poly(d, layer, pts, closed=True):
    from sls.cad.model import Entity

    return d.add(Entity("polyline", layer, {"points": [list(p) for p in pts], "closed": closed})).id


def _run(d, units, bid, types):
    from sls.engine.inputs import extract_inputs
    from sls.engine.run import run_calculation
    from sls.rules.excel_rules import LandUseRule

    rule = LandUseRule("SYN-B", {"front": 4, "side": 3, "rear": 3}, 4, "x", "B", max_coverage_pct=80)
    inputs = extract_inputs(d, boundary_entity_id=bid, parcel_verified=True, edge_types=types, rule=rule,
                            rule_version_id="rv", rule_version_verified=True, land_use_code="SYN-B", licenses=[],
                            units=units)
    return inputs, run_calculation(inputs)


RECT20 = [(0, 0), (20, 0), (20, 30), (0, 30)]


def test_a4_building_outside_the_parcel_is_flagged():
    d, u = _calc_drawing()
    bid = _poly(d, "PB", RECT20)
    _poly(d, "BLD_G", [(30, 5), (40, 5), (40, 15), (30, 15)])  # entirely on the neighbour's land
    _, r = _run(d, u, bid, ["front", "side", "rear", "side"])
    assert any("outside the property boundary" in i["message"] for i in r["issues"])
    sb = next(c for c in r["eligibility"]["checks"] if c["key"] == "setbacks")
    assert sb["status"] == "fail" and sb["outside_parcel_m2"] == pytest.approx(100)


def test_a6_outline_with_coincident_ends_counts_as_closed():
    d, u = _calc_drawing()
    bid = _poly(d, "PB", RECT20)
    _poly(d, "BLD_G", [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5.0002)], closed=False)  # ends 0.2 mm apart
    _, r = _run(d, u, bid, ["front", "side", "rear", "side"])
    assert r["floors"][0]["area_m2"] == pytest.approx(100, abs=0.01)
    assert "closed_by_coincident_ends" in {i["code"] for i in r["issues"]}


def test_a7_dialog_and_engine_count_the_same_edges():
    from sls.cad.model import logical_boundary

    cases = {
        "repeated closing vertex": [(0, 0), (20, 0), (20, 30), (0, 30), (0, 0)],
        "near-duplicate vertex": [(0, 0), (10, 0), (10.0005, 0), (20, 0), (20, 30), (0, 30)],
        "arc on edge 2": [(0, 0), (20, 0, 0.4), (20, 30), (0, 30)],
    }
    for name, pts in cases.items():
        d, u = _calc_drawing()
        bid = _poly(d, "PB", pts)
        n = len(logical_boundary(pts, True, 0.001))  # rows the boundary dialog shows
        types = (["front", "side", "rear", "side"] * 2)[:n]
        _, r = _run(d, u, bid, types)
        assert not r["errors"], (name, r["errors"])
        assert r["setback"]["buildable_area_m2"] > 0


def test_a8_nested_outline_is_flagged_as_possible_courtyard():
    d, u = _calc_drawing()
    bid = _poly(d, "PB", [(0, 0), (60, 0), (60, 60), (0, 60)])
    _poly(d, "BLD_G", [(10, 10), (50, 10), (50, 50), (10, 50)])
    _poly(d, "BLD_G", [(20, 20), (40, 20), (40, 40), (20, 40)])
    _, r = _run(d, u, bid, ["side"] * 4)
    assert "nested_outline" in {i["code"] for i in r["issues"]}


def test_a10_circle_on_fence_layer_is_counted():
    from sls.cad.model import Entity

    d, u = _calc_drawing()
    bid = _poly(d, "PB", RECT20)
    d.add(Entity("line", "FENCE", {"p1": [0, -1], "p2": [30, -1]}))
    d.add(Entity("circle", "FENCE", {"center": [50, 50], "radius": 3.0}))
    _, r = _run(d, u, bid, ["front", "side", "rear", "side"])
    fence = next(x for x in r["lengths"] if x["kind"] == "fence")
    assert fence["length_m"] == pytest.approx(30 + 2 * math.pi * 3, abs=0.01)


def test_a12_unknown_projected_crs_is_warned():
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit

    u = DrawingUnits(LengthUnit.METER, True, "EPSG:30666", CrsKind.PROJECTED, "tester")
    d, _ = _calc_drawing(u)
    bid = _poly(d, "PB", [(353000, 355000), (353030, 355000), (353030, 355020), (353000, 355020)])
    _, r = _run(d, u, bid, ["front", "side", "rear", "side"])
    assert "crs_unknown" in {i["code"] for i in r["issues"]}


def test_a5_saving_unchanged_geometry_does_not_make_the_calculation_stale(full_case):
    from sls.services.case_service import CaseService
    from sls.services.report_service import ReportService

    ws, case, calc = full_case["ws"], full_case["case"], full_case["calc"]
    svc = CaseService(ws)
    loaded = svc.survey_drawing(case.id)
    svc.apply_derived_geometry(loaded.drawing, calc.results, calc.id)   # generated layers only
    ws.drawings.save_revision(ADMIN, loaded.record.id, loaded.drawing, "derived geometry")
    assert svc.build_inputs(case.id).drawing_revision_id != calc.inputs.drawing_revision_id
    blockers = ReportService(ws).approval_blockers(case.id, "approved")
    assert not any("changed after the latest calculation" in b for b in blockers)


def test_engine_version_change_is_explained_by_reproduce(full_case):
    ws, calc = full_case["ws"], full_case["calc"]
    with ws.db.transaction():
        ws.db.execute("UPDATE calculations SET engine_version='1.0.0' WHERE id=?", (calc.id,))
    ok, detail = ws.calculations.reproduce(calc.id)
    assert not ok and "made with engine 1.0.0" in detail  # results now carry engine 1.1.0 behaviour/version


# P5-P9, P12-P15 -- persistence, reports, UI ---------------------------------------------------------

def test_p5_failed_first_save_can_be_retried(workspace):
    from sls.persistence.cases import Case

    case = Case(case_number="RETRY-1", rule_version_id="no-such-version")  # foreign key fails
    with pytest.raises(Exception):
        workspace.cases.save(ADMIN, case)
    assert case.id == ""
    case.rule_version_id = None
    saved = workspace.cases.save(ADMIN, case)
    assert [c.case_number for c in workspace.cases.list()] == ["RETRY-1"] and saved.id


def test_p6_p7_p8_damaged_tampered_or_multipage_images_do_not_abort_the_report(full_case):
    from PIL import Image

    from sls.reporting.report import ReportOptions
    from sls.services.report_service import ReportService

    ws, case = full_case["ws"], full_case["case"]
    tmp = full_case["tmp"]
    good = tmp / "ok.png"
    Image.new("RGB", (40, 30), "red").save(good)
    broken = tmp / "broken.png"
    broken.write_bytes(good.read_bytes()[:60])            # truncated after the header
    tiff = tmp / "pages.tif"
    Image.new("RGB", (20, 20)).save(tiff, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")])
    ws.media.add_photo(ADMIN, case.id, broken, "broken")
    tampered = ws.media.add_photo(ADMIN, case.id, good, "tampered")
    ws.media.add_attachment(ADMIN, case.id, tiff, "multipage")
    Image.new("RGB", (40, 30), "green").save(ws.files.path(tampered.file_id))  # replaced after import
    res = ReportService(ws).preview(case.id, ReportOptions(language="en", generated_at="2026-10-18T09:00:00+00:00",
                                                           generated_by="tester"))
    assert res.pdf.startswith(b"%PDF")
    text = " ".join(res.warnings)
    assert "broken" in text and "checksum" in text
    docs = {d["file"]: d for d in res.manifest["documents"]}
    assert any(d["status"] == "not_included" and "pages" in d["error"] for d in docs.values())


def test_p9_brackets_in_mixed_arabic_latin_text():
    from sls.reporting.arabic import visual

    v = visual("الارتداد الأمامي (front) = 4.00 م", "R")
    assert "(front)" in v and v.index("4.00") < v.index("(front)")
    assert visual("Rule (سكن) here", "L").startswith("Rule (") and visual("ABC (def) GHI", "L") == "ABC (def) GHI"


def test_p12_cancel_does_not_attach_a_license_document(full_case, qtbot, qapp, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QInputDialog

    from sls.ui.dialogs.licenses_dialog import LicensesDialog

    ws, case = full_case["ws"], full_case["case"]
    doc = tmp_path / "lic.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")
    dlg = LicensesDialog(ws, case.id)
    qtbot.addWidget(dlg)
    dlg.list.setCurrentRow(0)
    lic = full_case["license"]
    before = len(ws.licenses.get(lic.id).documents)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(doc)], ""))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", False))  # Cancel
    dlg._attach()
    assert len(ws.licenses.get(lic.id).documents) == before


def test_p14_p15_damaged_settings_never_block_start_up(tmp_path):
    from sls.core.settings import load_settings_checked

    path = tmp_path / "settings.json"
    path.write_text('{"language": "en", "backup_ret', encoding="utf-8")          # truncated
    s, problems = load_settings_checked(path)
    assert s.language == "ar" and problems and not path.exists()
    assert list(tmp_path.glob("settings.json.bad-*"))                            # kept aside
    path.write_bytes(b"\xef\xbb\xbf" + b'{"language": "en", "backup_retention": "30", "setback_method": "x"}')
    s, problems = load_settings_checked(path)                                    # BOM, wrong type, bad value
    assert s.language == "en" and s.backup_retention == 30 and s.setback_method == "offset_corner"
    assert len(problems) == 2


# Phase 3 -- actionable import report ----------------------------------------------------------------

def test_import_report_lists_concrete_actions(tmp_path):
    from sls.cad.dxf_io import import_dxf

    def build(m):
        m.add_line((0, 0), (10, 0))
        m.add_polyline2d([(0, 0)])                                 # damaged
        h = m.add_hatch()
        h.paths.add_polyline_path([(0, 0), (1, 0), (1, 1)], is_closed=True)
        m.add_spline([(0, 0), (1, 1), (2, 0), (3, 1)])            # approximated

    import ezdxf

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 0                                    # units not declared
    build(doc.modelspace())
    doc.saveas(str(tmp_path / "a.dxf"))
    _, report = import_dxf(tmp_path / "a.dxf")
    text = " ".join(report.actions)
    assert "redraw them" in text and report.damaged_handles[0] in text
    assert "HATCH" in text and "approximated" in text and "units are not declared" in text


def test_dwg_import_report_requires_comparison_with_source(tmp_path, monkeypatch):
    from sls.cad import dwg

    src = tmp_path / "plan.dwg"
    src.write_bytes(b"AC1032 fake")
    before = src.read_bytes()

    def fake_convert(exe, path, dst):
        import ezdxf

        d = ezdxf.new()
        d.header["$INSUNITS"] = 6
        d.modelspace().add_line((0, 0), (1, 0))
        d.saveas(str(dst))
        return ["ERROR: bad object 0x1F"]

    conv = dwg.Converter("libredwg", tmp_path / "dwg2dxf")
    monkeypatch.setattr(dwg, "libredwg_version", lambda p: "test")
    monkeypatch.setattr(dwg, "choose_converter", lambda *a: conv)
    monkeypatch.setattr(dwg, "_convert_libredwg", fake_convert)
    _, report = dwg.import_dwg(src)
    assert src.read_bytes() == before                              # source untouched
    assert any("compare layers, entity counts" in a and "1 read error" in a for a in report.actions)


def test_p20_p11_report_print_scaling_and_no_missing_symbols(full_case):
    import io

    from pypdf import PdfReader

    from sls.core.i18n import catalogue
    from sls.reporting.report import ReportOptions
    from sls.services.report_service import ReportService

    res = ReportService(full_case["ws"]).preview(full_case["case"].id, ReportOptions(
        language="en", generated_at="2026-10-18T09:00:00+00:00", generated_by="tester"))
    prefs = PdfReader(io.BytesIO(res.pdf)).trailer["/Root"]["/ViewerPreferences"]
    assert prefs["/PrintScaling"] == "/None"
    for lang in ("ar", "en"):  # report strings use only characters the bundled fonts can draw
        bad = {k for k, v in catalogue(lang).items() if k.startswith("rpt.") and set(v) & set("∩→←✓⚠")}
        assert not bad, bad


def test_a13_license_duplicates_use_tolerance_case_and_per_attribute_blocking():
    from sls.licensing.reconcile import LicenseRecordInput as L
    from sls.licensing.reconcile import reconcile

    near = reconcile({"G": 300.0}, {}, [L("a", "LIC-7", 1, "verified", (("G", 180.0),)),
                                        L("b", "lic-7 ", 1, "verified", ((" G", 180.001),))])
    g = near.areas[0]
    assert g.licensed == pytest.approx(180.0) and not g.conflicts         # same license, within tolerance
    fence_only = reconcile({"G": 300.0}, {"fence": 50.0},
                           [L("a", "L-1", 1, "verified", (("G", 100.0),), (("fence", 20.0),)),
                            L("b", "L-1", 1, "verified", (("G", 100.0),), (("fence", 25.0),))])
    assert not fence_only.areas[0].conflicts and fence_only.areas[0].licensed == pytest.approx(100.0)
    assert fence_only.lengths[0].conflicts                                   # only the differing item is blocked
    neg = reconcile({"G": 300.0}, {}, [L("a", "L-2", 1, "verified", (("G", -50.0),))])
    assert neg.areas[0].conflicts and neg.areas[0].licensed == 0
    two = reconcile({"G": 300.0}, {}, [L("a", "", 1, "verified", (("G", 100.0),)),
                                       L("b", "", 1, "verified", (("G", 100.0),))])
    assert two.areas[0].licensed == pytest.approx(200.0)                     # not merged as duplicates
