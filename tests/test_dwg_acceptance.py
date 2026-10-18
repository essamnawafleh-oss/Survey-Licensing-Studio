"""DWG acceptance tests.

Prerequisites: a converter (ODA File Converter, or LibreDWG ``dwg2dxf`` given by
SLS_LIBREDWG) and real DWG files in tests/fixtures/dwg/ or SLS_DWG_FIXTURES. Official
drawings must never be committed; point SLS_DWG_FIXTURES at a local folder instead. DXF
tests never count as DWG evidence. Without both prerequisites the tests skip with a reason.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sls.cad.dwg import choose_converter, dwg_status, export_dwg, find_oda, import_dwg
from sls.core.errors import DwgUnavailableError

ODA = os.environ.get("SLS_ODA_CONVERTER", "")
LIBREDWG = os.environ.get("SLS_LIBREDWG", "")
CONVERTER = choose_converter(ODA, LIBREDWG)
FIXTURE_DIR = Path(os.environ.get("SLS_DWG_FIXTURES", Path(__file__).parent / "fixtures" / "dwg"))
DWG_FILES = sorted(FIXTURE_DIR.glob("*.dwg")) if FIXTURE_DIR.is_dir() else []

needs_dwg = pytest.mark.skipif(CONVERTER is None or not DWG_FILES,
                               reason="DWG acceptance needs a DWG converter and real DWG fixtures")


def test_missing_converter_is_reported_explicitly(tmp_path):
    if choose_converter("", "") is not None:
        pytest.skip("a converter is installed on this machine")
    with pytest.raises(DwgUnavailableError):
        import_dwg(tmp_path / "x.dwg")
    assert "unavailable" in dwg_status()


@needs_dwg
@pytest.mark.parametrize("path", DWG_FILES, ids=lambda p: p.name)
def test_dwg_import_has_entities_layers_and_full_accounting(path):
    drawing, report = import_dwg(path, ODA, LIBREDWG)
    assert report.converter
    assert report.total_imported > 0 and len(drawing.layers) > 1
    assert drawing.extents() is not None
    # Every damaged entity is accounted for in the report, never silently dropped.
    damaged = sum(v for k, v in report.skipped.items() if "damaged" in k)
    assert damaged == len(report.damaged_handles)
    if any(m.upper().startswith("ERROR") for m in report.converter_messages):
        assert any("read error" in w for w in report.warnings)


@needs_dwg
@pytest.mark.parametrize("path", DWG_FILES, ids=lambda p: p.name)
def test_dwg_round_trip_preserves_entity_counts(path, tmp_path):
    if find_oda(ODA) is None:
        pytest.skip("DWG writing requires ODA File Converter (LibreDWG is used read-only)")
    drawing, _ = import_dwg(path, ODA, LIBREDWG)
    out = tmp_path / "rt.dwg"
    export_dwg(drawing, out, ODA)
    again, _ = import_dwg(out, ODA, LIBREDWG)
    assert len(again.entities) == len(drawing.entities)
    assert {layer.name for layer in again.layers} >= {layer.name for layer in drawing.layers}
