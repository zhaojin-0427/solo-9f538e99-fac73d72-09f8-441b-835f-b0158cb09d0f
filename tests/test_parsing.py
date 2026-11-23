import pytest

from app.parsing import (
    MAX_ENTRIES,
    ParseError,
    parse_batch,
    parse_date_totals,
    parse_entries,
    parse_window_totals,
)

ENTRIES_CSV = """票号,窗口,售票日,候选金额1,候选金额2,候选金额3
T1,W1,D1,100,200,300
T2,W1,D1,400,500
T3,W2,D2,600,700,800
"""

WINDOW_CSV = """窗口,合计
W1,700
W2,700
"""

DATE_CSV = """售票日,合计
D1,700
D2,700
"""


def test_parse_batch_happy_path():
    parsed = parse_batch(ENTRIES_CSV, WINDOW_CSV, DATE_CSV)
    assert len(parsed.entries) == 3
    assert parsed.entries[0].ticket_no == "T1"
    assert parsed.entries[0].candidates == (100, 200, 300)
    assert parsed.entries[1].candidates == (400, 500)
    assert {r.group_key: r.total for r in parsed.window_totals} == {"W1": 700, "W2": 700}


def test_english_headers_accepted():
    en = (
        "ticket_no,window,sale_date,candidate1,candidate2,candidate3\n"
        "A1,W1,D1,10,20,30\n"
    )
    rows = parse_entries(en)
    assert rows[0].ticket_no == "A1"
    wt = parse_window_totals("window,total\nW1,10\n")
    assert wt[0].group_key == "W1" and wt[0].total == 10
    dt = parse_date_totals("sale_date,total\nD1,10\n")
    assert dt[0].group_key == "D1" and dt[0].total == 10
    parsed = parse_batch(en, "窗口,合计\nW1,10\n", "售票日,合计\nD1,10\n")
    assert parsed.entries[0].candidates == (10, 20, 30)


def test_blank_lines_and_bom_tolerated():
    raw = "﻿票号,窗口,售票日,候选金额1\nT9,W3,D3,42\n\n"
    rows = parse_entries(raw)
    assert rows[0].ticket_no == "T9"


def test_duplicate_ticket_rejected():
    bad = (
        "票号,窗口,售票日,候选金额1\n"
        "T1,W1,D1,10\n"
        "T1,W2,D2,20\n"
    )
    with pytest.raises(ParseError, match="重复"):
        parse_entries(bad)


def test_non_integer_amount_rejected():
    bad = "票号,窗口,售票日,候选金额1\nT1,W1,D1,12.5\n"
    with pytest.raises(ParseError, match="非负整数"):
        parse_entries(bad)


def test_amount_out_of_range_rejected():
    bad = "票号,窗口,售票日,候选金额1\nT1,W1,D1,1000001\n"
    with pytest.raises(ParseError, match="超出允许范围"):
        parse_entries(bad)


def test_too_many_entries_rejected():
    lines = ["票号,窗口,售票日,候选金额1"]
    lines += [f"T{i},W1,D1,100" for i in range(MAX_ENTRIES + 1)]
    with pytest.raises(ParseError, match="最多 24 笔"):
        parse_entries("\n".join(lines))


def test_missing_candidate_rejected():
    bad = "票号,窗口,售票日,候选金额1\nT1,W1,D1,\n"
    with pytest.raises(ParseError, match="至少需要 1 个"):
        parse_entries(bad)


def test_missing_window_total_rejected():
    with pytest.raises(ParseError, match="窗口合计表缺少窗口"):
        parse_batch(
            ENTRIES_CSV,
            "窗口,合计\nW1,700\n",
            DATE_CSV,
        )


def test_extra_total_key_rejected():
    with pytest.raises(ParseError, match="不存在的窗口"):
        parse_batch(
            ENTRIES_CSV,
            "窗口,合计\nW1,700\nW2,700\nW9,1\n",
            DATE_CSV,
        )


def test_total_out_of_candidate_range_rejected():
    with pytest.raises(ParseError, match="可达范围"):
        parse_batch(
            ENTRIES_CSV,
            "窗口,合计\nW1,9999\nW2,700\n",
            DATE_CSV,
        )


def test_two_total_sums_must_match():
    with pytest.raises(ParseError, match="两套合计总额不一致"):
        parse_batch(
            ENTRIES_CSV,
            "窗口,合计\nW1,700\nW2,700\n",
            "售票日,合计\nD1,700\nD2,701\n",
        )
