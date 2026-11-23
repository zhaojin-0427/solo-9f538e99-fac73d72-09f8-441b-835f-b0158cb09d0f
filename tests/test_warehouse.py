"""数仓入仓：同票去重、累计明细、求解约束与并发状态变化。"""
import io

import pytest

from app import services
from app.services import WarehouseRejected


def upload(client, entries, window_totals, date_totals):
    def data(text):
        return (io.BytesIO(text.encode("utf-8")), "f.csv")

    return client.post(
        "/upload",
        data={
            "entries_file": data(entries),
            "window_file": data(window_totals),
            "date_file": data(date_totals),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


# 第一批：6 笔，两套合计下有两个完整解：
#   解A = (1000,900,2300,600,3100,450)
#   解B = (1200,800,2200,500,3200,450)
# 复核员确认 T001=1000 后整批唯一为解A。
ENTRIES_B1 = """票号,窗口,售票日,候选金额1,候选金额2,候选金额3
T001,W1,2026-11-01,1000,1200,1500
T002,W1,2026-11-01,800,900
T003,W1,2026-11-02,2200,2300,2500
T004,W2,2026-11-01,500,600,700
T005,W2,2026-11-02,3000,3100,3200
T006,W2,2026-11-02,400,450,500
"""
WINDOW_B1 = "窗口,合计\nW1,4200\nW2,4150\n"
DATE_B1 = "售票日,合计\n2026-11-01,2500\n2026-11-02,5850\n"

# 第二批：重扫清单，含同票 T001/T004（同日同窗同票号），另有新票 T007。
# T001/T004 已入仓（1000/600），候选里也包含该金额；第二批自身合计已包含它们。
ENTRIES_B2 = """票号,窗口,售票日,候选金额1,候选金额2
T001,W1,2026-11-01,1000,1200
T004,W2,2026-11-01,500,600
T007,W2,2026-11-02,900,950
"""
WINDOW_B2 = "窗口,合计\nW1,1000\nW2,1550\n"
DATE_B2 = "售票日,合计\n2026-11-01,1600\n2026-11-02,950\n"


def _entry_ids(app, batch_id=None):
    from app.db import get_db, latest_batch_id, load_entries

    with app.app_context():
        conn = get_db()
        bid = batch_id or latest_batch_id(conn)
        return {r["ticket_no"]: r["id"] for r in load_entries(conn, bid)}


def _sql(app, query, params=()):
    from app.db import get_db

    with app.app_context():
        conn = get_db()
        return conn.execute(query, params).fetchall()


def _confirm(client, entry_id, amount, ticket_no="", batch_id=None):
    data = {"amount": str(amount), "ticket_no": ticket_no}
    if batch_id is not None:
        data["batch_id"] = str(batch_id)
    return client.post(
        f"/entries/{entry_id}/confirm",
        data=data,
        follow_redirects=True,
    )


def _commit(client, batch_id):
    return client.post(
        "/warehouse/commit",
        data={"batch_id": str(batch_id)},
        follow_redirects=True,
    )


@pytest.fixture()
def app(tmp_path):
    from app import create_app

    app = create_app(database=str(tmp_path / "test.sqlite3"))
    app.config.update(TESTING=True)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def test_rejects_when_ambiguous_and_writes_nothing(app, client):
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    resp = _commit(client, 1)
    body = resp.data.decode()
    assert "金额未唯一" in body or "多个可行金额" in body
    rows = _sql(app, "SELECT * FROM warehouse_records")
    assert rows == []


def test_two_batches_same_ticket_written_once(app, client):
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")

    resp = _commit(client, 1)
    assert "已入仓 6 笔" in resp.data.decode()
    rows = _sql(
        app,
        "SELECT ticket_no, amount, batch_id FROM warehouse_records ORDER BY ticket_no",
    )
    assert [(r["ticket_no"], r["amount"], r["batch_id"]) for r in rows] == [
        ("T001", 1000, 1),
        ("T002", 900, 1),
        ("T003", 2300, 1),
        ("T004", 600, 1),
        ("T005", 3100, 1),
        ("T006", 450, 1),
    ]
    # 重复入仓同批不应再写
    resp = _commit(client, 1)
    assert (
        "没有需要新入仓" in resp.data.decode()
        or "跳过 6 笔" in resp.data.decode()
    )
    assert len(_sql(app, "SELECT * FROM warehouse_records")) == 6

    # 第二批：T001/T004 已在仓，按已入仓金额约束求解，只写 T007
    upload(client, ENTRIES_B2, WINDOW_B2, DATE_B2)
    # 第二批无确认，也仅剩唯一解 (1000,600,950)
    resp = _commit(client, 2)
    body = resp.data.decode()
    assert "已入仓 1 笔" in body
    rows2 = _sql(
        app,
        "SELECT ticket_no, amount, batch_id FROM warehouse_records "
        "WHERE batch_id = 2 ORDER BY ticket_no",
    )
    assert [(r["ticket_no"], r["amount"], r["batch_id"]) for r in rows2] == [
        ("T007", 950, 2)
    ]
    # 累计明细按售票日：同票只计一次
    rows_d1 = _sql(
        app,
        "SELECT ticket_no, amount, batch_id FROM warehouse_records "
        "WHERE sale_date = '2026-11-01' ORDER BY ticket_no",
    )
    assert [(r["ticket_no"], r["amount"], r["batch_id"]) for r in rows_d1] == [
        ("T001", 1000, 1),
        ("T002", 900, 1),
        ("T004", 600, 1),
    ]


def test_warehouse_page_lists_daily_totals(app, client):
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)

    resp = client.get("/?date=2026-11-01")
    body = resp.data.decode()
    assert "入仓累计明细" in body
    assert "共入仓" in body
    assert "T001" in body and "T004" in body
    # 11-01 三笔合计 1000+900+600 = 2500
    assert "2500" in body


def test_warehouse_solve_uses_warehoused_amount(app, client):
    """同票已入仓金额参与求解：第二批若把 T001 拖向 1200 则无解。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)

    # 第二批纸面只允许 T001=1200（与已入仓 1000 冲突）
    bad_entries = """票号,窗口,售票日,候选金额1,候选金额2
T001,W1,2026-11-01,1000,1200
T007,W2,2026-11-02,900,950
"""
    upload(client, bad_entries, "窗口,合计\nW1,1200\nW2,950\n", "售票日,合计\n2026-11-01,1200\n2026-11-02,950\n")
    resp = _commit(client, 2)
    body = resp.data.decode()
    assert "入仓被拒绝" in body
    assert "整批未写入" in body
    assert len(_sql(app, "SELECT * FROM warehouse_records WHERE batch_id = 2")) == 0


def test_confirmation_conflicting_with_warehouse_rejected(app, client):
    """同票已入仓后，本批确认成另一个金额必须被拒绝。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)
    upload(client, ENTRIES_B2, WINDOW_B2, DATE_B2)
    ids2 = _entry_ids(app)
    # 第二批里 T001 若确认 1200 与仓内 1000 冲突，服务会拒绝
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        try:
            services.confirm_entry(conn, 2, ids2["T001"], 1200)
        except services.InfeasibleError:
            pass
        else:  # pragma: no cover
            raise AssertionError("应拒绝与已入仓金额冲突的确认")
    row = _sql(app, "SELECT confirmed_amount FROM entries WHERE id = ?", (ids2["T001"],))
    assert row[0]["confirmed_amount"] is None


