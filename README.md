# 旧票款入仓校对（Flask + SQLite）

扫描得到的旧票款清单存在 OCR 金额识别不准的问题：每笔票款保留 1～3 个候选
金额，需要结合按窗口、按售票日的两套纸面合计，逐笔校对出唯一正确金额后才能
入仓。本应用用约束求解（CSP）在网页上逐笔列出**当前仍可行**的候选金额，
复核员确认一笔即重算一次，全部唯一后导出入仓 CSV。

## 功能与约束

- 每批最多 24 笔；金额均为 `0～1 000 000` 的整数分。
- 上传三个 UTF-8 CSV：
  - 票款清单：`票号,窗口,售票日,候选金额1[,候选金额2,候选金额3]`
    （兼容英文表头 `ticket_no,window,sale_date,candidate1..3`）
  - 窗口合计：`窗口,合计`
  - 售票日合计：`售票日,合计`
- 求解器不枚举整批填法，只通过带记忆化的回溯搜索 + 分组后缀可达集合剪枝，
  判定每个候选值是否出现在**至少一个同时满足两套合计的完整解**中。
- 复核员提交一笔确认时，系统把该笔候选域收缩为确认值并与全部最新已确认金额
  一起重新求解；无解则整笔事务回滚，提示冲突并保留原有确认。
- 只有每笔都只剩一个可行金额时才允许下载入仓 CSV
  （列：票号、窗口、售票日、金额，金额单位：分）。
- 所有确认/撤销动作写入 `confirmations` 审计表。

## 目录

```
app/            Flask 应用（solver 求解器 / parsing 校验 / db 持久化 / services 编排）
tests/          pytest 用例（求解器、CSV 校验、HTTP 全流程）
sample_data/    可直接上传的示例 CSV
wsgi.py         gunicorn 入口
Dockerfile / docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `recon-data`（容器内 `/data/recon.sqlite3`）。
生产环境请通过环境变量 `RECON_SECRET_KEY` 覆盖默认密钥。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py            # http://localhost:8000
```

## 自测

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
```

## 关键设计说明

- **分组后缀可达集合**：对每个窗口/售票日分组，预计算“剩余 k 个成员能凑出的
  金额集合”，DFS 赋值后用 `目标-已用` 是否在集合内做前向检查；集合过大时
  自动退化为 min/max 剪枝，并有搜索状态数上限保护。
- **确认即收缩域**：已确认笔的候选域变为单值，重算结果写回 `entries.feasible`；
  导出前还会再实时求解一次，避免库内状态过期。
- **事务安全**：确认、撤销、建批均在单个 SQLite 事务内完成，冲突回滚不留痕。
