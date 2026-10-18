"""Edit operations, measurements and undo/redo."""
from __future__ import annotations

import math

import pytest

from sls.cad import ops
from sls.cad.commands import AddEntities, DeleteEntities, ReplaceEntities, UndoStack, layer_command
from sls.cad.model import Drawing, Entity


def line(x1, y1, x2, y2, layer="WALLS"):
    return Entity("line", layer, {"p1": [x1, y1], "p2": [x2, y2]})


def test_measurements():
    rect = Entity("polyline", "BUILDINGS", {"points": [[0, 0], [10, 0], [10, 5], [0, 5]], "closed": True})
    assert ops.entity_area(rect) == pytest.approx(50)
    assert ops.entity_length(rect) == pytest.approx(30)
    circ = Entity("circle", "0", {"center": [0, 0], "radius": 2})
    assert ops.entity_area(circ) == pytest.approx(math.pi * 4)
    assert ops.entity_area(line(0, 0, 1, 1)) is None
    area, valid = ops.polygon_area([(0, 0), (4, 0), (4, 3)])
    assert area == pytest.approx(6) and valid
    _, valid = ops.polygon_area([(0, 0), (4, 4), (4, 0), (0, 4)])
    assert not valid


def test_transforms():
    e = line(0, 0, 10, 0)
    assert ops.translate(e, 5, 5).data["p2"] == [15, 5]
    r = ops.rotate(e, (0, 0), 90)
    assert r.data["p2"] == pytest.approx([0, 10])
    s = ops.scale(e, (0, 0), 2)
    assert s.data["p2"] == [20, 0]
    assert ops.rotate(e, (0, 0), 90).id == e.id  # same identity, new geometry
    t = Entity("text", "LABELS", {"position": [1, 1], "text": "نص", "height": 1, "rotation": 0})
    assert ops.scale(t, (0, 0), 3).data["height"] == 3
    with pytest.raises(ValueError):
        ops.scale(e, (0, 0), 0)


def test_offset_line_circle_polyline():
    off = ops.offset(line(0, 0, 10, 0), 2, (5, 5))
    assert off.data["p1"] == pytest.approx([0, 2]) and off.id != "x"
    off2 = ops.offset(line(0, 0, 10, 0), 2, (5, -5))
    assert off2.data["p1"] == pytest.approx([0, -2])
    c = Entity("circle", "0", {"center": [0, 0], "radius": 5})
    assert ops.offset(c, 1, (0, 0)).data["radius"] == 4
    assert ops.offset(c, 1, (10, 0)).data["radius"] == 6
    sq = Entity("polyline", "0", {"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "closed": True})
    inner = ops.offset(sq, 1, (5, 5))
    assert ops.entity_area(inner) == pytest.approx(64)
    outer = ops.offset(sq, 1, (20, 20))
    assert ops.entity_area(outer) == pytest.approx(144)
    with pytest.raises(ValueError):
        ops.offset(sq, 6, (5, 5))


def test_trim_and_extend():
    target = line(0, 0, 10, 0)
    cutters = [line(3, -1, 3, 1), line(7, -1, 7, 1)]
    pieces = ops.trim_line(target, (5, 0.1), cutters)
    assert len(pieces) == 2
    assert pieces[0].id == target.id and pieces[1].id != target.id
    assert pieces[0].data["p2"] == pytest.approx([3, 0]) and pieces[1].data["p1"] == pytest.approx([7, 0])
    end_piece = ops.trim_line(target, (9, 0), cutters)
    assert len(end_piece) == 1 and end_piece[0].data["p2"] == pytest.approx([7, 0])
    with pytest.raises(ValueError):
        ops.trim_line(target, (5, 0), [])
    short = line(0, 0, 4, 0)
    ext = ops.extend_line(short, (3.9, 0), [line(10, -5, 10, 5)])
    assert ext.data["p2"] == pytest.approx([10, 0])
    with pytest.raises(ValueError):
        ops.extend_line(short, (3.9, 0), [line(-10, -5, -10, -4)])


def test_hit_test_and_snaps():
    e = line(0, 0, 10, 0)
    assert ops.hit_distance(e, 5, 1) == pytest.approx(1)
    kinds = {k for *_, k in ops.snap_points(e)}
    assert kinds == {"end", "mid"}


def test_undo_redo_stack():
    d = Drawing.with_default_layers()
    stack = UndoStack(d)
    e = line(0, 0, 1, 0)
    stack.push(AddEntities([e]))
    assert e.id in d.entities and stack.dirty
    moved = ops.translate(e, 1, 0)
    stack.push(ReplaceEntities([e.clone()], [moved]))
    assert d.entities[e.id].data["p1"] == [1, 0]
    stack.push(DeleteEntities([e.id]))
    assert e.id not in d.entities
    stack.undo()
    assert d.entities[e.id].data["p1"] == [1, 0]
    stack.undo()
    assert d.entities[e.id].data["p1"] == [0, 0]
    stack.redo()
    assert d.entities[e.id].data["p1"] == [1, 0]
    cmd = layer_command(d, lambda dr: dr.rename_layer("WALLS", "WALLS_NEW"), "rename")
    stack.push(cmd, already_applied=True)
    assert d.entities[e.id].layer == "WALLS_NEW"
    stack.undo()
    assert d.entities[e.id].layer == "WALLS" and d.has_layer("WALLS")
    dup = layer_command(d, lambda dr: dr.duplicate_layer("WALLS"), "dup")
    stack.push(dup, already_applied=True)
    assert len(d.entities) == 2
    stack.undo()
    assert len(d.entities) == 1
    stack.redo()
    assert len(d.entities) == 2