def test_full_rescan_of_same_batch_writes_nothing(app, client):
    """整批重扫（票号完全相同）入仓时全部视为同票，不重复写入。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)
    assert len(_sql(app, "SELECT * FROM warehouse_records")) == 6

    # 同清单再上传一批（不确认，靠同票入仓金额钉住唯一解）
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    resp = _commit(client, 2)
    body = resp.data.decode()
    assert "没有需要新入仓" in body
    assert "6 笔同票已在仓内" in body
    assert len(_sql(app, "SELECT * FROM warehouse_records")) == 6
    # 累计明细中每张票仍只有一条
    rows = _sql(
        app,
        "SELECT ticket_no, COUNT(*) c FROM warehouse_records "
        "GROUP BY ticket_no HAVING c > 1",
    )
    assert rows == []


def test_revoke_after_warehouse_keeps_records(app, client):
    """入仓后撤销确认：原入仓记录保留，且同票仍受已入仓金额约束。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)

    resp = client.post(f"/entries/{ids1['T001']}/revoke", follow_redirects=True)
    assert "已撤销".encode() in resp.data
    record = _sql(
        app, "SELECT amount FROM warehouse_records WHERE ticket_no = 'T001'"
    )
    assert record[0]["amount"] == 1000
    assert len(_sql(app, "SELECT * FROM warehouse_records")) == 6
    # 页面仍标出该票已入仓
    resp = client.get("/?batch_id=1")
    assert "已入仓" in resp.data.decode()


