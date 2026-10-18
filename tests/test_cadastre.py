"""Parcel register from Shapefiles (synthetic layer reproducing real-world data problems)."""
from __future__ import annotations

import pytest
import shapefile

from conftest import ADMIN, SURVEYOR
from sls.core.crs import JTM_EPSG, JTM_ESRI, from_wkt, same_grid
from sls.core.errors import PermissionDeniedError, ValidationError
from sls.core.units import CrsKind, DrawingUnits, LengthUnit
from sls.gis.shapefile_layer import inspect, iter_records, suggest_mapping, summarize
from sls.persistence.cases import Case
from sls.services.case_service import CaseService

JTM_PRJ = ('PROJCS["Jordan_JTM",GEOGCS["GCS_Jordan",DATUM["D_Jordan",SPHEROID["International_1924",6378388.0,297.0]],'
           'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
           'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",-3000000.0],'
           'PARAMETER["Central_Meridian",37.0],PARAMETER["Scale_Factor",0.9998],'
           'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
OX, OY = 353000.0, 355500.0


def sq(x, y, w, h):
    # clockwise exterior ring (shapefile convention)
    return [(x, y), (x, y + h), (x + w, y + h), (x + w, y), (x, y)]


@pytest.fixture
def parcel_shp(tmp_path):
    path = tmp_path / "SYN_PARCELS.shp"
    w = shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8")
    for name, size in (("VILL_CODE", 4), ("HOD_CODE", 3), ("SECTOR_COD", 3), ("SHEET_NO", 10), ("PARCEL_ID", 5),
                       ("DLS_KEY", 15), ("GLOBALID", 38)):
        w.field(name, "C", size)
    rows = [
        ("9001", "030", "000", "92", "00351", "900103000000351", sq(OX, OY, 30, 25)),        # the target parcel
        ("9001", "030", "000", "92", "00352", "900103000000352", sq(OX + 30, OY, 20, 25)),   # neighbour
        ("9001", "030", "000", "92", "00353", "900103000000353", sq(OX, OY + 25, 50, 20)),   # neighbour
        ("9001", "025", "000", "60", "00200", "900102500000200", sq(OX + 200, OY, 10, 10)),  # repeated key ...
        ("9001", "025", "000", "60", "00200", "900102500000200", sq(OX + 220, OY, 12, 12)),  # ... different polygon
        ("9001", "011", "002", "267", "", "", sq(OX + 400, OY, 10, 10)),                      # missing key/number
    ]
    for i, (v, h, s, sh, p, k, ring) in enumerate(rows):
        w.poly([ring])
        w.record(v, h, s, sh, p, k, f"{{SYN-{i:04d}}}")
    w.poly([[(OX + 600, OY), (OX + 610, OY + 10), (OX + 610, OY), (OX + 600, OY + 10), (OX + 600, OY)]])  # bow-tie
    w.record("9001", "011", "001", "235", "00097", "900101100100097", "{SYN-BAD}")
    w.poly([sq(OX + 800, OY, 10, 10), sq(OX + 820, OY, 5, 5)])  # two parts
    w.record("9001", "012", "005", "100", "00196", "900101200500196", "{SYN-MULTI}")
    w.close()
    path.with_suffix(".prj").write_text(JTM_PRJ)
    path.with_suffix(".cpg").write_text("UTF-8")
    return path


def test_prj_is_identified_and_same_grid_as_epsg3066():
    assert from_wkt(JTM_PRJ) == JTM_ESRI
    assert same_grid(JTM_ESRI, JTM_EPSG)
    assert not same_grid(JTM_ESRI, "EPSG:32636")


def test_inspect_mapping_and_quality_summary(parcel_shp):
    info = inspect(parcel_shp)
    assert info.crs_code == JTM_ESRI and info.encoding == "utf-8" and info.count == 8
    m = suggest_mapping(info.fields)
    assert m.columns == {"key": "DLS_KEY", "village": "VILL_CODE", "basin": "HOD_CODE", "sector": "SECTOR_COD",
                         "sheet": "SHEET_NO", "parcel": "PARCEL_ID", "globalid": "GLOBALID"}
    recs = list(iter_records(parcel_shp, info.encoding))
    s = summarize(recs, m)
    assert (s["invalid"], s["multipart"], s["missing_key"], s["repeated_keys"], s["repeated_key_records"]) == (1, 1, 1, 1, 2)
    bad = next(r for r in recs if not r.valid)
    assert "Self-intersection" in bad.issue


def test_incomplete_shapefile_is_rejected(tmp_path, parcel_shp):
    parcel_shp.with_suffix(".dbf").unlink()
    with pytest.raises(Exception, match="missing .dbf"):
        inspect(parcel_shp)


def test_import_and_lookups(workspace, parcel_shp):
    with pytest.raises(PermissionDeniedError):
        workspace.cadastre.import_layer(SURVEYOR, parcel_shp, suggest_mapping(inspect(parcel_shp).fields))
    layer = workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields), "Synthetic")
    assert layer.record_count == 8 and layer.crs_code == JTM_ESRI and len(layer.sources) >= 4
    assert workspace.cadastre.distinct("village") == ["9001"]
    assert workspace.cadastre.distinct("basin", village="9001") == ["011", "012", "025", "030"]
    hit = workspace.cadastre.find("9001", "030", "000", "351")
    assert len(hit) == 1 and hit[0].dls_key == "900103000000351" and hit[0].area_m2 == pytest.approx(750)
    assert workspace.cadastre.find(parcel_no="00351")[0].id == hit[0].id
    assert len(workspace.cadastre.find(key="900102500000200")) == 2  # repeated key: both returned, none chosen
    at = workspace.cadastre.at_point(OX + 10, OY + 10)
    assert [p.parcel_no for p in at] == ["00351"]
    assert workspace.cadastre.at_point(OX - 50, OY - 50) == []
    layer2 = workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields), "Synthetic")
    assert layer2.version == 2 and workspace.cadastre.active_layer().id == layer2.id


