from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 主汇总表：粒度锁定为业务日+门店编码，不含品类列；对账唯一汇总来源
CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 旁路汇总表：业务日+门店+品类，独立于主汇总层；主路径禁止写入，对账禁止读取
CREATE TABLE IF NOT EXISTS layer_summary_category (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  category TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code, category),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  lane TEXT NOT NULL DEFAULT '主路径',
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 主旁路隔离证明：随每次主路径/旁路任务落一条，记录主写入数、旁路写入数、
-- 对账对象层名与旁路成败；只由对应任务写，供只读接口按任务 id 查询。
CREATE TABLE IF NOT EXISTS lane_isolation_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL UNIQUE,
  lane TEXT NOT NULL,
  main_write_count INTEGER NOT NULL DEFAULT 0,
  bypass_write_count INTEGER NOT NULL DEFAULT 0,
  reconcile_layer TEXT NOT NULL,
  bypass_outcome TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id),
  FOREIGN KEY(job_id) REFERENCES layer_jobs(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);
"""


def _migrate(conn) -> None:
    """对旧版本库做幂等补齐，不改动既有数据。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(layer_jobs)").fetchall()}
    if "lane" not in cols:
        conn.execute(
            "ALTER TABLE layer_jobs ADD COLUMN lane TEXT NOT NULL DEFAULT '主路径'"
        )
    # 老库的主汇总表若带品类列，重建为业务日+门店编码粒度，并把旧行按新键收敛保留
    summary_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(layer_summary)").fetchall()
    }
    if "category" in summary_cols:
        legacy = conn.execute(
            """
            SELECT s.domain_id, s.biz_day, s.store_code,
                   SUM(s.amount_sum) AS amount_sum, SUM(s.qty_sum) AS qty_sum,
                   SUM(s.line_count) AS line_count
            FROM layer_summary s
            JOIN domains d ON d.id = s.domain_id
            GROUP BY s.domain_id, s.biz_day, s.store_code
            """
        ).fetchall()
        conn.execute("DROP TABLE layer_summary")
        conn.execute(
            """
            CREATE TABLE layer_summary (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain_id INTEGER NOT NULL,
              biz_day TEXT NOT NULL,
              store_code TEXT NOT NULL,
              amount_sum REAL NOT NULL,
              qty_sum INTEGER NOT NULL,
              line_count INTEGER NOT NULL,
              UNIQUE(domain_id, biz_day, store_code),
              FOREIGN KEY(domain_id) REFERENCES domains(id)
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO layer_summary(domain_id, biz_day, store_code, amount_sum, qty_sum, line_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["domain_id"],
                    r["biz_day"],
                    r["store_code"],
                    r["amount_sum"],
                    r["qty_sum"],
                    r["line_count"],
                )
                for r in legacy
            ],
        )


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        # 先备好主题域再迁移，保证重建表时的外键目标存在
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_domain(conn) -> None:
    fixture = "门店日销售-贴源.jsonl"
    cur = conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, description)
        VALUES (?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            fixture,
            "演示贴源装载、明细清洗与按门店日汇总",
        ),
    )
    domain_id = int(cur.lastrowid)
    path = FIXTURES_DIR / fixture
    if not path.exists():
        # 仓库内相对路径兜底
        alt = Path(__file__).resolve().parents[2] / "fixtures" / fixture
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                obj.get("业务日") or "",
                obj.get("门店编码") or "",
                obj.get("门店名称") or "",
                obj.get("品类") or "",
                float(obj.get("金额") or 0),
                int(obj.get("件数") or 0),
                json.dumps(obj, ensure_ascii=False),
            ),
        )
