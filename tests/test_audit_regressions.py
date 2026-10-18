"""Regression tests for defects confirmed by the code audit (see AUDIT_REPORT.md for IDs).

Each test reproduces the original failure; synthetic data only.
"""
from __future__ import annotations

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
