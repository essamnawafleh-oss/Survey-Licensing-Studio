"""DXF import/export tests with synthetic drawings."""
from __future__ import annotations

import ezdxf
import pytest

from sls.cad.dxf_io import export_dxf, import_dxf
from sls.cad.model import Drawing, Entity, Layer, flatten_bulge_polyline
from sls.core.errors import CadImportError
from sls.core.units import DrawingUnits, LengthUnit
from sls.samples import OX, OY, write_sample_dxf


def test_import_supported_entities_and_layers(sample_dxf):
    drawing, report = import_dxf(sample_dxf)
    assert report.suggested_unit == "m"
    assert drawing.units.unit is LengthUnit.METER
    assert drawing.units.verified is False  # never auto-verified
    assert report.imported["LWPOLYLINE"] == 7
    assert report.imported["LINE"] == 2
    assert report.imported["TEXT"] == 2 and report.imported["MTEXT"] == 1
    assert report.imported["ARC"] == 1
    assert report.expanded["INSERT"] == 1
    assert report.expanded.get("CIRCLE") == 1  # block content
    for name in ("PARCEL", "BLDG_GF", "BLDG_1F", "WALL", "FENCE", "ROAD", "TEXT", "WELL"):
        assert drawing.has_layer(name)
    assert report.layers["BLDG_GF"] == 2


def test_unsupported_entities_are_reported_not_silently_dropped(sample_dxf):
    _, report = import_dxf(sample_dxf)
    assert report.skipped == {"HATCH": 1}
    assert report.approximated == {"SPLINE": 1}
    assert any("HATCH" in w for w in report.warnings)
    drawing, _ = import_dxf(sample_dxf)
    spl = [e for e in drawing.entities.values() if e.meta.get("approximated_from") == "SPLINE"]
    assert len(spl) == 1


def test_block_entities_on_layer_zero_take_insert_layer(sample_dxf):
    drawing, _ = import_dxf(sample_dxf)
    circles = [e for e in drawing.entities.values() if e.kind == "circle"]
    assert circles and all(c.layer == "WELL" and c.meta["block"] == "WELL_SYMBOL" for c in circles)


def test_coordinates_and_arabic_text_preserved(sample_dxf):
    drawing, report = import_dxf(sample_dxf)
    parcel = next(e for e in drawing.entities.values() if e.layer == "PARCEL")
    assert parcel.data["points"][0] == pytest.approx([OX, OY])
    assert parcel.data["closed"] is True
    texts = [e.data["text"] for e in drawing.entities.values() if e.kind == "text"]
    assert "قطعة تجريبية رقم ١٢٣" in texts
    assert report.extents[0] <= OX


def test_missing_insunits_gives_unknown_units(tmp_path):
    path = write_sample_dxf(tmp_path / "nounits.dxf", units_code=0)
    drawing, report = import_dxf(path)
    assert drawing.units.unit is LengthUnit.UNKNOWN
    assert any("$INSUNITS" in w for w in report.warnings)


def test_round_trip_preserves_ids_meta_layers_units(sample_dxf, tmp_path):
    drawing, _ = import_dxf(sample_dxf)
    drawing.units = DrawingUnits(LengthUnit.METER, True)
    drawing.layer("BLDG_GF").color = "#123456"
    drawing.layer("ROAD").visible = False
    drawing.layer("WALL").locked = True
    dim = drawing.add(Entity("dimension", "TEXT", {"p1": [OX, OY], "p2": [OX + 10, OY], "offset": 2.0}))
    first = next(e for e in drawing.entities.values() if e.layer == "PARCEL")
    first.meta["classification"] = "property_boundary"
    out = tmp_path / "out.dxf"
    export_dxf(drawing, out)
    again, report = import_dxf(out)
    assert again.units.unit is LengthUnit.METER
    assert again.layer("BLDG_GF").color == "#123456"
    assert again.layer("ROAD").visible is False
    assert again.layer("WALL").locked is True
    assert first.id in again.entities
    assert again.entities[first.id].meta["classification"] == "property_boundary"
    assert again.entities[first.id].data["points"][2] == pytest.approx(first.data["points"][2])
    assert again.entities[dim.id].kind == "dimension"
    assert report.restored_sls_metadata == len(drawing.entities)


def test_bulge_polyline_round_trip_and_flattening(tmp_path):
    d = Drawing.with_default_layers(DrawingUnits(LengthUnit.METER, True))
    # Semicircle bulge=1 from (0,0) to (10,0): CCW arc below the chord.
    e = d.add(Entity("polyline", "WALLS", {"points": [[0, 0, 1.0], [10, 0]], "closed": False}))
    pts = flatten_bulge_polyline(e.data["points"], False, 256)
    ys = [p[1] for p in pts]
    assert min(ys) == pytest.approx(-5, abs=1e-3)
    out = tmp_path / "bulge.dxf"
    export_dxf(d, out)
    again, _ = import_dxf(out)
    assert again.entities[e.id].data["points"][0][2] == pytest.approx(1.0)


def test_invalid_file_raises(tmp_path):
    bad = tmp_path / "bad.dxf"
    bad.write_text("not a dxf")
    with pytest.raises(CadImportError):
        import_dxf(bad)
    with pytest.raises(CadImportError):
        import_dxf(tmp_path / "missing.dxf")


def test_layer_operations():
    d = Drawing.with_default_layers()
    d.add(Entity("line", "WALLS", {"p1": [0, 0], "p2": [1, 0]}))
    with pytest.raises(ValueError):
        d.remove_layer("WALLS")
    dup = d.duplicate_layer("WALLS")
    assert len(d.entities_on([dup.name])) == 1
    d.rename_layer("WALLS", "WALLS_SURVEYED")
    assert all(e.layer != "WALLS" for e in d.entities.values())
    d.move_layer("REFERENCE", 1)
    assert d.layers[1].name == "REFERENCE"
    with pytest.raises(ValueError):
        d.add_layer(Layer("REFERENCE"))
    restored = Drawing.from_json(d.to_json())
    assert restored.content_hash() == d.content_hash()
