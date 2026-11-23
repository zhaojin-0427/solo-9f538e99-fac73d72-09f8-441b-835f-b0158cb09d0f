"""上传 CSV 的解析与业务校验。

票款清单 CSV（UTF-8，首行表头），表头别名兼容中英文：
    票号/ticket_no | 窗口/window | 售票日/sale_date |
    候选金额1/candidate1 [| 候选金额2/candidate2 | 候选金额3/candidate3]

纸面合计 CSV（按窗口、按售票日各一份）：
    窗口/window | 合计/total
    售票日/sale_date | 合计/total
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

MAX_ENTRIES = 24
MIN_AMOUNT = 0
MAX_AMOUNT = 1_000_000

TICKET_ALIASES = ("票号", "ticket_no", "ticket", "no")
WINDOW_ALIASES = ("窗口", "window", "counter")
DATE_ALIASES = ("售票日", "sale_date", "date", "selldate")
CANDIDATE_ALIASES = (
    "候选金额1", "候选金额2", "候选金额3",
    "candidate1", "candidate2", "candidate3",
    "candidate_1", "candidate_2", "candidate_3",
    "候选1", "候选2", "候选3",
)
GROUP_ALIASES = WINDOW_ALIASES + DATE_ALIASES
TOTAL_ALIASES = ("合计", "total", "sum", "amount")


class ParseError(ValueError):
    """CSV 内容不满足入仓校对要求。"""


@dataclass(frozen=True)
class EntryRow:
    ticket_no: str
    window: str
    sale_date: str
    candidates: tuple[int, ...]


@dataclass(frozen=True)
class TotalRow:
    group_key: str
    total: int


@dataclass(frozen=True)
class ParsedBatch:
    entries: tuple[EntryRow, ...]
    window_totals: tuple[TotalRow, ...]
    date_totals: tuple[TotalRow, ...]


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8-sig")
    return raw.lstrip("﻿")


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "")


def _match_header(header: str, aliases: tuple[str, ...]) -> bool:
    h = _normalize_header(header)
    return any(h == _normalize_header(a) for a in aliases)


def _read_rows(raw: bytes | str, source: str) -> list[list[str]]:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError(f"{source}为空文件")
    if not any(cell.strip() for cell in header):
        raise ParseError(f"{source}缺少表头")
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    return rows


def _parse_int(cell: str, label: str, line_no: int) -> int:
    s = cell.strip()
    if not s:
        raise ParseError(f"第 {line_no} 行{label}为空")
    if not s.isdigit():
        raise ParseError(f"第 {line_no} 行{label}“{cell.strip()}”不是非负整数（单位：分）")
    value = int(s)
    return value


def parse_entries(raw: bytes | str) -> tuple[EntryRow, ...]:
    rows = _read_rows(raw, "票款清单")
    text = _decode(raw)
    header = next(csv.reader(io.StringIO(text)))

    col_ticket = col_window = col_date = None
    candidate_cols: list[int] = []
    for idx, name in enumerate(header):
        if _match_header(name, TICKET_ALIASES):
            col_ticket = idx
        elif _match_header(name, WINDOW_ALIASES):
            col_window = idx
        elif _match_header(name, DATE_ALIASES):
            col_date = idx
        elif _match_header(name, CANDIDATE_ALIASES):
            candidate_cols.append(idx)
    missing = []
    if col_ticket is None:
        missing.append("票号")
    if col_window is None:
        missing.append("窗口")
    if col_date is None:
        missing.append("售票日")
    if missing:
        raise ParseError("票款清单缺少表头列：" + "、".join(missing))
    candidate_cols.sort()
    if not candidate_cols:
        raise ParseError("票款清单缺少候选金额列（候选金额1～候选金额3）")

    entries: list[EntryRow] = []
    seen_tickets: set[str] = set()
    for offset, row in enumerate(rows):
        line_no = offset + 2  # 含表头行

        def cell(col: int) -> str:
            return row[col].strip() if col < len(row) else ""

        ticket = cell(col_ticket)
        window = cell(col_window)
        sale_date = cell(col_date)
        if not ticket:
            raise ParseError(f"第 {line_no} 行票号为空")
        if not window:
            raise ParseError(f"第 {line_no} 行窗口为空")
        if not sale_date:
            raise ParseError(f"第 {line_no} 行售票日为空")
        if ticket in seen_tickets:
            raise ParseError(f"票号“{ticket}”重复出现")
        seen_tickets.add(ticket)

        amounts: list[int] = []
        for c_idx, col in enumerate(candidate_cols):
            raw_val = cell(col)
            if not raw_val:
                continue
            value = _parse_int(raw_val, f"候选金额{c_idx + 1}", line_no)
            if not MIN_AMOUNT <= value <= MAX_AMOUNT:
                raise ParseError(
                    f"第 {line_no} 行候选金额“{value}”超出允许范围"
                    f"（{MIN_AMOUNT}～{MAX_AMOUNT} 分）"
                )
            amounts.append(value)
        if not amounts:
            raise ParseError(f"第 {line_no} 行至少需要 1 个候选金额")
        if len(amounts) > 3:
            raise ParseError(f"第 {line_no} 行候选金额超过 3 个")
        unique = tuple(sorted(set(amounts)))
        entries.append(EntryRow(ticket, window, sale_date, unique))

    if not entries:
        raise ParseError("票款清单没有任何票款记录")
    if len(entries) > MAX_ENTRIES:
        raise ParseError(f"每批最多 {MAX_ENTRIES} 笔，当前 {len(entries)} 笔")
    return tuple(entries)


def _parse_totals(
    raw: bytes | str, source: str, key_aliases: tuple[str, ...], key_label: str
) -> tuple[TotalRow, ...]:
    rows = _read_rows(raw, source)
    text = _decode(raw)
    header = next(csv.reader(io.StringIO(text)))

    col_key = col_total = None
    for idx, name in enumerate(header):
        if _match_header(name, key_aliases):
            col_key = idx
        elif _match_header(name, TOTAL_ALIASES):
            col_total = idx
    if col_key is None:
        raise ParseError(f"{source}缺少“{key_label}”列")
    if col_total is None:
        raise ParseError(f"{source}缺少“合计”列")

    result: list[TotalRow] = []
    seen: set[str] = set()
    for offset, row in enumerate(rows):
        line_no = offset + 2
        key = row[col_key].strip() if col_key < len(row) else ""
        if not key:
            raise ParseError(f"{source}第 {line_no} 行{key_label}为空")
        if key in seen:
            raise ParseError(f"{source}中{key_label}“{key}”重复")
        seen.add(key)
        raw_total = row[col_total] if col_total < len(row) else ""
        total = _parse_int(raw_total, "合计", line_no)
        if total < 0:
            raise ParseError(f"{source}第 {line_no} 行合计不能为负")
        result.append(TotalRow(key, total))
    if not result:
        raise ParseError(f"{source}没有任何合计记录")
    return tuple(result)


def parse_window_totals(raw: bytes | str) -> tuple[TotalRow, ...]:
    return _parse_totals(raw, "窗口合计表", WINDOW_ALIASES, "窗口")


def parse_date_totals(raw: bytes | str) -> tuple[TotalRow, ...]:
    return _parse_totals(raw, "售票日合计表", DATE_ALIASES, "售票日")


def validate_batch(
    entries: tuple[EntryRow, ...],
    window_totals: tuple[TotalRow, ...],
    date_totals: tuple[TotalRow, ...],
) -> None:
    """交叉校验：分组覆盖、合计范围、两套总额一致。"""
    window_map = {r.group_key: r.total for r in window_totals}
    date_map = {r.group_key: r.total for r in date_totals}

    entry_windows = {e.window for e in entries}
    entry_dates = {e.sale_date for e in entries}

    unknown_w = entry_windows - set(window_map)
    if unknown_w:
        raise ParseError("窗口合计表缺少窗口：" + "、".join(sorted(unknown_w)))
    unknown_d = entry_dates - set(date_map)
    if unknown_d:
        raise ParseError("售票日合计表缺少售票日：" + "、".join(sorted(unknown_d)))
    extra_w = set(window_map) - entry_windows
    if extra_w:
        raise ParseError("窗口合计表包含票款清单中不存在的窗口：" + "、".join(sorted(extra_w)))
    extra_d = set(date_map) - entry_dates
    if extra_d:
        raise ParseError("售票日合计表包含票款清单中不存在的售票日：" + "、".join(sorted(extra_d)))

    # 单组合计必须落在该组候选金额可达范围内
    def _check_range(group_map: dict[str, int], key_attr: str, label: str) -> None:
        buckets: dict[str, list[EntryRow]] = {}
        for e in entries:
            buckets.setdefault(getattr(e, key_attr), []).append(e)
        for key, rows in buckets.items():
            lo = sum(min(r.candidates) for r in rows)
            hi = sum(max(r.candidates) for r in rows)
            total = group_map[key]
            if not lo <= total <= hi:
                raise ParseError(
                    f"{label}“{key}”合计 {total} 超出候选金额可达范围（{lo}～{hi}）"
                )

    _check_range(window_map, "window", "窗口")
    _check_range(date_map, "sale_date", "售票日")

    sum_w = sum(r.total for r in window_totals)
    sum_d = sum(r.total for r in date_totals)
    if sum_w != sum_d:
        raise ParseError(
            f"两套合计总额不一致：窗口合计 {sum_w}，售票日合计 {sum_d}"
        )
    if sum_w > MAX_AMOUNT * len(entries):
        raise ParseError("纸面合计总额超过全部票款金额上限")


def parse_batch(
    entries_raw: bytes | str,
    window_raw: bytes | str,
    date_raw: bytes | str,
) -> ParsedBatch:
    entries = parse_entries(entries_raw)
    window_totals = parse_window_totals(window_raw)
    date_totals = parse_date_totals(date_raw)
    validate_batch(entries, window_totals, date_totals)
    return ParsedBatch(entries, window_totals, date_totals)
