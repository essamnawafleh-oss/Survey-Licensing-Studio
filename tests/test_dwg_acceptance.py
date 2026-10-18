"""DWG acceptance tests.

These require (1) ODA File Converter installed (or SLS_ODA_CONVERTER set) and (2) real
DWG files in tests/fixtures/dwg/ (set SLS_DWG_FIXTURES to override). DXF tests never
count as DWG evidence. Until both prerequisites exist the tests SKIP with a reason and
DWG support remains unverified.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sls.cad.dwg import dwg_status, export_dwg, find_converter, import_dwg
from sls.core.errors import DwgUnavailableError

CONVERTER = find_converter(os.environ.get("SLS_ODA_CONVERTER", ""))
FIXTURE_DIR = Path(os.environ.get("SLS_DWG_FIXTURES", Path(__file__).parent / "fixtures" / "dwg"))
DWG_FILES = sorted(FIXTURE_DIR.glob("*.dwg")) if FIXTURE_DIR.is_dir() else []

needs_dwg = pytest.mark.skipif(CONVERTER is None or not DWG_FILES,
                               reason="DWG acceptance needs ODA File Converter and real DWG fixtures")


def test_missing_converter_is_reported_explicitly(tmp_path):
    if CONVERTER is not None:
        pytest.skip("converter installed")
    with pytest.raises(DwgUnavailableError):
        import_dwg(tmp_path / "x.dwg", "")
    assert "unavailable" in dwg_status("")


@needs_dwg
@pytest.mark.parametrize("path", DWG_FILES, ids=lambda p: p.name)
def test_dwg_import_has_entities_and_layers(path):
    drawing, report = import_dwg(path, str(CONVERTER))
    assert report.total_imported > 0
    assert len(drawing.layers) > 0


@needs_dwg
@pytest.mark.parametrize("path", DWG_FILES, ids=lambda p: p.name)
def test_dwg_round_trip_preserves_entity_counts(path, tmp_path):
    drawing, report = import_dwg(path, str(CONVERTER))
    out = tmp_path / "rt.dwg"
    export_dwg(drawing, out, str(CONVERTER))
    again, _ = import_dwg(out, str(CONVERTER))
    assert len(again.entities) == len(drawing.entities)
    assert {layer.name for layer in again.layers} >= {layer.name for layer in drawing.layers}
