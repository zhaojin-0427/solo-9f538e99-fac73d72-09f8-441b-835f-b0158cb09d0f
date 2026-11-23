"""业务编排：把持久化数据喂给求解器，落库确认结果，生成入仓 CSV。

入仓（写入数仓台账）采用乐观并发：

1. 在**不持有写锁**的情况下读取本批确认状态与同票已入仓情况并求解；
2. 用 ``BEGIN IMMEDIATE`` 开启写事务，重新读取状态指纹与求解时比对，
   一致才一次写入本批尚未入仓的票；
3. 不同意（求解期间有人确认/撤销，或同票被其他批次入仓）则回滚，
   按最新状态重新求解，再写入或拒绝。与无关票的入仓不会改变指纹，
   因此不会触发重算。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass

from . import solver as csp
from .db import (
    load_batch,
    load_entries,
    load_groups,
    load_warehouse_by_key,
    row_candidates,
    row_feasible,
    ticket_key,
)
from .parsing import ParsedBatch


class InfeasibleError(Exception):
    """当前确认状态与两套合计无共同可行解。"""


class WarehouseRejected(Exception):
    """本批当前状态不允许入仓（冲突、无解、金额不唯一或并发反复变化）。"""

    def __init__(self, message: str, reasons: tuple[str, ...] | list[str] = ()):
        self.reasons = tuple(reasons)
        full = message if not self.reasons else message + "：" + "；".join(self.reasons)
        super().__init__(full)


@dataclass(frozen=True)
class WarehouseItem:
    entry_id: int
    ticket_no: str
    window: str
    sale_date: str
    amount: int


@dataclass(frozen=True)
class WarehousePlan:
    """一次入仓判定结果：求解所依据的状态 + 待写/已入仓票款。"""

    batch_id: int
    written: tuple[WarehouseItem, ...]
    skipped: tuple[WarehouseItem, ...]

    @property
    def written_total(self) -> int:
        return sum(item.amount for item in self.written)


def _group_layout(conn: sqlite3.Connection, batch_id: int):
    """分组 id 布局：前半段窗口分组、后半段售票日分组。"""
    group_rows = load_groups(conn, batch_id)
    window_keys = [r["group_key"] for r in group_rows if r["group_type"] == "window"]
    date_keys = [r["group_key"] for r in group_rows if r["group_type"] == "date"]
    window_index = {k: i for i, k in enumerate(window_keys)}
    date_offset = len(window_keys)
    date_index = {k: i + date_offset for i, k in enumerate(date_keys)}
    totals = [r["total"] for r in group_rows if r["group_type"] == "window"]
    totals += [r["total"] for r in group_rows if r["group_type"] == "date"]
    return window_index, date_index, totals


def build_problem(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    apply_warehouse: bool = True,
    strict: bool = True,
):
    """从库里组装求解器输入（逐笔校对展示与确认校验使用）。

    已确认金额的笔，候选域收缩为单值（确认值）；未确认笔保留原候选。
    ``apply_warehouse`` 为真时，同票已入仓的笔进一步收缩为已入仓金额，
    使展示的可行金额与入仓判定一致。``strict`` 为假时忽略“同票已入仓金额
    与原候选/确认冲突”，仅用于撤销等需要始终可执行的动作。
    返回 (domains, memberships, totals, entry_rows)，其中分组 id 布局为
    前半段窗口分组、后半段售票日分组。
    """
    entry_rows = load_entries(conn, batch_id)
    window_index, date_index, totals = _group_layout(conn, batch_id)
    warehouse = load_warehouse_by_key(conn) if apply_warehouse else {}

    domains: list[tuple[int, ...]] = []
    memberships: list[tuple[int, int]] = []
    for row in entry_rows:
        confirmed = row["confirmed_amount"]
        candidates = row_candidates(row)
        wh = warehouse.get(
            ticket_key(row["ticket_no"], row["window_key"], row["sale_date"])
        )
        if wh is not None and wh["amount"] in candidates:
            if confirmed is not None and confirmed != wh["amount"]:
                if strict:
                    raise InfeasibleError(
                        f"票号 {row['ticket_no']} 本批确认 {confirmed} 分与同票"
                        f"已入仓金额 {wh['amount']} 分不一致"
                    )
                domains.append(candidates)
            else:
                domains.append((wh["amount"],))
        elif wh is not None:
            if strict:
                raise InfeasibleError(
                    f"票号 {row['ticket_no']} 同票已入仓金额 {wh['amount']} 分"
                    f"不在本批原候选金额 {list(candidates)} 中"
                )
            domains.append((confirmed,) if confirmed is not None else candidates)
        elif confirmed is None:
            domains.append(candidates)
        else:
            if strict and confirmed not in candidates:
                raise InfeasibleError(
                    f"票号 {row['ticket_no']} 当前人工确认 {confirmed} 分"
                    f"不在本批原候选金额 {list(candidates)} 中"
                )
            domains.append((confirmed,))
        memberships.append(
            (window_index[row["window_key"]], date_index[row["sale_date"]])
        )
    return domains, memberships, totals, entry_rows


def recompute_feasible(
    conn: sqlite3.Connection, batch_id: int, *, persist: bool = True
) -> list[tuple[int, ...]]:
    """按当前确认状态重算每笔可行金额，可选择写回 entries.feasible。"""
    domains, memberships, totals, entry_rows = build_problem(conn, batch_id)
    feasible = csp.compute_feasible(domains, memberships, totals)
    if not feasible or any(len(v) == 0 for v in feasible):
        raise InfeasibleError("当前票款候选与两套纸面合计不存在可行解")
    if persist:
        for row, vals in zip(entry_rows, feasible):
            conn.execute(
                "UPDATE entries SET feasible = ? WHERE id = ?",
                (json.dumps(list(vals)), row["id"]),
            )
    return [tuple(v) for v in feasible]


def create_batch(conn: sqlite3.Connection, parsed: ParsedBatch) -> int:
    """整批入库并完成首次可行性计算；无解则抛 InfeasibleError 并不留数据。"""
    cur = conn.execute(
        "INSERT INTO batches (entry_count) VALUES (?)", (len(parsed.entries),)
    )
    batch_id = cur.lastrowid

    window_total_rows = {r.group_key: r.total for r in parsed.window_totals}
    date_total_rows = {r.group_key: r.total for r in parsed.date_totals}
    for key, total in window_total_rows.items():
        conn.execute(
            "INSERT INTO groups (batch_id, group_type, group_key, total) "
            "VALUES (?, 'window', ?, ?)",
            (batch_id, key, total),
        )
    for key, total in date_total_rows.items():
        conn.execute(
            "INSERT INTO groups (batch_id, group_type, group_key, total) "
            "VALUES (?, 'date', ?, ?)",
            (batch_id, key, total),
        )
    for pos, e in enumerate(parsed.entries):
        conn.execute(
            "INSERT INTO entries "
            "(batch_id, position, ticket_no, window_key, sale_date, "
            " candidates, confirmed_amount, feasible) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '[]')",
            (
                batch_id,
                pos,
                e.ticket_no,
                e.window,
                e.sale_date,
                json.dumps(list(e.candidates)),
            ),
        )
    try:
        # 上传阶段先按本批自身的候选与两套合计校验；同票入仓约束在
        # 校对页实时求解与入仓提交时再参与。
        domains, memberships, totals, _ = build_problem(
            conn, batch_id, apply_warehouse=False
        )
        feasible = csp.compute_feasible(domains, memberships, totals)
        if not feasible or any(len(v) == 0 for v in feasible):
            raise InfeasibleError("当前票款候选与两套纸面合计不存在可行解")
        for pos, vals in enumerate(feasible):
            conn.execute(
                "UPDATE entries SET feasible = ? WHERE batch_id = ? AND position = ?",
                (json.dumps(list(vals)), batch_id, pos),
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return batch_id


def confirm_entry(
    conn: sqlite3.Connection, batch_id: int, entry_id: int, amount: int
) -> None:
    """确认单笔金额：与最新已确认金额一起必须仍有解，否则全部回滚。"""
    row = conn.execute(
        "SELECT * FROM entries WHERE id = ? AND batch_id = ?",
        (entry_id, batch_id),
    ).fetchone()
    if row is None:
        raise InfeasibleError("票款记录不存在或不属于当前批次")
    candidates = row_candidates(row)
    if amount not in candidates:
        raise InfeasibleError("所选金额不在该笔票款的候选金额中")

    try:
        conn.execute(
            "UPDATE entries SET confirmed_amount = ? WHERE id = ?",
            (amount, entry_id),
        )
        recompute_feasible(conn, batch_id)
        conn.execute(
            "INSERT INTO confirmations (batch_id, entry_id, amount, action) "
            "VALUES (?, ?, ?, 'confirm')",
            (batch_id, entry_id, amount),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def revoke_entry(conn: sqlite3.Connection, batch_id: int, entry_id: int) -> None:
    """撤销单笔确认，恢复其候选域并重算其余可行金额。"""
    row = conn.execute(
        "SELECT id, confirmed_amount FROM entries WHERE id = ? AND batch_id = ?",
        (entry_id, batch_id),
    ).fetchone()
    if row is None:
        raise InfeasibleError("票款记录不存在或不属于当前批次")
    if row["confirmed_amount"] is None:
        return
    conn.execute(
        "UPDATE entries SET confirmed_amount = NULL WHERE id = ?", (entry_id,)
    )
    conn.execute(
        "INSERT INTO confirmations (batch_id, entry_id, amount, action) "
        "VALUES (?, ?, ?, 'revoke')",
        (batch_id, entry_id, row["confirmed_amount"]),
    )
    # 撤销本身始终允许；即使入仓金额与原候选/其他确认冲突，也先恢复候选域
    try:
        domains, memberships, totals, entry_rows = build_problem(
            conn, batch_id, apply_warehouse=True, strict=False
        )
        feasible = csp.compute_feasible(domains, memberships, totals)
        if not feasible or any(len(v) == 0 for v in feasible):
            raise InfeasibleError("撤销后当前票款候选与两套纸面合计不存在可行解")
        for r, vals in zip(entry_rows, feasible):
            conn.execute(
                "UPDATE entries SET feasible = ? WHERE id = ?",
                (json.dumps(list(vals)), r["id"]),
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def all_singletons(conn: sqlite3.Connection, batch_id: int) -> bool:
    """每笔只剩一个可行金额（含已确认笔）时才允许下载。"""
    rows = load_entries(conn, batch_id)
    return bool(rows) and all(len(row_feasible(r)) == 1 for r in rows)


def live_feasible(
    conn: sqlite3.Connection, batch_id: int
) -> tuple[list[tuple[int, ...]] | None, str | None]:
    """按最新确认与同票入仓状态实时求可行金额，用于展示。

    返回 (可行集合列表, 错误说明)；无法求解时前者为 None。不写库。
    """
    try:
        feasible = recompute_feasible(conn, batch_id, persist=False)
    except (InfeasibleError, csp.SolverError) as exc:
        return None, str(exc)
    return feasible, None


def export_csv(conn: sqlite3.Connection, batch_id: int) -> str:
    """生成入仓 CSV：原票号、窗口、售票日、确定金额（单位：分）。"""
    # 导出前再实时求解一次，防止库内 feasible 过期
    feasible = recompute_feasible(conn, batch_id, persist=False)
    if any(len(v) != 1 for v in feasible):
        raise InfeasibleError("仍有票款存在多个可行金额，暂不能入仓")
    rows = load_entries(conn, batch_id)
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["票号", "窗口", "售票日", "金额"])
    for row, vals in zip(rows, feasible):
        writer.writerow(
            [row["ticket_no"], row["window_key"], row["sale_date"], vals[0]]
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 数仓入仓
# ---------------------------------------------------------------------------


def _state_fingerprint(conn: sqlite3.Connection, batch_id: int):
    """求解所依据的状态：本批每笔的候选/确认 + 同票已入仓金额。

    只纳入与本批票号键相交的入仓记录，因此无关票入仓不会改变指纹。
    """
    rows = load_entries(conn, batch_id)
    confirm_sig = tuple(
        (r["id"], r["candidates"], r["confirmed_amount"]) for r in rows
    )
    wh = load_warehouse_by_key(conn)
    relevant = []
    for r in rows:
        key = ticket_key(r["ticket_no"], r["window_key"], r["sale_date"])
        hit = wh.get(key)
        if hit is not None:
            relevant.append((key, hit["id"], hit["amount"], hit["batch_id"]))
    return confirm_sig, tuple(sorted(relevant))


def _state_changed_since(conn: sqlite3.Connection, batch_id: int, snapshot) -> bool:
    """判断拒绝判定之后状态是否已变化（变化则应按新状态重判）。"""
    if snapshot is None:
        return True
    try:
        conn.execute("BEGIN")
    except sqlite3.OperationalError:
        return True
    try:
        changed = _state_fingerprint(conn, batch_id) != snapshot
    finally:
        conn.rollback()
    return changed


def _plan_warehouse(conn: sqlite3.Connection, batch_id: int) -> WarehousePlan:
    """按当前状态求解并给出入仓计划；不写库，不合规则抛 WarehouseRejected。"""
    entry_rows = load_entries(conn, batch_id)
    if not entry_rows:
        raise WarehouseRejected(f"批次 #{batch_id} 不存在或没有票款记录")

    window_index, date_index, totals = _group_layout(conn, batch_id)
    warehouse = load_warehouse_by_key(conn)

    domains: list[tuple[int, ...]] = []
    memberships: list[tuple[int, int]] = []
    warehouse_rows: list[sqlite3.Row | None] = []
    conflicts: list[str] = []

    for row in entry_rows:
        key = ticket_key(row["ticket_no"], row["window_key"], row["sale_date"])
        wh_row = warehouse.get(key)
        confirmed = row["confirmed_amount"]
        candidates = row_candidates(row)
        label = (
            f"票号 {row['ticket_no']}"
            f"（{row['window_key']} / {row['sale_date']}）"
        )
        if wh_row is not None:
            wh_amount = wh_row["amount"]
            if confirmed is not None and confirmed != wh_amount:
                conflicts.append(
                    f"{label}本批确认 {confirmed} 分，与同票已入仓金额 "
                    f"{wh_amount} 分（批次 #{wh_row['batch_id']}）不一致"
                )
            if wh_amount not in candidates:
                conflicts.append(
                    f"{label}同票已入仓金额 {wh_amount} 分不在本批原候选金额 "
                    f"{list(candidates)} 中"
                )
            domains.append((wh_amount,))
        elif confirmed is not None:
            if confirmed not in candidates:
                conflicts.append(
                    f"{label}当前人工确认 {confirmed} 分不在本批原候选金额 "
                    f"{list(candidates)} 中"
                )
            domains.append((confirmed,))
        else:
            domains.append(candidates)
        memberships.append(
            (window_index[row["window_key"]], date_index[row["sale_date"]])
        )
        warehouse_rows.append(wh_row)

    if conflicts:
        raise WarehouseRejected("同票已入仓金额与本批记录无法同时满足", conflicts)

    feasible = csp.compute_feasible(domains, memberships, totals)
    if not feasible or any(len(v) == 0 for v in feasible):
        fixed = sum(1 for w in warehouse_rows if w is not None)
        reasons = [
            "本批原候选、当前人工确认、两套纸面合计与同票已入仓金额之间"
            "不存在任何同时成立的完整填法"
        ]
        if fixed:
            reasons.append(f"其中 {fixed} 笔已受同票入仓金额约束")
        raise WarehouseRejected("无解，整批不写入", reasons)

    ambiguous = [
        (
            f"票号 {row['ticket_no']}（{row['window_key']} / {row['sale_date']}）"
            f"仍有多个可行金额：" + "、".join(str(v) for v in vals)
        )
        for row, vals in zip(entry_rows, feasible)
        if len(vals) != 1
    ]
    if ambiguous:
        raise WarehouseRejected("金额未唯一，整批不写入", ambiguous)

    written: list[WarehouseItem] = []
    skipped: list[WarehouseItem] = []
    for row, vals, wh_row in zip(entry_rows, feasible, warehouse_rows):
        item = WarehouseItem(
            entry_id=row["id"],
            ticket_no=row["ticket_no"],
            window=row["window_key"],
            sale_date=row["sale_date"],
            amount=vals[0],
        )
        if wh_row is not None:
            skipped.append(item)
        else:
            written.append(item)
    return WarehousePlan(batch_id, tuple(written), tuple(skipped))


def commit_batch_to_warehouse(
    conn: sqlite3.Connection,
    batch_id: int,
    max_attempts: int = 5,
    max_reject_retries: int = 2,
) -> WarehousePlan:
    """求解并在一次写事务中入仓本批尚未入仓的票。

    求解期间不持写锁，确认/撤销及其他批次入仓可以继续。写入前校验状态
    指纹；若变化则按最新状态重新求解。始终无解/不唯一/反复变化时抛
    WarehouseRejected，且不留下任何入仓记录。

    ``max_reject_retries`` 用于吸收“求解期间状态变化导致本次拒绝”的情形：
    状态变了就按新状态再判一次（最多重判两次），避免用旧状态误拒。
    """
    if conn.in_transaction:
        conn.commit()

    rejects = 0
    for _ in range(max_attempts):
        if load_batch(conn, batch_id) is None:
            raise WarehouseRejected(f"批次 #{batch_id} 不存在")
        # 在一个只读快照里同时取得状态指纹与求解计划，避免二者之间状态变化
        # 造成“旧指纹配新计划”的错配。只读事务不阻塞其他写入（WAL 模式）。
        try:
            conn.execute("BEGIN")
        except sqlite3.OperationalError:
            continue
        snapshot = None
        try:
            snapshot = _state_fingerprint(conn, batch_id)
            plan = _plan_warehouse(conn, batch_id)
        except WarehouseRejected as exc:
            conn.rollback()
            if _state_changed_since(conn, batch_id, snapshot) and rejects < max_reject_retries:
                rejects += 1
                continue
            raise
        finally:
            if conn.in_transaction:
                conn.rollback()

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # 另一个写入事务持锁中：等待后按最新状态重判
            continue
        try:
            if _state_fingerprint(conn, batch_id) != snapshot:
                # 求解所依据的确认或同票入仓情况有变：回滚后按新状态重判
                conn.rollback()
                continue
            for item in plan.written:
                conn.execute(
                    "INSERT INTO warehouse_records "
                    "(batch_id, entry_id, ticket_no, window_key, sale_date, amount) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        item.entry_id,
                        item.ticket_no,
                        item.window,
                        item.sale_date,
                        item.amount,
                    ),
                )
            conn.commit()
            return plan
        except sqlite3.IntegrityError:
            # 同票被其他事务抢先入仓；按新状态重判
            conn.rollback()
            continue
        except sqlite3.OperationalError:
            conn.rollback()
            continue
        except Exception:
            conn.rollback()
            raise

    raise WarehouseRejected(
        "入仓期间本批确认或同票入仓状态持续变化，整批未写入，请稍后重试"
    )
