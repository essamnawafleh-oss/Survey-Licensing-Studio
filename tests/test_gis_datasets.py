"""GIS dataset refresh: staged import, comparison, activation, rollback, pinning (synthetic data only)."""
from __future__ import annotations

import shapefile
import pytest
from shapely.geometry import shape

from conftest import ADMIN, SURVEYOR
from sls.core.errors import PermissionDeniedError, ValidationError
from sls.gis.shapefile_layer import FieldMapping, ShapefileError, inspect, suggest_mapping
from sls.persistence.datasets import ADAPTERS, ImportCancelled, ImportControl

JTM_PRJ = ('PROJCS["Jordan_JTM",GEOGCS["GCS_Jordan",DATUM["D_Jordan",SPHEROID["International_1924",6378388.0,297.0]],'
           'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
           'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",-3000000.0],'
           'PARAMETER["Central_Meridian",37.0],PARAMETER["Scale_Factor",0.9998],'
           'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
UTM36_PRJ = ('PROJCS["WGS_1984_UTM_Zone_36N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
             '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
             'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",33.0],'
             'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
OX, OY = 353000.0, 355500.0
FIELDS = (("VILL_CODE", 4), ("HOD_CODE", 3), ("SECTOR_COD", 3), ("SHEET_NO", 10), ("PARCEL_ID", 5), ("DLS_KEY", 15),
          ("GLOBALID", 38))


def sq(x, y, w, h):
    return [(x, y), (x, y + h), (x + w, y + h), (x + w, y), (x, y)]


def parcel_rows(n=6):
    rows = []
    for i in range(n):
        rows.append({"VILL_CODE": "9001", "HOD_CODE": "030", "SECTOR_COD": "000", "SHEET_NO": "92",
                     "PARCEL_ID": f"{i + 1:05d}", "DLS_KEY": f"90010300000{i + 1:04d}", "GLOBALID": f"{{G-{i:04d}}}",
                     "rings": [sq(OX + 30 * i, OY, 30, 25)]})
    return rows


def write_layer(path, rows, fields=FIELDS, prj=JTM_PRJ, cpg="UTF-8", shape_type=shapefile.POLYGON):
    w = shapefile.Writer(str(path), shapeType=shape_type, encoding="utf-8")
    for name, size in fields:
        w.field(name, "C", size)
    for r in rows:
        if shape_type == shapefile.POLYLINE:
            w.line(r["rings"])
        else:
            w.poly(r["rings"])
        w.record(*[r.get(name, "") for name, _ in fields])
    w.close()
    if prj:
        path.with_suffix(".prj").write_text(prj)
    if cpg:
        path.with_suffix(".cpg").write_text(cpg)
    return path


@pytest.fixture
def v1(tmp_path):
    (tmp_path / "v1").mkdir()
    return write_layer(tmp_path / "v1" / "PARCELS.shp", parcel_rows())


def mapping_for(path):
    return suggest_mapping(inspect(path).fields)


def stage(ws, path, kind="cadastre", **kw):
    m = kw.pop("mapping", None) or ADAPTERS[kind].suggest(inspect(path).fields)
    return ws.datasets.stage(kind, ADMIN, path, m, kw.pop("name", "Parcels"), **kw)


def audit_actions(ws):
    return [r["action"] for r in ws.db.query("SELECT action FROM audit_log ORDER BY id")]


# -- staging, comparison, activation ---------------------------------------------------------

def test_staging_never_touches_the_active_version_and_activation_is_explicit(workspace, v1):
    ws = workspace
    s1 = stage(ws, v1)
    assert s1.status == "staged" and ws.datasets.active("cadastre") is None
    assert ws.cadastre.find(parcel_no="1") == []  # staged rows are not searchable until activated
    a1 = ws.datasets.activate("cadastre", ADMIN, s1.id, "first load")
    assert a1.status == "active" and a1.activated_by == "admin"
    assert {s["file"].rsplit(".", 1)[1] for s in a1.sources} == {"shp", "shx", "dbf", "prj", "cpg"}
    assert all(len(s["sha256"]) == 64 for s in a1.sources)
    assert a1.mapping.columns["key"] == "DLS_KEY" and a1.crs_code == "ESRI:102158"
    assert a1.validation["compatible_with_default"] is True
    assert "stage_dataset" in audit_actions(ws) and "activate_dataset" in audit_actions(ws)
    with pytest.raises(PermissionDeniedError):
        ws.datasets.stage("cadastre", SURVEYOR, v1, mapping_for(v1))


def test_reimporting_an_unchanged_dataset_reports_no_changes(workspace, v1):
    ws = workspace
    ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    s2 = stage(ws, v1)
    c = s2.validation["comparison"]
    assert c["identical_sources"] and (c["added"], c["removed"], c["geometry_changed"], c["attributes_changed"]) == \
        (0, 0, 0, 0) and c["unchanged"] == 6
    assert s2.version == 2 and ws.datasets.active("cadastre").version == 1


def test_valid_update_is_compared_activated_and_searchable(workspace, v1, tmp_path):
    ws = workspace
    first = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    rows = parcel_rows()
    rows[0]["rings"] = [sq(OX, OY, 30, 40)]                     # geometry changed a lot (suspicious)
    rows[1]["SHEET_NO"] = "93"                                  # attribute change
    rows[2]["DLS_KEY"] = "900103000009999"                      # identifier change (same GLOBALID)
    del rows[5]                                                 # removed
    rows.append({**parcel_rows(8)[7], "rings": [sq(OX + 600, OY, 20, 20)]})  # added
    (tmp_path / "v2").mkdir()
    v2 = write_layer(tmp_path / "v2" / "PARCELS.shp", rows)
    s2 = stage(ws, v2)
    c = s2.validation["comparison"]
    assert c["basis"] == "globalid"
    assert (c["added"], c["removed"], c["geometry_changed"], c["attributes_changed"]) == (1, 1, 1, 2)
    assert c["identifier_changes"] == [{"globalid": "{G-0002}", "old_key": "900103000000003",
                                        "new_key": "900103000009999"}]
    assert [s["id"] for s in c["suspicious_geometry_changes"]] == ["{G-0000}"]
    assert ws.cadastre.find(parcel_no="8") == []
    ws.datasets.activate("cadastre", ADMIN, s2.id, "monthly refresh")
    found = ws.cadastre.find(parcel_no="8")
    assert len(found) == 1 and found[0].layer_id == s2.id  # search uses the new active version
    assert ws.cadastre.find(key="900103000009999")[0].layer_id == s2.id
    assert ws.cadastre.at_point(OX + 605, OY + 5)[0].parcel_no == "00008"
    assert len(ws.cadastre.find(parcel_no="6", layer_id=first.id)) == 1  # old version kept and queryable
    names = {r["name"] for r in ws.db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_cad_key", "ix_cad_admin", "ix_cad_bbox"} <= names


def test_missing_sidecars_are_refused_before_anything_is_written(workspace, v1):
    ws = workspace
    v1.with_suffix(".shx").unlink()
    with pytest.raises(ShapefileError, match="missing .shx"):
        stage(ws, v1)
    assert ws.datasets.versions("cadastre") == []


def test_missing_unknown_and_changed_crs(workspace, v1, tmp_path):
    ws = workspace
    v1.with_suffix(".prj").unlink()
    with pytest.raises(ValidationError, match="no .prj"):
        stage(ws, v1)
    s = stage(ws, v1, declared_crs="EPSG:3066")  # explicit declaration, same grid as the default
    assert s.crs_code == "EPSG:3066" and s.validation["crs_source"] == "declared"
    ws.datasets.activate("cadastre", ADMIN, s.id)
    (tmp_path / "bad").mkdir()
    bad = write_layer(tmp_path / "bad" / "PARCELS.shp", parcel_rows(), prj='PROJCS["nonsense"]')
    with pytest.raises(ValidationError, match="not a recognised"):
        stage(ws, bad)
    (tmp_path / "utm").mkdir()
    utm = write_layer(tmp_path / "utm" / "PARCELS.shp", parcel_rows(), prj=UTM36_PRJ)
    with pytest.raises(ValidationError, match="differs from the .prj"):
        stage(ws, utm, declared_crs="ESRI:102158")  # a declaration never overrides a real .prj
    su = stage(ws, utm)
    assert su.validation["compatible_with_default"] is False
    assert any("not the application grid" in w for w in su.validation["warnings"])
    assert su.validation["comparison"]["crs"]["changed"] is True


def test_changed_field_names_need_explicit_mapping(workspace, v1, tmp_path):
    ws = workspace
    ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    fields = tuple(("PARCELKEY", s) if n == "DLS_KEY" else (n, s) for n, s in FIELDS)
    rows = [{("PARCELKEY" if k == "DLS_KEY" else k): v for k, v in r.items()} for r in parcel_rows()]
    (tmp_path / "v2").mkdir()
    v2 = write_layer(tmp_path / "v2" / "PARCELS.shp", rows, fields=fields)
    assert "key" not in mapping_for(v2).columns          # not guessed from an unknown name
    with pytest.raises(ValidationError, match="do not exist"):
        stage(ws, v2, mapping=mapping_for(v1))           # the old mapping no longer fits
    m = FieldMapping({**mapping_for(v2).columns, "key": "PARCELKEY"})
    s2 = stage(ws, v2, mapping=m)
    c = s2.validation["comparison"]
    assert c["mapping_changed"] and c["mapping"]["new"]["key"] == "PARCELKEY"
    assert (c["added"], c["removed"], c["geometry_changed"]) == (0, 0, 0)


def test_invalid_multipart_duplicate_and_missing_ids_are_reported_not_repaired(workspace, tmp_path):
    ws = workspace
    rows = parcel_rows(3)
    rows.append({**rows[0], "GLOBALID": "{G-DUP}", "rings": [sq(OX + 200, OY, 10, 10)]})        # duplicate DLS key
    rows.append({"VILL_CODE": "9001", "PARCEL_ID": "", "DLS_KEY": "", "GLOBALID": "{G-NOKEY}",
                 "rings": [sq(OX + 300, OY, 10, 10)]})                                           # missing ids
    rows.append({**parcel_rows(9)[8], "rings": [[(OX + 400, OY), (OX + 410, OY + 10), (OX + 410, OY),
                                                (OX + 400, OY + 10), (OX + 400, OY)]]})          # bow-tie
    rows.append({**parcel_rows(10)[9], "rings": [sq(OX + 500, OY, 10, 10), sq(OX + 520, OY, 5, 5)]})  # two parts
    (tmp_path / "q").mkdir()
    path = write_layer(tmp_path / "q" / "PARCELS.shp", rows)
    s = stage(ws, path)
    sm = s.summary
    assert s.record_count == len(rows) == sm["records"]   # nothing dropped
    assert (sm["invalid"], sm["multipart"], sm["missing_key"], sm["repeated_keys"], sm["missing_parcel"]) == \
        (1, 1, 1, 1, 1)
    assert sm["null_fields"]["key"] == 1 and sm["null_fields"]["sheet"] == 1
    stored = ws.db.query("SELECT valid, issue, wkb FROM cadastre_parcels WHERE layer_id=? ORDER BY fid", (s.id,))
    assert len(stored) == len(rows) and stored[5]["valid"] == 0 and "Self-intersection" in stored[5]["issue"]
    reader = shapefile.Reader(str(path))
    assert shape(reader.shape(5).__geo_interface__).equals_exact(  # stored geometry is the source geometry
        __import__("shapely").wkb.loads(stored[5]["wkb"]), 0)


# -- failure, cancellation, rollback, interruption ------------------------------------------------

def test_failed_import_leaves_no_staged_rows_and_no_new_files(workspace, v1, monkeypatch):
    ws = workspace
    active = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1, name="Base").id)
    files_before = sorted(p.name for p in (ws.store.root / "shared" / "cadastre").iterdir())
    ad = ADAPTERS["cadastre"]
    real = ad.row

    def boom(lid, r, m):
        if r.fid == 3:
            raise RuntimeError("disk failure while writing")
        return real(lid, r, m)

    monkeypatch.setattr(ad, "row", boom)
    v1.with_suffix(".cpg").write_text("utf-8")  # different content: a new copy would be created
    with pytest.raises(RuntimeError, match="disk failure"):
        stage(ws, v1, name="Other")
    assert [v.id for v in ws.datasets.versions("cadastre")] == [active.id]
    assert ws.db.query_one("SELECT COUNT(*) FROM cadastre_parcels WHERE layer_id<>?", (active.id,))[0] == 0
    assert sorted(p.name for p in (ws.store.root / "shared" / "cadastre").iterdir()) == files_before
    assert ws.datasets.active("cadastre").id == active.id
    assert audit_actions(ws)[-1] == "stage_dataset_failed"


def test_cancellation_keeps_the_active_version(workspace, v1):
    ws = workspace
    active = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    seen = []
    control = ImportControl(progress=lambda st, n, t: seen.append(st), cancelled=lambda: "validate" in seen)
    with pytest.raises(ImportCancelled):
        ws.datasets.stage("cadastre", ADMIN, v1, mapping_for(v1), "Parcels", control=control)
    assert [v.id for v in ws.datasets.versions("cadastre")] == [active.id]
    assert ws.datasets.active("cadastre").id == active.id


def test_rollback_reactivates_the_previous_version(workspace, v1, tmp_path):
    ws = workspace
    a = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    rows = parcel_rows(7)
    (tmp_path / "v2").mkdir()
    b = ws.datasets.activate("cadastre", ADMIN, stage(ws, write_layer(tmp_path / "v2" / "P.shp", rows)).id)
    assert ws.datasets.version("cadastre", a.id).status == "inactive"
    r = ws.datasets.rollback("cadastre", ADMIN, "wrong file delivered")
    assert r.id == a.id and ws.datasets.active("cadastre").id == a.id
    assert ws.datasets.version("cadastre", b.id).status == "inactive"
    assert len(ws.cadastre.find(parcel_no="7")) == 0 and len(ws.cadastre.find(parcel_no="7", layer_id=b.id)) == 1
    assert "rollback_dataset" in audit_actions(ws)
    with pytest.raises(ValidationError):
        ws.datasets.discard("cadastre", ADMIN, b.id)  # activated versions are kept


def test_interrupted_activation_keeps_the_old_version_active(workspace, v1, tmp_path, monkeypatch):
    ws = workspace
    a = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    (tmp_path / "v2").mkdir()
    s2 = stage(ws, write_layer(tmp_path / "v2" / "P.shp", parcel_rows(7)))

    def fail(*args, **kwargs):
        raise RuntimeError("database failure during activation")

    monkeypatch.setattr(ws.audit, "record", fail)
    with pytest.raises(RuntimeError):
        ws.datasets.activate("cadastre", ADMIN, s2.id)
    monkeypatch.undo()
    assert ws.datasets.active("cadastre").id == a.id
    assert ws.datasets.version("cadastre", s2.id).status == "staged"


def test_discarded_staged_version_keeps_its_record(workspace, v1):
    ws = workspace
    s = stage(ws, v1)
    ws.datasets.discard("cadastre", ADMIN, s.id, "test")
    v = ws.datasets.version("cadastre", s.id)
    assert v.status == "discarded" and v.sources
    assert ws.db.query_one("SELECT COUNT(*) FROM cadastre_parcels WHERE layer_id=?", (s.id,))[0] == 0
    with pytest.raises(ValidationError):
        ws.datasets.activate("cadastre", ADMIN, s.id)


# -- pinning ------------------------------------------------------------------------------------

def _zone_layer(path, code):
    return write_layer(path, [{"ZONE_ID": code, "NAME": "zone", "rings": [sq(OX - 1000, OY - 1000, 3000, 3000)]}],
                       fields=(("ZONE_ID", 10), ("NAME", 20)))


def test_historical_calculations_stay_pinned_after_a_refresh(full_case, tmp_path):
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit
    from sls.services.case_service import CaseService

    ws, case = full_case["ws"], full_case["case"]
    case.units = DrawingUnits(LengthUnit.METER, True, "ESRI:102158", CrsKind.PROJECTED, "admin")
    ws.cases.save(ADMIN, case)
    (tmp_path / "z1").mkdir()
    (tmp_path / "z2").mkdir()
    z1 = ws.datasets.activate("zoning", ADMIN, stage(ws, _zone_layer(tmp_path / "z1" / "Z.shp", "SYN-A"),
                                                     kind="zoning", name="Zones").id)
    svc = CaseService(ws)
    old = svc.run_calculation(case.id)
    assert old.results["datasets"]["zoning"]["layer_id"] == z1.id
    assert old.results["datasets"]["rules"]["rule_version_id"] == case.rule_version_id
    n_calcs = len(ws.calculations.list(case.id))
    z2 = ws.datasets.activate("zoning", ADMIN, stage(ws, _zone_layer(tmp_path / "z2" / "Z.shp", "SYN-B"),
                                                     kind="zoning", name="Zones").id)
    assert len(ws.calculations.list(case.id)) == n_calcs   # nothing recalculated automatically
    stored = ws.calculations.get(old.id)
    assert stored.results["zoning"]["zones"][0]["zone_id"] == "SYN-A"
    assert ws.calculations.reproduce(old.id)[0]
    assert svc.build_inputs(case.id).input_hash() != old.input_hash  # shown as out of date, not changed
    new = svc.run_calculation(case.id)
    assert new.results["datasets"]["zoning"]["layer_id"] == z2.id
    assert new.results["zoning"]["zones"][0]["zone_id"] == "SYN-B"


def test_case_boundary_from_register_records_the_parcel_layer(workspace, v1):
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit
    from sls.persistence.cases import Case
    from sls.services.case_service import CaseService

    ws = workspace
    layer = ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    svc = CaseService(ws)
    case = ws.cases.save(ADMIN, Case(case_number="PIN-1", units=DrawingUnits(LengthUnit.METER, True, "ESRI:102158",
                                                                               CrsKind.PROJECTED, "admin")))
    loaded = svc.new_blank_drawing(case.id)
    target = ws.cadastre.find(parcel_no="2")[0]
    entities, ids, _ = svc.cadastre_entities(case.id, [target.id])
    for e in entities:
        loaded.drawing.add(e)
    ws.drawings.save_revision(ADMIN, loaded.record.id, loaded.drawing)
    b = next(e for e in entities if e.id == ids[0])
    svc.link_case_to_parcel(case.id, target.id, b.id, b.data["points"])
    pins = svc.build_inputs(case.id).datasets
    assert pins["cadastre"]["layer_id"] == layer.id and pins["cadastre"]["globalid"] == "{G-0001}"


# -- roads --------------------------------------------------------------------------------------

ROAD_FIELDS = (("ROAD_ID", 10), ("NAME", 40), ("CLASS", 20), ("WIDTH", 6), ("NOTE", 30))


def road_rows():
    return [
        {"ROAD_ID": "R1", "NAME": "شارع الملك حسين", "CLASS": "main", "WIDTH": "20", "NOTE": "مرجع",
         "rings": [[(OX - 50, OY - 10), (OX + 300, OY - 10)]]},
        {"ROAD_ID": "R2", "NAME": "Side street", "CLASS": "local", "WIDTH": "8", "NOTE": "",
         "rings": [[(OX - 20, OY - 10), (OX - 20, OY + 100), (OX - 15, OY + 150)]]},
        {"ROAD_ID": "R3", "NAME": "", "CLASS": "local", "WIDTH": "", "NOTE": "two parts",
         "rings": [[(OX + 1000, OY), (OX + 1100, OY)], [(OX + 1200, OY), (OX + 1300, OY)]]},
    ]


def test_road_layer_preserves_attributes_and_geometry(workspace, tmp_path):
    ws = workspace
    (tmp_path / "r").mkdir()
    path = write_layer(tmp_path / "r" / "ROADS.shp", road_rows(), fields=ROAD_FIELDS, shape_type=shapefile.POLYLINE)
    s = stage(ws, path, kind="roads", name="Roads")
    assert s.mapping.columns == {"road_id": "ROAD_ID", "name": "NAME", "road_class": "CLASS", "width": "WIDTH"}
    assert s.summary["multipart"] == 1 and s.summary["missing_name"] == 1 and s.summary["invalid"] == 0
    assert ws.roads.in_window(OX - 100, OY - 100, OX + 400, OY + 200) == []  # staged: not active yet
    ws.datasets.activate("roads", ADMIN, s.id)
    feats = sorted(ws.roads.in_window(OX - 1e4, OY - 1e4, OX + 1e4, OY + 1e4), key=lambda f: f.fid)
    reader = shapefile.Reader(str(path), encoding="utf-8")
    for f, sr in zip(feats, reader.iterShapeRecords()):
        assert f.geometry.equals_exact(shape(sr.shape.__geo_interface__), 0)
        assert f.attrs == {k: str(v) for k, v in sr.record.as_dict().items()}
    assert feats[0].name == "شارع الملك حسين" and feats[0].width == "20" and feats[0].attrs["NOTE"] == "مرجع"
    from shapely.geometry import Point

    near = ws.roads.near(Point(OX + 10, OY + 5), 20)
    assert [f.road_id for f, _ in near] == ["R1"] and near[0][1] == pytest.approx(15)
    assert [f.road_id for f in ws.roads.find("Side")] == ["R2"]


def test_roads_insert_as_reference_entities(full_case, tmp_path):
    from sls.core.units import CrsKind, DrawingUnits, LengthUnit
    from sls.services.case_service import CaseService

    ws, case = full_case["ws"], full_case["case"]
    svc = CaseService(ws)
    (tmp_path / "r").mkdir()
    path = write_layer(tmp_path / "r" / "ROADS.shp", road_rows(), fields=ROAD_FIELDS, shape_type=shapefile.POLYLINE)
    ws.datasets.activate("roads", ADMIN, stage(ws, path, kind="roads", name="Roads").id)
    with pytest.raises(ValidationError, match="not compatible"):
        svc.road_entities(case.id, OX - 100, OY - 100, OX + 400, OY + 200)  # case is in a local grid
    case.units = DrawingUnits(LengthUnit.METER, True, "ESRI:102158", CrsKind.PROJECTED, "admin")
    ws.cases.save(ADMIN, case)
    ents = svc.road_entities(case.id, OX - 100, OY - 100, OX + 400, OY + 200)
    assert {e.meta["road_id"] for e in ents} == {"R1", "R2"} and all(e.layer == "GIS_ROADS" for e in ents)
    assert all(e.meta["source"] == "gis_roads" and not e.data["closed"] for e in ents)


def test_point_layer_is_refused_for_roads(workspace, tmp_path):
    (tmp_path / "p").mkdir()
    w = shapefile.Writer(str(tmp_path / "p" / "PTS.shp"), shapeType=shapefile.POINT)
    w.field("ID", "C", 5)
    w.point(OX, OY)
    w.record("1")
    w.close()
    (tmp_path / "p" / "PTS.prj").write_text(JTM_PRJ)
    with pytest.raises(ValidationError, match="expected"):
        stage(workspace, tmp_path / "p" / "PTS.shp", kind="roads", mapping=FieldMapping({}))


# -- rules ----------------------------------------------------------------------------------------

def test_excel_rule_update_keeps_prior_versions(workspace, tmp_path):
    from sls.rules.excel_rules import ColumnMapping
    from sls.samples import SAMPLE_RULE_MAPPING, SAMPLE_RULE_ROWS, write_sample_rules_xlsx

    ws = workspace
    v1 = ws.rules.import_version(ADMIN, "Rules", write_sample_rules_xlsx(tmp_path / "r1.xlsx"), "Setbacks", 2,
                                 ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    before = [r.to_dict() for r in ws.rules.rules(v1.id)]
    rows = [list(r) for r in SAMPLE_RULE_ROWS]
    rows[1][3] = 5                                  # SYN-B front setback 4 -> 5
    rows[2][4] = "حسب المخطط"                       # conditional text is kept as text, never guessed
    rows.append(["SYN-D", "جديد", "New", 2, 2, 2, 0.5, 2, 1.0, ""])
    v2 = ws.rules.import_version(ADMIN, "Rules", write_sample_rules_xlsx(tmp_path / "r2.xlsx", rows), "Setbacks", 2,
                                 ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert v2.version == v1.version + 1 and not v2.verified
    assert [r.to_dict() for r in ws.rules.rules(v1.id)] == before   # earlier version unchanged
    c = ws.rules.compare_versions(v1.id, v2.id)
    assert c["added"] == ["SYN-D"] and c["removed"] == []
    changed = {x["code"]: x["changes"] for x in c["changed"]}
    assert changed["SYN-B"]["setbacks"]["new"]["front"] == 5
    assert ws.rules.rule(v2.id, "SYN-C").status == "conditional"
    assert "side" not in ws.rules.rule(v2.id, "SYN-C").setbacks


# -- UI -------------------------------------------------------------------------------------------

def test_gis_data_dialog_and_import_dialog(qtbot, qapp, workspace, v1, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from sls.ui.dialogs.gis_data_dialogs import DatasetImportDialog, GisDataDialog

    ws = workspace
    ws.datasets.activate("cadastre", ADMIN, stage(ws, v1).id)
    dlg = GisDataDialog(ws, lambda _m, fn: fn())
    qtbot.addWidget(dlg)
    assert dlg.tables["cadastre"].rowCount() == 1 and "Parcels v1" in dlg.active_labels["cadastre"].text()
    (tmp_path / "r").mkdir()
    path = write_layer(tmp_path / "r" / "ROADS.shp", road_rows(), fields=ROAD_FIELDS, shape_type=shapefile.POLYLINE)
    imp = DatasetImportDialog(ws, lambda _m, fn: fn(), kind="roads")
    qtbot.addWidget(imp)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    imp._browse()
    imp._check()
    assert imp.import_btn.isEnabled() and "3" in imp.summary.text()
    reviewed = []
    imp.review = lambda v: reviewed.append(v)
    imp._import()
    assert reviewed[0].status == "staged" and ws.datasets.active("roads") is None
    from sls.ui.dialogs.gis_data_dialogs import ComparisonDialog

    cmp_ = ComparisonDialog(ws, "roads", reviewed[0].id)
    qtbot.addWidget(cmp_)
    assert not cmp_.activate_btn.isEnabled()
    cmp_.confirm.setChecked(True)
    cmp_._activate()
    assert ws.datasets.active("roads").id == reviewed[0].id
    dlg.refresh()
    assert dlg.tables["roads"].rowCount() == 1
