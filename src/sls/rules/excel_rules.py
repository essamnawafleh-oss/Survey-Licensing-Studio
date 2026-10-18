"""Land-use setback rule tables from Excel workbooks.

Workflow: inspect -> preview sheet -> explicit column mapping -> validate -> import.
Nothing is inferred: missing, non-numeric, negative or duplicate values are errors and
block the import. Blank rows are skipped and counted.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..core.errors import AmbiguousRuleError, MissingRuleError, RuleImportError

REQUIRED_FIELDS = ("code", "front", "side", "rear")
OPTIONAL_FIELDS = ("name_ar", "name_en", "max_coverage_pct", "max_floors", "max_far", "notes")
BASE_BOUNDARY_TYPES = ("front", "side", "rear")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


@dataclass
class Cell:
    value: Any
    number_format: str = "General"


@dataclass
class SheetPreview:
    sheet: str
    header_row: int  # 1-based
    headers: list[str]
    rows: list[list[Cell]]  # data rows after the header
    first_data_row: int


@dataclass
class ColumnMapping:
    """Maps logical fields to header names in the sheet.

    ``extra_setbacks`` maps additional boundary types (e.g. "corner", "side_2") to headers,
    for rule tables that define more than front/side/rear.
    """

    columns: dict[str, str] = field(default_factory=dict)
    extra_setbacks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ColumnMapping":
        return cls(dict(d.get("columns", {})), dict(d.get("extra_setbacks", {})))


@dataclass
class LandUseRule:
    code: str
    setbacks: dict[str, float]
    source_row: int
    name_ar: str = ""
    name_en: str = ""
    max_coverage_pct: float | None = None
    max_floors: int | None = None
    max_far: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LandUseRule":
        return cls(**dict(d))  # type: ignore[arg-type]

    def label(self, lang: str) -> str:
        name = self.name_ar if lang == "ar" else self.name_en
        return f"{self.code} — {name}" if name else self.code


@dataclass
class RowError:
    row: int
    column: str
    message: str


@dataclass
class RuleValidation:
    rules: list[LandUseRule]
    errors: list[RowError]
    skipped_blank_rows: int

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rules)


def list_sheets(path: Path) -> list[str]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def read_sheet(path: Path, sheet: str, header_row: int = 1, max_rows: int = 5000) -> SheetPreview:
    # data_only=True: use cached formula results. Formula cells without cached values
    # (workbook never saved by Excel) come back as None and are flagged as missing.
    wb = load_workbook(path, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise RuleImportError(f"Worksheet '{sheet}' not found in {Path(path).name}.")
        ws = wb[sheet]
        if header_row < 1 or header_row > ws.max_row:
            raise RuleImportError(f"Header row {header_row} is outside the sheet (1..{ws.max_row}).")
        headers = [_text(c.value) for c in ws[header_row]]
        while headers and not headers[-1]:
            headers.pop()
        if not any(headers):
            raise RuleImportError(f"Header row {header_row} is empty.")
        dupes = {h for h in headers if h and headers.count(h) > 1}
        if dupes:
            raise RuleImportError(f"Duplicate column headers: {', '.join(sorted(dupes))}")
        rows: list[list[Cell]] = []
        for r in ws.iter_rows(min_row=header_row + 1, max_row=min(ws.max_row, header_row + max_rows)):
            rows.append([Cell(c.value, c.number_format or "General") for c in r[: len(headers)]])
        return SheetPreview(sheet, header_row, headers, rows, header_row + 1)
    finally:
        wb.close()


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _number(cell: Cell | None, percent_field: bool = False) -> float | None:
    """Parse a numeric cell. Returns None for blank; raises ValueError for anything else."""
    if cell is None or cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
        return None
    v = cell.value
    if isinstance(v, bool):
        raise ValueError("boolean is not a number")
    if isinstance(v, (int, float)):
        num = float(v)
        if percent_field and "%" in (cell.number_format or ""):
            num *= 100.0  # Excel stores 60% as 0.6
    else:
        s = str(v).strip().translate(_ARABIC_DIGITS).replace(",", "")
        pct = percent_field and s.endswith("%")
        if pct:
            s = s[:-1].strip()
        num = float(s)  # raises ValueError for text such as "4 m"
    if not math.isfinite(num):
        raise ValueError("not a finite number")
    return num


def validate(preview: SheetPreview, mapping: ColumnMapping) -> RuleValidation:
    errors: list[RowError] = []
    header_index = {h: i for i, h in enumerate(preview.headers) if h}
    for fld in REQUIRED_FIELDS:
        if not mapping.columns.get(fld):
            errors.append(RowError(preview.header_row, fld, f"Required field '{fld}' is not mapped."))
    all_cols = {**mapping.columns, **{f"setback:{k}": v for k, v in mapping.extra_setbacks.items()}}
    for fld, header in all_cols.items():
        if header and header not in header_index:
            errors.append(RowError(preview.header_row, fld, f"Mapped column '{header}' does not exist."))
    for key in mapping.extra_setbacks:
        if key in BASE_BOUNDARY_TYPES or not key.strip():
            errors.append(RowError(preview.header_row, key, f"Invalid extra boundary type '{key}'."))
    if errors:
        return RuleValidation([], errors, 0)

    def cell(row: list[Cell], fld_header: str | None) -> Cell | None:
        if not fld_header:
            return None
        idx = header_index[fld_header]
        return row[idx] if idx < len(row) else None

    rules: list[LandUseRule] = []
    seen: dict[str, int] = {}
    blank = 0
    for offset, row in enumerate(preview.rows):
        rownum = preview.first_data_row + offset
        if all(c.value is None or (isinstance(c.value, str) and not c.value.strip()) for c in row):
            blank += 1
            continue
        code = _text(cell(row, mapping.columns["code"]).value if cell(row, mapping.columns["code"]) else None)
        if not code:
            errors.append(RowError(rownum, "code", "Classification code is missing."))
            continue
        if code in seen:
            errors.append(RowError(rownum, "code", f"Duplicate classification code '{code}' (also row {seen[code]})."))
            continue
        seen[code] = rownum
        setbacks: dict[str, float] = {}
        row_ok = True
        sb_cols = {k: mapping.columns[k] for k in BASE_BOUNDARY_TYPES}
        sb_cols.update(mapping.extra_setbacks)
        for btype, header in sb_cols.items():
            try:
                val = _number(cell(row, header))
            except ValueError:
                errors.append(RowError(rownum, header, f"Setback '{btype}' is not a number: "
                                                       f"{cell(row, header).value!r}"))
                row_ok = False
                continue
            if val is None:
                if btype in BASE_BOUNDARY_TYPES:
                    errors.append(RowError(rownum, header, f"Setback '{btype}' is missing."))
                    row_ok = False
                continue  # extra boundary types may legitimately be blank (not defined)
            if val < 0:
                errors.append(RowError(rownum, header, f"Setback '{btype}' is negative ({val})."))
                row_ok = False
                continue
            setbacks[btype] = val
        optional: dict[str, Any] = {}
        for fld in ("max_coverage_pct", "max_floors", "max_far"):
            header = mapping.columns.get(fld)
            if not header:
                continue
            try:
                val = _number(cell(row, header), percent_field=(fld == "max_coverage_pct"))
            except ValueError:
                errors.append(RowError(rownum, header, f"'{fld}' is not a number: {cell(row, header).value!r}"))
                row_ok = False
                continue
            if val is not None and val < 0:
                errors.append(RowError(rownum, header, f"'{fld}' is negative."))
                row_ok = False
                continue
            if fld == "max_coverage_pct" and val is not None and val > 100:
                errors.append(RowError(rownum, header, f"Coverage {val}% exceeds 100%."))
                row_ok = False
                continue
            if fld == "max_floors" and val is not None:
                if not float(val).is_integer():
                    errors.append(RowError(rownum, header, "Maximum floors must be a whole number."))
                    row_ok = False
                    continue
                val = int(val)
            optional[fld] = val
        if not row_ok:
            continue
        texts = {f: _text(cell(row, mapping.columns.get(f)).value) if cell(row, mapping.columns.get(f)) else ""
                 for f in ("name_ar", "name_en", "notes")}
        rules.append(LandUseRule(code=code, setbacks=setbacks, source_row=rownum, **texts, **optional))
    if not rules and not errors:
        errors.append(RowError(preview.first_data_row, "", "The sheet contains no rule rows."))
    return RuleValidation(rules, errors, blank)


def select_rule(rules: Sequence[LandUseRule], code: str) -> LandUseRule:
    """Return the single rule for ``code``; never falls back to another rule."""
    code = code.strip()
    if not code:
        raise MissingRuleError("No land-use classification has been selected for this case.")
    matches = [r for r in rules if r.code == code]
    if not matches:
        raise MissingRuleError(f"Land-use classification '{code}' is not defined in the applied rule version.",
                               code=code)
    if len(matches) > 1:
        raise AmbiguousRuleError(f"Land-use classification '{code}' is defined more than once.", code=code)
    return matches[0]
