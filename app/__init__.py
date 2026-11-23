"""Flask 入仓校对应用。"""
from __future__ import annotations

import os

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db as db_mod
from . import parsing, services
from .db import (
    batch_id_for_entry,
    get_db,
    latest_batch_id,
    load_batch,
    load_batches,
    load_entries,
    load_groups,
    load_warehouse_by_date,
    load_warehouse_by_key,
    row_candidates,
    row_feasible,
    ticket_key,
)
from .parsing import ParseError
from .solver import SearchLimitExceeded, SolverError
from .services import InfeasibleError, WarehouseRejected


def create_app(database: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database or os.environ.get(
        "RECON_DB_PATH", os.path.join(app.instance_path, "recon.sqlite3")
    )
    app.config["SECRET_KEY"] = os.environ.get("RECON_SECRET_KEY", "dev-secret-key")
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    db_dir = os.path.dirname(app.config["DATABASE"]) or "."
    os.makedirs(db_dir, exist_ok=True)

    app.teardown_appcontext(db_mod.close_db)

    with app.app_context():
        db_mod.init_db()

    def _resolve_batch_id(conn) -> int | None:
        raw = request.values.get("batch_id")
        if raw:
            try:
                batch_id = int(raw)
            except ValueError:
                return latest_batch_id(conn)
            if load_batch(conn, batch_id) is not None:
                return batch_id
        return latest_batch_id(conn)

    def _current_batch_view(batch_id: int | None):
        conn = get_db()
        if batch_id is None:
            return None, None
        batch = load_batch(conn, batch_id)
        if batch is None:
            return None, None
        entries = load_entries(conn, batch_id)
        groups = load_groups(conn, batch_id)
        warehouse = load_warehouse_by_key(conn)
        try:
            live, live_error = services.live_feasible(conn, batch_id)
        except Exception as exc:  # 展示层兜底：绝不因求解异常整页 500
            live, live_error = None, str(exc)
        view_entries = []
        for idx, row in enumerate(entries):
            key = ticket_key(row["ticket_no"], row["window_key"], row["sale_date"])
            wh = warehouse.get(key)
            if live is not None:
                feasible = live[idx]
            elif live_error is not None:
                # 当前状态无解：不展示可能已经过期的旧可行值
                feasible = ()
            else:
                feasible = row_feasible(row)
            view_entries.append(
                {
                    "id": row["id"],
                    "position": row["position"],
                    "ticket_no": row["ticket_no"],
                    "window": row["window_key"],
                    "sale_date": row["sale_date"],
                    "candidates": row_candidates(row),
                    "feasible": feasible,
                    "confirmed": row["confirmed_amount"],
                    "warehoused": wh["amount"] if wh is not None else None,
                    "warehouse_batch": wh["batch_id"] if wh is not None else None,
                }
            )
        window_totals = [g for g in groups if g["group_type"] == "window"]
        date_totals = [g for g in groups if g["group_type"] == "date"]
        all_singletons = (
            live_error is None
            and len(view_entries) > 0
            and all(len(e["feasible"]) == 1 for e in view_entries)
        )
        return (
            {
                "batch": batch,
                "entries": view_entries,
                "window_totals": window_totals,
                "date_totals": date_totals,
                "all_singletons": all_singletons,
                "confirmed_count": sum(
                    1 for e in view_entries if e["confirmed"] is not None
                ),
                "warehoused_count": sum(
                    1 for e in view_entries if e["warehoused"] is not None
                ),
                "pending_warehouse": sum(
                    1 for e in view_entries if e["warehoused"] is None
                ),
                "solve_error": live_error,
            },
            batch_id,
        )

    def _warehouse_view(sale_date: str | None):
        conn = get_db()
        by_date = load_warehouse_by_date(conn)
        selected = sale_date or (next(iter(by_date)) if by_date else None)
        records = by_date.get(selected, []) if selected else []
        rows = [
            {
                "ticket_no": r["ticket_no"],
                "window": r["window_key"],
                "sale_date": r["sale_date"],
                "amount": r["amount"],
                "batch_id": r["batch_id"],
                "created_at": r["created_at"],
            }
            for r in records
        ]
        return {
            "dates": sorted(by_date),
            "selected_date": selected,
            "records": rows,
            "total": sum(r["amount"] for r in rows),
            "record_count": len(rows),
            "daily_summary": [
                {
                    "sale_date": d,
                    "count": len(items),
                    "total": sum(r["amount"] for r in items),
                }
                for d, items in sorted(by_date.items())
            ],
        }

    def _render_index(**extra):
        conn = get_db()
        batch_id = _resolve_batch_id(conn)
        view, _ = _current_batch_view(batch_id)
        context = {
            "view": view,
            "selected_batch_id": batch_id,
            "batches": load_batches(conn),
            "warehouse": _warehouse_view(request.args.get("date")),
        }
        context.update(extra)
        return render_template("index.html", **context)

    @app.get("/")
    def index():
        return _render_index()

    @app.post("/upload")
    def upload():
        files = {
            "entries": request.files.get("entries_file"),
            "window": request.files.get("window_file"),
            "date": request.files.get("date_file"),
        }
        missing = [name for name, f in files.items() if f is None or f.filename == ""]
        if missing:
            labels = {"entries": "票款清单", "window": "窗口合计表", "date": "售票日合计表"}
            flash("请同时选择三个 CSV 文件，缺少：" + "、".join(labels[m] for m in missing), "error")
            return redirect(url_for("index"))
        try:
            parsed = parsing.parse_batch(
                files["entries"].read(),
                files["window"].read(),
                files["date"].read(),
            )
            conn = get_db()
            batch_id = services.create_batch(conn, parsed)
        except (ParseError, InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"第 {batch_id} 批已入库，共 {len(parsed.entries)} 笔，已算出各笔可行金额。", "ok")
        return redirect(url_for("index", batch_id=batch_id))

    @app.post("/entries/<int:entry_id>/confirm")
    def confirm(entry_id: int):
        raw = request.form.get("amount", "")
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            flash("提交失败：请选择一个候选金额", "error")
            return redirect(url_for("index"))
        conn = get_db()
        batch_id = _resolve_batch_id(conn)
        if batch_id is None or batch_id_for_entry(conn, entry_id) != batch_id:
            flash("提交失败：该票款不属于所查看的批次", "error")
            return redirect(url_for("index", batch_id=batch_id) if batch_id else url_for("index"))
        try:
            services.confirm_entry(conn, batch_id, entry_id, amount)
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"确认冲突，已保留原有确认记录：{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        flash(f"票款 {request.form.get('ticket_no', '')} 已确认 {amount} 分，剩余候选已更新。", "ok")
        return redirect(url_for("index", batch_id=batch_id))

    @app.post("/entries/<int:entry_id>/revoke")
    def revoke(entry_id: int):
        conn = get_db()
        batch_id = _resolve_batch_id(conn)
        if batch_id is None or batch_id_for_entry(conn, entry_id) != batch_id:
            flash("操作失败：该票款不属于所查看的批次", "error")
            return redirect(url_for("index", batch_id=batch_id) if batch_id else url_for("index"))
        try:
            services.revoke_entry(conn, batch_id, entry_id)
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"撤销失败：{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        flash("已撤销该笔确认并重新计算可行金额。", "ok")
        return redirect(url_for("index", batch_id=batch_id))

    @app.get("/export")
    def export():
        conn = get_db()
        batch_id = _resolve_batch_id(conn)
        if batch_id is None:
            flash("当前没有批次，无法下载", "error")
            return redirect(url_for("index"))
        try:
            content = services.export_csv(conn, batch_id)
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"暂不能入仓：{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        filename = f"warehouse_batch_{batch_id}.csv"
        return Response(
            content,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.post("/warehouse/commit")
    def warehouse_commit():
        conn = get_db()
        raw = request.form.get("batch_id", "")
        try:
            batch_id = int(raw)
        except (TypeError, ValueError):
            flash("入仓失败：请选择要入仓的批次", "error")
            return redirect(url_for("index"))
        try:
            plan = services.commit_batch_to_warehouse(conn, batch_id)
        except WarehouseRejected as exc:
            flash(f"第 {batch_id} 批入仓被拒绝，整批未写入。{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"第 {batch_id} 批入仓失败，整批未写入：{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        except Exception as exc:  # 兜底：任何异常都不写入半批
            flash(f"第 {batch_id} 批入仓失败，整批未写入：{exc}", "error")
            return redirect(url_for("index", batch_id=batch_id))
        if plan.written:
            flash(
                f"第 {batch_id} 批已入仓 {len(plan.written)} 笔，"
                f"合计 {plan.written_total} 分；"
                f"同票已入仓而跳过 {len(plan.skipped)} 笔。",
                "ok",
            )
        else:
            flash(
                f"第 {batch_id} 批没有需要新入仓的票款"
                f"（{len(plan.skipped)} 笔同票已在仓内）。",
                "ok",
            )
        return redirect(url_for("index", batch_id=batch_id))

    return app
