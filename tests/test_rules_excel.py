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


def test_structural_errors_block_but_conditional_values_are_kept(tmp_path):
    rows = [
        ["X1", "a", "a", 5, None, 3, 0.4, 3, 1, ""],                # blank side -> conditional
        ["X2", "b", "b", "4م بعد 14م", 3, 3, 0.4, 3, 1, ""],        # conditional text kept verbatim
        ["X3", "c", "c", 4, -1, 3, 0.4, 3, 1, ""],                  # negative -> blocking error
        ["X4", "d", "d", 4, 3, 3, 0.4, 3.5, 1, ""],                 # fractional floors -> not a plain count
        ["X4", "e", "e", 4, 3, 3, 0.4, 3, 1, ""],                   # duplicate code -> blocking error
        [None, "f", "f", 4, 3, 3, 0.4, 3, 1, ""],                   # missing code -> blocking error
        [None] * 10,                                                # blank row: skipped and counted
        ["X5", "g", "g", "٤٫٥", "صفر", "4م", 0.4, 3, 1, ""],          # Arabic digits, صفر, metres suffix
    ]
    path = write_sample_rules_xlsx(tmp_path / "bad.xlsx", rows)
    res = validate(read_sheet(path, "Setbacks", 2), ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    assert not res.ok
    msgs = " | ".join(f"{e.row}:{e.message}" for e in res.errors)
    assert "negative" in msgs and "Duplicate classification code 'X4'" in msgs
    assert "Classification code is missing" in msgs
    assert res.skipped_blank_rows == 1
    by_code = {r.code: r for r in res.rules}
    assert by_code["X1"].status == "conditional" and "side" not in by_code["X1"].setbacks
    assert by_code["X2"].status == "conditional" and by_code["X2"].setbacks_raw["front"] == "4م بعد 14م"
    assert "front" not in by_code["X2"].setbacks
    assert by_code["X5"].status == "numeric"
    assert by_code["X5"].setbacks == {"front": 4.5, "side": 0.0, "rear": 4.0}


def test_parse_value_only_accepts_unambiguous_forms():
    from sls.rules.excel_rules import Cell, parse_value

    def n(v, kind="length", fmt="General"):
        return parse_value(Cell(v, fmt), kind).number

    assert n("5") == 5 and n("2.5") == 2.5 and n("صفر") == 0 and n("4م") == 4 and n(3.0) == 3
    for text in ("صفر - 2", "4م بعد 14م", "حسب المخطط", "صفر لطابقين و2م للبقية", "3م عن الطريق", "ـــــ"):
        p = parse_value(Cell(text), "length")
        assert p.number is None and p.state == "text" and p.raw == text
    assert n("39%", "percent") == 39 and n("%15", "percent") == 15 and n(0.45, "percent", "0%") == 45
    assert n(0.45, "percent") is None  # ambiguous without a % format
    assert n("ضمن الارتدادات", "percent") is None
    assert n("30%بحد اقصى 100م2", "percent") is None
    assert n("4", "count") == 4 and n("*طابقين+روف", "count") is None and n("3.5", "count") is None
    assert parse_value(Cell(None)).state == "blank" and parse_value(Cell("  ")).state == "blank"


def test_rule_statuses_and_suggested_mapping(tmp_path):
    from openpyxl import Workbook

    from sls.rules.excel_rules import suggest_mapping

    wb = Workbook()
    ws = wb.active
    ws.append(["OBJECTID", "ZONE_ID", "NAME", "BUILT_UP_AREA_PERCENT", "SETBACK_FRONT", "SETBACK_SIDE",
               "SETBACK_BACK", "FLOOR_NO", "REMARK"])
    ws.append([1, 1, "صفة تجريبية أ", "39%", "5", "4", "5", "4", " "])
    ws.append([2, 5, "صفة تجريبية ب", "ضمن الارتدادات", "حسب المخطط", "4م بعد 14م", "4", "4", ""])
    ws.append([3, 12, "صفة تجريبية ج", None, None, None, None, None, ""])
    p = tmp_path / "zones.xlsx"
    wb.save(p)
    prev = read_sheet(p, "Sheet", 1)
    m = suggest_mapping(prev.headers)
    assert m.columns["code"] == "ZONE_ID" and m.columns["rear"] == "SETBACK_BACK"
    assert m.columns["max_coverage_pct"] == "BUILT_UP_AREA_PERCENT" and m.columns["name_ar"] == "NAME"
    res = validate(prev, m)
    assert res.ok
    assert (res.count("numeric"), res.count("conditional"), res.count("no_setbacks")) == (1, 1, 1)
    b = select_rule(res.rules, "5")
    assert b.setbacks == {"rear": 4.0} and b.coverage_raw == "ضمن الارتدادات" and b.max_coverage_pct is None
    assert b.source["OBJECTID"] == "2"  # full source row kept verbatim


def test_legacy_xls_workbook(tmp_path):
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("ZONING")
    for c, h in enumerate(["ZONE_ID", "NAME", "SETBACK_FRONT", "SETBACK_SIDE", "SETBACK_BACK", "BUILT_UP_AREA_PERCENT"]):
        ws.write(0, c, h)
    for r, row in enumerate([[1.0, "صفة تجريبية", "5", "4", "5", "39%"], [2.0, "صفة مشروطة", "صفر", "صفر - 2", "3", "50%"]],
                            start=1):
        for c, v in enumerate(row):
            ws.write(r, c, v)
    path = tmp_path / "zones.xls"
    wb.save(str(path))
    assert list_sheets(path) == ["ZONING"]
    from sls.rules.excel_rules import suggest_mapping

    prev = read_sheet(path, "ZONING", 1)
    res = validate(prev, suggest_mapping(prev.headers))
    assert res.ok
    a, b = select_rule(res.rules, "1"), select_rule(res.rules, "2")
    assert a.setbacks == {"front": 5, "side": 4, "rear": 5} and a.max_coverage_pct == 39
    assert b.status == "conditional" and b.setbacks_raw["side"] == "صفر - 2" and b.setbacks["front"] == 0


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
