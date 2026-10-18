"""Excel rule-table import tests (synthetic rule values)."""
from __future__ import annotations

import pytest

from sls.core.errors import AmbiguousRuleError, MissingRuleError, RuleImportError
from sls.rules.excel_rules import ColumnMapping, LandUseRule, list_sheets, read_sheet, select_rule, validate
from sls.samples import RULE_HEADERS, SAMPLE_RULE_MAPPING, write_sample_rules_xlsx


def test_inspect_preview_and_import(sample_xlsx):
    assert list_sheets(sample_xlsx) == ["Setbacks", "Notes"]
    prev = read_sheet(sample_xlsx, "Setbacks", header_row=2)
    assert prev.headers == RULE_HEADERS
    res = validate(prev, ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert res.ok, res.errors
    a = select_rule(res.rules, "SYN-A")
    assert a.setbacks == {"front": 5, "side": 4, "rear": 4}
    assert a.max_coverage_pct == pytest.approx(45)  # 0.45 formatted as % -> 45
    assert a.max_floors == 4 and a.name_ar == "سكن تجريبي أ"
    assert a.source_row == 3


def test_missing_invalid_negative_duplicate_values_block(tmp_path):
    rows = [
        ["X1", "a", "a", 5, None, 3, 0.4, 3, 1, ""],      # missing side
        ["X2", "b", "b", "4 m", 3, 3, 0.4, 3, 1, ""],     # non-numeric
        ["X3", "c", "c", 4, -1, 3, 0.4, 3, 1, ""],        # negative
        ["X4", "d", "d", 4, 3, 3, 0.4, 3.5, 1, ""],       # fractional floors
        ["X4", "e", "e", 4, 3, 3, 0.4, 3, 1, ""],         # duplicate code
        [None, "f", "f", 4, 3, 3, 0.4, 3, 1, ""],         # missing code
        [None] * 10,                                      # blank row: skipped and counted
        ["X5", "g", "g", "٤٫٥", 3, 3, 0.4, 3, 1, ""],      # Arabic-Indic digits are valid
    ]
    path = write_sample_rules_xlsx(tmp_path / "bad.xlsx", rows)
    res = validate(read_sheet(path, "Setbacks", 2), ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert not res.ok
    msgs = " | ".join(f"{e.row}:{e.message}" for e in res.errors)
    assert "3:Setback 'side' is missing" in msgs
    assert "not a number" in msgs and "negative" in msgs and "whole number" in msgs
    assert "Duplicate classification code 'X4'" in msgs
    assert "Classification code is missing" in msgs
    assert res.skipped_blank_rows == 1
    assert [r.code for r in res.rules] == ["X5"]
    assert res.rules[0].setbacks["front"] == pytest.approx(4.5)


def test_duplicate_code_detected(tmp_path):
    rows = [["D", "", "", 1, 1, 1, None, None, None, ""], ["D", "", "", 2, 2, 2, None, None, None, ""]]
    path = write_sample_rules_xlsx(tmp_path / "dup.xlsx", rows)
    res = validate(read_sheet(path, "Setbacks", 2), ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert any("Duplicate classification code 'D'" in e.message for e in res.errors)


def test_unmapped_required_and_unknown_columns(sample_xlsx):
    prev = read_sheet(sample_xlsx, "Setbacks", 2)
    res = validate(prev, ColumnMapping({"code": "رمز التصنيف", "front": "nope"}))
    msgs = [e.message for e in res.errors]
    assert any("'side' is not mapped" in m for m in msgs)
    assert any("'nope' does not exist" in m for m in msgs)


def test_extra_boundary_types(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["code", "front", "side", "rear", "corner"])
    ws.append(["Z", 4, 3, 3, 2])
    ws.append(["Y", 4, 3, 3, None])
    p = tmp_path / "x.xlsx"
    wb.save(p)
    m = ColumnMapping({"code": "code", "front": "front", "side": "side", "rear": "rear"}, {"corner": "corner"})
    res = validate(read_sheet(p, "Sheet", 1), m)
    assert res.ok
    assert select_rule(res.rules, "Z").setbacks["corner"] == 2
    assert "corner" not in select_rule(res.rules, "Y").setbacks  # undefined, not invented


def test_select_rule_never_falls_back():
    rules = [LandUseRule("A", {"front": 1, "side": 1, "rear": 1}, 2)]
    with pytest.raises(MissingRuleError):
        select_rule(rules, "B")
    with pytest.raises(MissingRuleError):
        select_rule(rules, "")
    with pytest.raises(AmbiguousRuleError):
        select_rule(rules + rules, "A")


def test_bad_sheet_and_header(sample_xlsx):
    with pytest.raises(RuleImportError):
        read_sheet(sample_xlsx, "Missing", 1)
    with pytest.raises(RuleImportError):
        read_sheet(sample_xlsx, "Setbacks", 500)
