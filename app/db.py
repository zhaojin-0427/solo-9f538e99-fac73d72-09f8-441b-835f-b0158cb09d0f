"""SQLite 持久化：批次、票款、分组合计、确认记录。"""
from __future__ import annotations

import json
import sqlite3
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    entry_count     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    group_type      TEXT NOT NULL CHECK (group_type IN ('window', 'date')),
    group_key       TEXT NOT NULL,
    total           INTEGER NOT NULL,
    UNIQUE (batch_id, group_type, group_key)
);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    position        INTEGER NOT NULL,
    ticket_no       TEXT NOT NULL,
    window_key      TEXT NOT NULL,
    sale_date       TEXT NOT NULL,
    candidates      TEXT NOT NULL,
    confirmed_amount INTEGER,
    feasible        TEXT NOT NULL,
    UNIQUE (batch_id, position)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    entry_id        INTEGER NOT NULL REFERENCES entries(id),
    amount          INTEGER NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('confirm', 'revoke')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 数仓入仓台账：只追加、从不修改或删除。
-- (票号, 窗口, 售票日) 相同即同一张票，唯一约束保证重扫票只入仓一次。
CREATE TABLE IF NOT EXISTS warehouse_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    entry_id        INTEGER NOT NULL REFERENCES entries(id),
    ticket_no       TEXT NOT NULL,
    window_key      TEXT NOT NULL,
    sale_date       TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ticket_no, window_key, sale_date)
);

CREATE INDEX IF NOT EXISTS idx_warehouse_sale_date
    ON warehouse_records (sale_date);
CREATE INDEX IF NOT EXISTS idx_warehouse_batch
    ON warehouse_records (batch_id);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        # WAL：读请求不阻塞写入，入仓求解在事务外进行时不影响确认/撤销。
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


def latest_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM batches").fetchone()
    return row["id"] if row and row["id"] is not None else None


def load_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()


def load_entries(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE batch_id = ? ORDER BY position", (batch_id,)
    ).fetchall()


def load_groups(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM groups WHERE batch_id = ? ORDER BY group_type, group_key",
        (batch_id,),
    ).fetchall()


def row_candidates(row: sqlite3.Row) -> tuple[int, ...]:
    return tuple(json.loads(row["candidates"]))


def row_feasible(row: sqlite3.Row) -> tuple[int, ...]:
    return tuple(json.loads(row["feasible"]))


def load_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches ORDER BY id DESC"
    ).fetchall()


def batch_id_for_entry(conn: sqlite3.Connection, entry_id: int) -> int | None:
    row = conn.execute(
        "SELECT batch_id FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    return row["batch_id"] if row is not None else None


def ticket_key(ticket_no: str, window_key: str, sale_date: str) -> tuple[str, str, str]:
    """同一天、同一窗口下票号相同的记录视为同一张票。"""
    return (ticket_no, window_key, sale_date)


def load_warehouse_records(
    conn: sqlite3.Connection, batch_id: int | None = None
) -> list[sqlite3.Row]:
    if batch_id is None:
        return conn.execute(
            "SELECT * FROM warehouse_records ORDER BY sale_date, window_key, ticket_no"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM warehouse_records WHERE batch_id = ? "
        "ORDER BY sale_date, window_key, ticket_no",
        (batch_id,),
    ).fetchall()


def load_warehouse_by_key(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str], sqlite3.Row]:
    """所有已入仓记录，按 (票号, 窗口, 售票日) 索引。"""
    return {
        ticket_key(r["ticket_no"], r["window_key"], r["sale_date"]): r
        for r in load_warehouse_records(conn)
    }


def load_warehouse_by_date(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """按售票日汇总已入仓明细，用于复核页核对。"""
    result: dict[str, list[sqlite3.Row]] = {}
    for row in load_warehouse_records(conn):
        result.setdefault(row["sale_date"], []).append(row)
    return result
