"""Control-point transformation for regulatory plans."""
from __future__ import annotations

import math

import pytest

from sls.cad.model import Drawing, Entity
from sls.cad.transform import Similarity, fit, transform_drawing
from sls.core.errors import ValidationError


def test_translation_fit():
    r = fit([(0, 0), (10, 0)], [(100, 50), (110, 50)], "translation")
    assert (r.transform.tx, r.transform.ty) == pytest.approx((100, 50))
    assert r.rms == pytest.approx(0)


def test_similarity_fit_recovers_parameters_with_large_coordinates():
    true = Similarity(math.cos(math.radians(2)) * 1.001, math.sin(math.radians(2)) * 1.001, 230000.0, 1020000.0)
    src = [(0, 0), (100, 0), (100, 80), (0, 80), (37.5, 12.25)]
    dst = [true.apply(*p) for p in src]
    r = fit(src, dst)
    assert r.transform.rotation_deg == pytest.approx(2, abs=1e-9)
    assert r.transform.scale == pytest.approx(1.001, abs=1e-12)
    assert r.rms < 1e-6


def test_residuals_report_bad_control_point():
    src = [(0, 0), (100, 0), (100, 100), (0, 100)]
    dst = [(10, 10), (110, 10), (110, 110), (10, 111.0)]  # last pair off by 1 m
    r = fit(src, dst)
    assert max(r.residuals) > 0.4 and r.rms > 0.2


def test_fit_validation():
    with pytest.raises(ValidationError):
        fit([(0, 0)], [(1, 1)], "similarity")
    with pytest.raises(ValidationError):
        fit([(0, 0), (0, 0)], [(1, 1), (2, 2)])
    with pytest.raises(ValidationError):
        fit([(0, 0)], [], "translation")


def test_transform_drawing_does_not_modify_original():
    d = Drawing.with_default_layers()
    e = d.add(Entity("circle", "ROADS", {"center": [10, 0], "radius": 2}))
    t = Similarity(0.0, 2.0, 5.0, 5.0)  # rotate 90°, scale 2, shift (5,5)
    out = transform_drawing(d, t)
    assert d.entities[e.id].data["center"] == [10, 0]
    assert out.entities[e.id].data["center"] == pytest.approx([5, 25])
    assert out.entities[e.id].data["radius"] == pytest.approx(4)


def test_service_applies_transformation_as_new_revision(workspace, sample_dxf):
    from sls.persistence.cases import Case
    from sls.services.case_service import CaseService

    svc = CaseService(workspace)
    case = workspace.cases.save(workspace.user, Case(case_number="TF-1"))
    reg = svc.import_drawing(case.id, sample_dxf, "regulatory")
    before = next(iter(reg.drawing.entities.values()))
    out = svc.apply_transformation(reg.record.id, [(0, 0), (10, 0)], [(5, 5), (15, 5)], "translation")
    assert out.revision.revision == reg.revision.revision + 1
    moved = out.drawing.entities[before.id]
    assert moved.vertices()[0][0] == pytest.approx(before.vertices()[0][0] + 5)
    assert out.drawing.info["transforms"][0]["kind"] == "translation"
    original = svc.load_drawing(reg.record.id, reg.revision.id).drawing
    assert original.entities[before.id].vertices()[0] == before.vertices()[0]
    assert any(a["action"] == "transform_drawing" for a in workspace.audit.for_case(case.id))
