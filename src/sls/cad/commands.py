"""Undoable edit commands for the drawing model."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .model import Drawing, Entity, Layer


class Command:
    label = "edit"

    def do(self, d: Drawing) -> None:
        raise NotImplementedError

    def undo(self, d: Drawing) -> None:
        raise NotImplementedError


@dataclass
class AddEntities(Command):
    entities: list[Entity]
    label: str = "add"

    def do(self, d: Drawing) -> None:
        for e in self.entities:
            d.add(e)

    def undo(self, d: Drawing) -> None:
        for e in self.entities:
            d.entities.pop(e.id, None)


@dataclass
class DeleteEntities(Command):
    ids: list[str]
    label: str = "erase"
    _removed: list[tuple[int, Entity]] = field(default_factory=list)

    def do(self, d: Drawing) -> None:
        order = list(d.entities)
        self._removed = [(order.index(i), d.entities.pop(i)) for i in self.ids if i in d.entities]

    def undo(self, d: Drawing) -> None:
        items = list(d.entities.items())
        for idx, e in sorted(self._removed, key=lambda t: t[0]):
            items.insert(idx, (e.id, e))
        d.entities = dict(items)


@dataclass
class ReplaceEntities(Command):
    """Swap entity versions (move/rotate/scale/property edits); may also add or drop entities."""

    before: list[Entity]
    after: list[Entity]
    label: str = "modify"

    def _apply(self, d: Drawing, remove: list[Entity], add: list[Entity]) -> None:
        add_ids = {e.id for e in add}
        for e in remove:
            if e.id not in add_ids:
                d.entities.pop(e.id, None)
        for e in add:
            d.entities[e.id] = copy.deepcopy(e)

    def do(self, d: Drawing) -> None:
        self._apply(d, self.before, self.after)

    def undo(self, d: Drawing) -> None:
        self._apply(d, self.after, self.before)


@dataclass
class LayerState(Command):
    """Snapshot-based command for layer operations (rename, delete, reorder, properties)."""

    before_layers: list[Layer]
    after_layers: list[Layer]
    before_entity_layers: dict[str, str]
    after_entity_layers: dict[str, str]
    before_active: str
    after_active: str
    label: str = "layers"
    added_entities: list[Entity] = field(default_factory=list)

    @classmethod
    def capture(cls, d: Drawing) -> tuple[list[Layer], dict[str, str], str, set[str]]:
        return (copy.deepcopy(d.layers), {i: e.layer for i, e in d.entities.items()}, d.active_layer,
                set(d.entities))

    def do(self, d: Drawing) -> None:
        d.layers = copy.deepcopy(self.after_layers)
        for e in self.added_entities:
            d.entities[e.id] = copy.deepcopy(e)
        for i, name in self.after_entity_layers.items():
            if i in d.entities:
                d.entities[i].layer = name
        d.active_layer = self.after_active

    def undo(self, d: Drawing) -> None:
        for e in self.added_entities:
            d.entities.pop(e.id, None)
        d.layers = copy.deepcopy(self.before_layers)
        for i, name in self.before_entity_layers.items():
            if i in d.entities:
                d.entities[i].layer = name
        d.active_layer = self.before_active


def layer_command(d: Drawing, fn, label: str) -> LayerState:  # type: ignore[no-untyped-def]
    """Run ``fn(d)`` (which mutates layers) and return the command describing it (already applied)."""
    layers, ent_layers, active, ids = LayerState.capture(d)
    fn(d)
    added = [copy.deepcopy(e) for i, e in d.entities.items() if i not in ids]
    return LayerState(layers, copy.deepcopy(d.layers), ent_layers, {i: e.layer for i, e in d.entities.items()},
                      active, d.active_layer, label, added)


class UndoStack:
    def __init__(self, drawing: Drawing, limit: int = 500) -> None:
        self.drawing = drawing
        self.done: list[Command] = []
        self.undone: list[Command] = []
        self.limit = limit
        self._clean_index = 0

    def push(self, cmd: Command, already_applied: bool = False) -> None:
        if not already_applied:
            cmd.do(self.drawing)
        self.done.append(cmd)
        self.undone.clear()
        if len(self.done) > self.limit:
            self.done.pop(0)
            self._clean_index -= 1

    def undo(self) -> Command | None:
        if not self.done:
            return None
        cmd = self.done.pop()
        cmd.undo(self.drawing)
        self.undone.append(cmd)
        return cmd

    def redo(self) -> Command | None:
        if not self.undone:
            return None
        cmd = self.undone.pop()
        cmd.do(self.drawing)
        self.done.append(cmd)
        return cmd

    def mark_clean(self) -> None:
        self._clean_index = len(self.done)

    @property
    def dirty(self) -> bool:
        return len(self.done) != self._clean_index
