import io

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(database=str(tmp_path / "test.sqlite3"))
    app.config.update(TESTING=True)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def upload_batch(client, entries, window_totals, date_totals):
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


# 两套解的样本：
# 解A (T1..T6) = (1000,900,2300,600,3100,450)
# 解B          = (1200,800,2200,500,3200,450)
ENTRIES = """票号,窗口,售票日,候选金额1,候选金额2,候选金额3
T001,W1,2026-11-01,1000,1200,1500
T002,W1,2026-11-01,800,900
T003,W1,2026-11-02,2200,2300,2500
T004,W2,2026-11-01,500,600,700
T005,W2,2026-11-02,3000,3100,3200
T006,W2,2026-11-02,400,450,500
"""
WINDOW_TOTALS = "窗口,合计\nW1,4200\nW2,4150\n"
DATE_TOTALS = "售票日,合计\n2026-11-01,2500\n2026-11-02,5850\n"


def test_index_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "暂无批次".encode() in resp.data


def test_upload_shows_feasible_candidates_and_disables_infeasible(app, client):
    resp = upload_batch(client, ENTRIES, WINDOW_TOTALS, DATE_TOTALS)
    assert resp.status_code == 200
    body = resp.data.decode()
    # 1500、2500、700、400 在任何可行解中都不出现，应被禁用（划线）
    assert 'value="1500" disabled' in body
    assert 'value="2500" disabled' in body
    assert 'value="700" disabled' in body
    assert 'value="400" disabled' in body
    # 可行候选可选择
    assert 'value="1000"' in body and 'value="1200"' in body
    # 存在多可行金额时下载按钮禁用
    assert "disabled" in body and "必须每笔都只剩一个可行金额" in body

    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries, row_feasible

        conn = get_db()
        rows = load_entries(conn, latest_batch_id(conn))
    feasible_map = {r["ticket_no"]: row_feasible(r) for r in rows}
    assert feasible_map["T001"] == (1000, 1200)
    assert feasible_map["T002"] == (800, 900)
    assert feasible_map["T003"] == (2200, 2300)
    assert feasible_map["T004"] == (500, 600)
    assert feasible_map["T005"] == (3100, 3200)
    assert feasible_map["T006"] == (450,)


def test_confirm_conflicting_value_is_rejected_and_kept(app, client):
    upload_batch(client, ENTRIES, WINDOW_TOTALS, DATE_TOTALS)
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries

        conn = get_db()
        entry_id = next(
            r["id"] for r in load_entries(conn, latest_batch_id(conn))
            if r["ticket_no"] == "T001"
        )
    # 1500 在当前状态下不可行，提交应被拒
    resp = client.post(
        f"/entries/{entry_id}/confirm",
        data={"amount": "1500", "ticket_no": "T001"},
        follow_redirects=True,
    )
    assert "确认冲突".encode() in resp.data
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries

        conn = get_db()
        row = next(
            r for r in load_entries(conn, latest_batch_id(conn))
            if r["ticket_no"] == "T001"
        )
        assert row["confirmed_amount"] is None
        # 确认记录表不应出现该笔确认
        count = conn.execute(
            "SELECT COUNT(*) c FROM confirmations WHERE entry_id = ?", (entry_id,)
        ).fetchone()["c"]
        assert count == 0


def test_confirm_then_propagation_and_revoke(app, client):
    upload_batch(client, ENTRIES, WINDOW_TOTALS, DATE_TOTALS)
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries

        conn = get_db()
        ids = {
            r["ticket_no"]: r["id"]
            for r in load_entries(conn, latest_batch_id(conn))
        }

    # 确认 T001=1000 后，整体被钉在解 A：所有笔应变为唯一可行
    resp = client.post(
        f"/entries/{ids['T001']}/confirm",
        data={"amount": "1000", "ticket_no": "T001"},
        follow_redirects=True,
    )
    assert "已确认".encode() in resp.data
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries, row_feasible

        conn = get_db()
        rows = load_entries(conn, latest_batch_id(conn))
        assert all(len(row_feasible(r)) == 1 for r in rows)
        pinned = {r["ticket_no"]: row_feasible(r)[0] for r in rows}
    assert pinned == {
        "T001": 1000, "T002": 900, "T003": 2300,
        "T004": 600, "T005": 3100, "T006": 450,
    }
    # 此时可以下载，内容为确定金额
    export = client.get("/export")
    assert export.status_code == 200
    text = export.data.decode("utf-8-sig")
    lines = [l for l in text.strip().splitlines()]
    assert lines[0] == "票号,窗口,售票日,金额"
    assert "T001,W1,2026-11-01,1000" in text
    assert "T005,W2,2026-11-02,3100" in text
    assert len(lines) == 7

    # 撤销后恢复多候选
    resp = client.post(
        f"/entries/{ids['T001']}/revoke", follow_redirects=True
    )
    assert "撤销".encode() in resp.data
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries, row_feasible

        conn = get_db()
        rows = load_entries(conn, latest_batch_id(conn))
        t1 = next(r for r in rows if r["ticket_no"] == "T001")
        assert t1["confirmed_amount"] is None
        assert row_feasible(t1) == (1000, 1200)
        action = conn.execute(
            "SELECT action FROM confirmations WHERE entry_id = ? ORDER BY id",
            (ids["T001"],),
        ).fetchall()
        assert [r["action"] for r in action] == ["confirm", "revoke"]


def test_export_blocked_while_ambiguous(client):
    upload_batch(client, ENTRIES, WINDOW_TOTALS, DATE_TOTALS)
    resp = client.get("/export", follow_redirects=True)
    assert "暂不能入仓".encode() in resp.data


def test_infeasible_upload_is_rejected_without_batch(app, client):
    # 窗口合计总额与售票日合计总额不一致，入库被拒
    bad_window = "窗口,合计\nW1,9999\nW2,4150\n"
    resp = upload_batch(client, ENTRIES, bad_window, DATE_TOTALS)
    assert "上传失败".encode() in resp.data
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        assert conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] == 0


def test_missing_file_rejected(client):
    resp = client.post(
        "/upload",
        data={"entries_file": (io.BytesIO(b"x"), "a.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "请同时选择三个 CSV".encode() in resp.data


def test_non_candidate_value_rejected(app, client):
    upload_batch(client, ENTRIES, WINDOW_TOTALS, DATE_TOTALS)
    with app.app_context():
        from app.db import get_db, latest_batch_id, load_entries

        conn = get_db()
        entry_id = next(
            r["id"] for r in load_entries(conn, latest_batch_id(conn))
            if r["ticket_no"] == "T001"
        )
    resp = client.post(
        f"/entries/{entry_id}/confirm",
        data={"amount": "999"},
        follow_redirects=True,
    )
    assert "确认冲突".encode() in resp.data