def test_parcel_to_case_boundary_and_fields(workspace, parcel_shp):
    workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields))
    svc = CaseService(workspace)
    case = workspace.cases.save(ADMIN, Case(case_number="CAD-1", units=DrawingUnits(
        LengthUnit.METER, True, JTM_EPSG, CrsKind.PROJECTED, "admin")))
    loaded = svc.new_blank_drawing(case.id)
    target = workspace.cadastre.find(key="900103000000351")[0]
    entities, boundary_ids, warnings = svc.cadastre_entities(case.id, [target.id])
    assert len(boundary_ids) == 1 and not warnings
    boundary = next(e for e in entities if e.id == boundary_ids[0])
    assert boundary.layer == "PROPERTY_BOUNDARY" and boundary.meta["dls_key"] == "900103000000351"
    assert boundary.meta["source"] == "cadastre"
    neighbours = {e.meta["parcel_number"] for e in entities if e.layer == "CADASTRE_PARCELS"}
    assert neighbours == {"00352", "00353"}
    labels = {e.data["text"] for e in entities if e.layer == "CADASTRE_LABELS"}
    assert labels == {"00352", "00353"}
    d = loaded.drawing
    for e in entities:
        d.add(e)
    workspace.drawings.save_revision(ADMIN, loaded.record.id, d)
    svc.link_case_to_parcel(case.id, target.id, boundary.id, [[p[0], p[1]] for p in boundary.data["points"]])
    case = workspace.cases.get(case.id)
    assert case.parcel_identifier == "900103000000351"
    assert case.fields["plot_number"] == "00351" and case.fields["basin"] == "030" and case.fields["sheet_no"] == "92"
    parcel = workspace.drawings.parcel(case.id)
    assert parcel.boundary_entity_id == boundary.id and parcel.verified is False


def test_multipart_and_invalid_parcels_warn_and_give_no_single_boundary(workspace, parcel_shp):
    workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields))
    svc = CaseService(workspace)
    case = workspace.cases.save(ADMIN, Case(case_number="CAD-2", units=DrawingUnits(
        LengthUnit.METER, False, JTM_ESRI, CrsKind.PROJECTED)))
    multi = workspace.cadastre.find(key="900101200500196")[0]
    _, ids, warnings = svc.cadastre_entities(case.id, [multi.id], neighbours_margin=0)
    assert len(ids) == 2 and any("2 separate parts" in w for w in warnings)
    bad = workspace.cadastre.find(key="900101100100097")[0]
    _, _, warnings = svc.cadastre_entities(case.id, [bad.id], neighbours_margin=0)
    assert any("geometry problem" in w for w in warnings)


def test_incompatible_case_crs_is_refused(workspace, parcel_shp):
    workspace.cadastre.import_layer(ADMIN, parcel_shp, suggest_mapping(inspect(parcel_shp).fields))
    case = workspace.cases.save(ADMIN, Case(case_number="CAD-3", units=DrawingUnits(
        LengthUnit.METER, False, "EPSG:32636", CrsKind.PROJECTED)))
    target = workspace.cadastre.find(key="900103000000351")[0]
    with pytest.raises(ValidationError, match="not compatible"):
        CaseService(workspace).cadastre_entities(case.id, [target.id])
