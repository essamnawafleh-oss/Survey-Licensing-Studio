"""Site photographs, general attachments and professional notes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


@dataclass
class Photo:
    id: str
    file_id: str
    original_name: str
    caption: str
    taken_at: str
    include_in_report: bool
    sort_order: int


@dataclass
class Attachment:
    id: str
    file_id: str
    original_name: str
    title: str
    include_in_report: bool
    page_selection: str
    sort_order: int


@dataclass
class CaseNote:
    id: str
    section: str
    text: str
    author: str
    created_at: str
    updated_at: str


NOTE_SECTIONS = ("general", "setbacks", "areas", "licenses", "violations", "recommendation")


class MediaRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    # photos ---------------------------------------------------------------------------
    def add_photo(self, user: "User", case_id: str, source: Path, caption: str = "", taken_at: str = "") -> Photo:
        user.require("edit_case")
        with self.db.transaction():
            fr = self.ws.files.add(user, Path(source), case_id, "photo")
            order = int(self.db.query_one("SELECT COALESCE(MAX(sort_order),0) FROM photos WHERE case_id=?",
                                          (case_id,))[0]) + 1
            pid = new_id()
            self.db.execute("INSERT INTO photos(id,case_id,file_id,caption,taken_at,sort_order) VALUES (?,?,?,?,?,?)",
                            (pid, case_id, fr.id, caption, taken_at, order))
            self.ws.audit.record(user, "add_photo", "photo", pid, case_id, file=fr.original_name, sha256=fr.sha256)
        return next(p for p in self.photos(case_id) if p.id == pid)

    def photos(self, case_id: str) -> list[Photo]:
        rows = self.db.query("SELECT p.*, f.original_name FROM photos p JOIN files f ON f.id=p.file_id "
                             "WHERE p.case_id=? ORDER BY p.sort_order", (case_id,))
        return [Photo(r["id"], r["file_id"], r["original_name"], r["caption"], r["taken_at"],
                      bool(r["include_in_report"]), r["sort_order"]) for r in rows]

    def update_photo(self, user: "User", photo_id: str, caption: str, include: bool) -> None:
        user.require("edit_case")
        self.db.execute("UPDATE photos SET caption=?, include_in_report=? WHERE id=?", (caption, int(include), photo_id))

    # attachments ----------------------------------------------------------------------
    def add_attachment(self, user: "User", case_id: str, source: Path, title: str = "") -> Attachment:
        user.require("edit_case")
        with self.db.transaction():
            fr = self.ws.files.add(user, Path(source), case_id, "attachment")
            order = int(self.db.query_one("SELECT COALESCE(MAX(sort_order),0) FROM attachments WHERE case_id=?",
                                          (case_id,))[0]) + 1
            aid = new_id()
            self.db.execute("INSERT INTO attachments(id,case_id,file_id,title,sort_order) VALUES (?,?,?,?,?)",
                            (aid, case_id, fr.id, title or fr.original_name, order))
            self.ws.audit.record(user, "add_attachment", "attachment", aid, case_id, file=fr.original_name)
        return next(a for a in self.attachments(case_id) if a.id == aid)

    def attachments(self, case_id: str) -> list[Attachment]:
        rows = self.db.query("SELECT a.*, f.original_name FROM attachments a JOIN files f ON f.id=a.file_id "
                             "WHERE a.case_id=? ORDER BY a.sort_order", (case_id,))
        return [Attachment(r["id"], r["file_id"], r["original_name"], r["title"], bool(r["include_in_report"]),
                           r["page_selection"], r["sort_order"]) for r in rows]

    def update_attachment(self, user: "User", attachment_id: str, title: str, include: bool,
                          page_selection: str) -> None:
        user.require("edit_case")
        from ..reporting.pages import parse_page_selection

        parse_page_selection(page_selection, None)
        self.db.execute("UPDATE attachments SET title=?, include_in_report=?, page_selection=? WHERE id=?",
                        (title, int(include), page_selection.strip(), attachment_id))

    # notes --------------------------------------------------------------------------------
    def notes(self, case_id: str) -> list[CaseNote]:
        rows = self.db.query("SELECT * FROM case_notes WHERE case_id=? ORDER BY created_at", (case_id,))
        return [CaseNote(r["id"], r["section"], r["text"], r["author"], r["created_at"], r["updated_at"]) for r in rows]

    def save_note(self, user: "User", case_id: str, section: str, text: str, note_id: str = "") -> str:
        """Professional notes are stored separately and never alter calculation results."""
        user.require("edit_case")
        ts = now_iso()
        with self.db.transaction():
            if note_id:
                self.db.execute("UPDATE case_notes SET section=?, text=?, updated_at=? WHERE id=?",
                                (section, text, ts, note_id))
            else:
                note_id = new_id()
                self.db.execute("INSERT INTO case_notes(id,case_id,section,text,author,created_at,updated_at)"
                                " VALUES (?,?,?,?,?,?,?)", (note_id, case_id, section, text, user.username, ts, ts))
            self.ws.audit.record(user, "save_note", "case_note", note_id, case_id, section=section)
        return note_id