def test_unrelated_warehouse_does_not_trigger_replan(app, client, monkeypatch):
    """无关票入仓不应让求解依据失效。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")

    import app.services as svc

    calls = {"n": 0}
    real = svc._state_fingerprint

    def counted(conn, batch_id):
        calls["n"] += 1
        return real(conn, batch_id)

    monkeypatch.setattr(svc, "_state_fingerprint", counted)
    plan = None
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        plan = svc.commit_batch_to_warehouse(conn, 1)
    assert len(plan.written) == 6
    # 快照一次 + 提交前校验一次，未发生重判
    assert calls["n"] == 2


def test_unrelated_batch_can_commit_during_solve(app, client, monkeypatch):
    """求解只持读事务：期间不重叠批次可入仓，且不触发本批重算。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")

    import app.services as svc

    real = svc._plan_warehouse
    state = {"first": True, "calls": 0}

    def during_solve(conn, batch_id):
        state["calls"] += 1
        if state["first"]:
            state["first"] = False
            other = _other_conn(app)
            try:
                # 模拟另一批把与本批完全无关的票写入数仓
                other.execute(
                    "INSERT INTO warehouse_records "
                    "(batch_id, entry_id, ticket_no, window_key, sale_date, amount) "
                    "SELECT batch_id, id, 'Z999', 'W9', '2026-10-01', 777 "
                    "FROM entries WHERE batch_id = ? ORDER BY position LIMIT 1",
                    (batch_id,),
                )
                other.commit()
            finally:
                other.close()
        return real(conn, batch_id)

    monkeypatch.setattr(svc, "_plan_warehouse", during_solve)
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        plan = svc.commit_batch_to_warehouse(conn, 1)
    assert len(plan.written) == 6
    # 无关入仓不改变指纹：只求解一次，没有重判
    assert state["calls"] == 1
    rows = _sql(app, "SELECT ticket_no FROM warehouse_records ORDER BY ticket_no")
    assert "Z999" in [r["ticket_no"] for r in rows]
    assert len(rows) == 7


def _other_conn(app):
    import sqlite3

    conn = sqlite3.connect(app.config["DATABASE"], timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def test_state_change_during_solve_replans_and_writes(app, client, monkeypatch):
    """求解快照后被外部事务确认：按新状态重判后写入，而不是拿旧计划写。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    import app.services as svc

    real_fp = svc._state_fingerprint
    calls = {"n": 0}

    def hijack(conn, batch_id):
        fp = real_fp(conn, batch_id)
        calls["n"] += 1
        if calls["n"] == 1:
            other = _other_conn(app)
            try:
                other.execute(
                    "UPDATE entries SET confirmed_amount = 1000 "
                    "WHERE ticket_no = 'T001' AND batch_id = ?",
                    (batch_id,),
                )
                other.commit()
            finally:
                other.close()
        return fp

    monkeypatch.setattr(svc, "_state_fingerprint", hijack)
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        plan = svc.commit_batch_to_warehouse(conn, 1)
    # 第一次快照时未确认 -> 不唯一被拒；重判后按 T001=1000 唯一解写入
    assert len(plan.written) == 6
    rows = _sql(app, "SELECT amount FROM warehouse_records WHERE ticket_no='T001'")
    assert [r["amount"] for r in rows] == [1000]


def test_always_rejected_state_writes_nothing(app, client, monkeypatch):
    """状态持续无解：重判后仍拒绝，整批不留记录。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    import app.services as svc

    real = svc._plan_warehouse
    state = {"first": True}

    def hijack(conn, batch_id):
        if state["first"]:
            state["first"] = False
            conn.execute(
                "UPDATE entries SET confirmed_amount = 99999 "
                "WHERE ticket_no = 'T006' AND batch_id = ?",
                (batch_id,),
            )
            conn.commit()
        return real(conn, batch_id)

    monkeypatch.setattr(svc, "_plan_warehouse", hijack)
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        with pytest.raises(WarehouseRejected):
            svc.commit_batch_to_warehouse(conn, 1)
    assert len(_sql(app, "SELECT * FROM warehouse_records")) == 0

def test_plan_skips_already_warehoused_but_keeps_records(app, client):
    """已入仓金额与原校对记录都保留。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    ids1 = _entry_ids(app)
    _confirm(client, ids1["T001"], 1000, "T001")
    _commit(client, 1)
    # 原校对记录（确认记录与可行域）仍在
    assert len(_sql(app, "SELECT * FROM confirmations")) == 1
    rows = _sql(app, "SELECT feasible, confirmed_amount FROM entries WHERE ticket_no='T001'")
    assert rows[0]["confirmed_amount"] == 1000
    assert rows[0]["feasible"] == "[1000]"


def test_commit_nonexistent_batch_rejected(client):
    resp = _commit(client, 42)
    assert "入仓被拒绝" in resp.data.decode()


def test_commit_accepts_any_uploaded_batch(app, client):
    """复核员可选任一已上传批次提交，不只是最新批。"""
    upload(client, ENTRIES_B1, WINDOW_B1, DATE_B1)
    upload(client, ENTRIES_B2, WINDOW_B2, DATE_B2)
    ids1 = _entry_ids(app, 1)
    _confirm(client, ids1["T001"], 1000, "T001", batch_id=1)
    resp = _commit(client, 1)
    assert "已入仓 6 笔" in resp.data.decode()
