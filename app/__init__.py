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
from .db import get_db, latest_batch_id, load_batch, load_entries, load_groups
from .db import row_candidates, row_feasible
from .parsing import ParseError
from .solver import SearchLimitExceeded, SolverError
from .services import InfeasibleError


def create_app(database: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database or os.environ.get(
        "RECON_DB_PATH", os.path.join(app.instance_path, "recon.sqlite3")
    )
    app.config["SECRET_KEY"] = os.environ.get("RECON_SECRET_KEY", "dev-secret-key")
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    app.teardown_appcontext(db_mod.close_db)

    with app.app_context():
        db_mod.init_db()

    def _current_batch_view():
        conn = get_db()
        batch_id = latest_batch_id(conn)
        if batch_id is None:
            return None
        batch = load_batch(conn, batch_id)
        entries = load_entries(conn, batch_id)
        groups = load_groups(conn, batch_id)
        view_entries = []
        for row in entries:
            view_entries.append(
                {
                    "id": row["id"],
                    "position": row["position"],
                    "ticket_no": row["ticket_no"],
                    "window": row["window_key"],
                    "sale_date": row["sale_date"],
                    "candidates": row_candidates(row),
                    "feasible": row_feasible(row),
                    "confirmed": row["confirmed_amount"],
                }
            )
        window_totals = [g for g in groups if g["group_type"] == "window"]
        date_totals = [g for g in groups if g["group_type"] == "date"]
        return {
            "batch": batch,
            "entries": view_entries,
            "window_totals": window_totals,
            "date_totals": date_totals,
            "all_singletons": services.all_singletons(conn, batch_id),
            "confirmed_count": sum(1 for e in view_entries if e["confirmed"] is not None),
        }

    @app.get("/")
    def index():
        return render_template("index.html", view=_current_batch_view())

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
        return redirect(url_for("index"))

    @app.post("/entries/<int:entry_id>/confirm")
    def confirm(entry_id: int):
        raw = request.form.get("amount", "")
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            flash("提交失败：请选择一个候选金额", "error")
            return redirect(url_for("index"))
        conn = get_db()
        batch_id = latest_batch_id(conn)
        if batch_id is None:
            flash("提交失败：当前没有批次", "error")
            return redirect(url_for("index"))
        try:
            services.confirm_entry(conn, batch_id, entry_id, amount)
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"确认冲突，已保留原有确认记录：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"票款 {request.form.get('ticket_no', '')} 已确认 {amount} 分，剩余候选已更新。", "ok")
        return redirect(url_for("index"))

    @app.post("/entries/<int:entry_id>/revoke")
    def revoke(entry_id: int):
        conn = get_db()
        batch_id = latest_batch_id(conn)
        if batch_id is None:
            flash("操作失败：当前没有批次", "error")
            return redirect(url_for("index"))
        try:
            services.revoke_entry(conn, batch_id, entry_id)
        except (InfeasibleError, SolverError, SearchLimitExceeded) as exc:
            flash(f"撤销失败：{exc}", "error")
            return redirect(url_for("index"))
        flash("已撤销该笔确认并重新计算可行金额。", "ok")
        return redirect(url_for("index"))

    @app.get("/export")
    def export():
        conn = get_db()
        batch_id = latest_batch_id(conn)
        if batch_id is None:
            flash("当前没有批次，无法下载", "error")
            return redirect(url_for("index"))
        try:
            content = services.export_csv(conn, batch_id)
        except InfeasibleError as exc:
            flash(f"暂不能入仓：{exc}", "error")
            return redirect(url_for("index"))
        filename = f"warehouse_batch_{batch_id}.csv"
        return Response(
            content,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return app
