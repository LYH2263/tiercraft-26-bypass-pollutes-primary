import ast
import inspect
import re
import textwrap

import pytest
from fastapi.testclient import TestClient

from app.db import DB_PATH, connect
from app.services import pipeline


def _code_without_docs(fn) -> str:
    """取函数源码并剥离 docstring 与注释，只留可执行代码用于静态守卫。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    if (
        func.body
        and isinstance(func.body[0], ast.Expr)
        and isinstance(func.body[0].value, ast.Constant)
    ):
        func.body = func.body[1:]
    return ast.unparse(tree)


@pytest.fixture
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    from app.main import app

    with TestClient(app) as c:
        yield c


def _table_columns(table: str) -> set[str]:
    conn = connect()
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _scalar(sql: str, args=()):
    conn = connect()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def prepared(client):
    """跑完贴源到明细 + 主路径汇总，返回 domain_id。"""
    r = client.post("/api/jobs/paste-to-detail", json={"domain_id": 1})
    assert r.status_code == 200 and r.json()["status"] == "成功"
    r = client.post("/api/jobs/detail-to-summary", json={"domain_id": 1})
    assert r.status_code == 200 and r.json()["status"] == "成功"
    return 1


# ---- 要求 1：主汇总粒度锁定为业务日+门店，对账只核主汇总与明细 ----

def test_main_summary_grain_locked_without_category(prepared):
    cols = _table_columns("layer_summary")
    assert cols == {"id", "domain_id", "biz_day", "store_code",
                    "amount_sum", "qty_sum", "line_count"}
    # 品类列不得进入主汇总表
    assert "category" not in cols

    rows = pipeline.layer_rows("summary", 1)
    keys = {(r["biz_day"], r["store_code"]) for r in rows}
    # 夹具中 S01 在 11-10 有两个品类行，主汇总必须收敛成 1 行
    assert ("2026-11-10", "S01") in keys
    assert len(rows) == len(keys)
    s01 = next(r for r in rows if r["biz_day"] == "2026-11-10" and r["store_code"] == "S01")
    assert s01["line_count"] == 2  # 两条品类明细合入同一主汇总行
    assert abs(s01["amount_sum"] - (120.5 + 86.0)) < 1e-9


def test_main_summary_task_never_touches_bypass_table():
    src = _code_without_docs(pipeline.run_detail_to_summary)
    assert "layer_summary_category" not in src
    # 汇总键只能由业务日与门店编码构成
    assert re.search(r"key\s*=\s*\(row\[.biz_day.\],\s*row\[.store_code.\]\)", src)
    assert "category" not in src


def test_reconcile_only_reads_main_summary(prepared):
    src = _code_without_docs(pipeline.run_reconcile)
    # 静态守卫：对账函数禁止引用旁路表/品类维
    assert "layer_summary_category" not in src
    assert "category" not in src.lower()

    report = pipeline.run_reconcile(1)
    assert report["status"] == "一致"
    assert "旁路" in report["detail"]["对账范围"]
    assert report["detail"]["键对齐"] is True
    # 报告字段中不得混入旁路计数
    assert not any("旁路" in k or "品类" in k for k in report["detail"].keys() - {"对账范围"})


# ---- 要求 2：独立旁路任务写独立旁路表 ----

def test_bypass_task_writes_independent_table(prepared):
    before_main = _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1")
    r = pipeline.run_detail_to_category_summary(1)
    assert r["lane"] == pipeline.BYPASS_LANE
    assert r["status"] == "成功"

    bypass_rows = pipeline.layer_rows("category_summary", 1)
    # 旁路键含品类：S01 在 11-10 的饮料/零食各一行
    cat_keys = {(r["biz_day"], r["store_code"], r["category"]) for r in bypass_rows}
    assert ("2026-11-10", "S01", "饮料") in cat_keys
    assert ("2026-11-10", "S01", "零食") in cat_keys

    # 旁路报告计数与旁路表同源派生
    assert r["out_count"] == len(bypass_rows)
    assert r["in_count"] == _scalar("SELECT COUNT(*) FROM layer_detail WHERE domain_id=1")
    assert _scalar(
        "SELECT COALESCE(SUM(line_count),0) FROM layer_summary_category WHERE domain_id=1"
    ) == _scalar("SELECT COUNT(*) FROM layer_detail WHERE domain_id=1")

    # 旁路任务不得写入主汇总层
    assert _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1") == before_main


def test_bypass_task_never_writes_main_layer():
    src = _code_without_docs(pipeline.run_detail_to_category_summary)
    assert "INSERT INTO layer_summary(" not in src
    assert "DELETE FROM layer_summary " not in src
    assert "UPDATE layer_summary " not in src


def test_bypass_not_a_reconcile_target(prepared):
    pipeline.run_detail_to_category_summary(1)
    # 旁路行数天然多于主汇总行数；若对账误读旁路，键对齐必然失败
    bypass_n = _scalar("SELECT COUNT(*) FROM layer_summary_category WHERE domain_id=1")
    main_n = _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1")
    assert bypass_n > main_n
    report = pipeline.run_reconcile(1)
    assert report["status"] == "一致"
    assert report["detail_count"] == report["detail"]["汇总行数合计"]


# ---- 要求 3：主/旁路失败域隔离 ----

def test_bypass_failure_isolated_from_main(prepared):
    main_before = pipeline.layer_rows("summary", 1)
    main_job_count = len([j for j in pipeline.list_jobs(1) if j["lane"] == pipeline.MAIN_LANE])

    job = pipeline.run_detail_to_category_summary(1, simulate_failure=True)

    # 只标旁路任务失败并保留原因
    assert job["lane"] == pipeline.BYPASS_LANE
    assert job["status"] == "失败"
    assert "原因" in job["message"]

    # 旁路写入整笔回滚，无残留半成品
    assert _scalar("SELECT COUNT(*) FROM layer_summary_category WHERE domain_id=1") == 0

    # 主汇总未被回滚/清空/锁死：行数与内容完全不变
    assert pipeline.layer_rows("summary", 1) == main_before
    main_jobs_after = [j for j in pipeline.list_jobs(1) if j["lane"] == pipeline.MAIN_LANE]
    assert len(main_jobs_after) == main_job_count
    assert all(j["status"] == "成功" for j in main_jobs_after)


def test_reconcile_still_consistent_when_bypass_failed(prepared):
    pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    # 主汇总成功、旁路失败：对账仍只对主汇总，可判定一致
    report = pipeline.run_reconcile(1)
    assert report["status"] == "一致"
    assert report["key_mismatch"] == 0
    assert abs(report["amount_drift"]) < 1e-9


def test_bypass_can_succeed_after_failure(prepared):
    pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    ok = pipeline.run_detail_to_category_summary(1, simulate_failure=False)
    assert ok["status"] == "成功"
    assert _scalar("SELECT COUNT(*) FROM layer_summary_category WHERE domain_id=1") == ok["out_count"]
    # 失败记录仍保留，且与成功记录分属不同任务行
    lanes = pipeline.list_jobs(1, lane=pipeline.BYPASS_LANE)
    statuses = {j["status"] for j in lanes}
    assert statuses == {"失败", "成功"}


def test_no_shared_transaction_rollback(prepared):
    # 主路径任务与旁路任务是不同连接/事务：旁路失败后仍可独立重跑主路径
    pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    main_job = pipeline.run_detail_to_summary(1)
    assert main_job["status"] == "成功"
    assert main_job["lane"] == pipeline.MAIN_LANE
    assert _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1") > 0


# ---- 要求 4：界面/接口结果可区分，旁路计数不进主报告字段 ----

def test_jobs_api_separates_lanes(client, prepared):
    pipeline.run_detail_to_category_summary(1)
    all_jobs = client.get("/api/jobs?domain_id=1").json()
    main_jobs = [j for j in all_jobs if j["lane"] == "主路径"]
    bypass_jobs = [j for j in all_jobs if j["lane"] == "品类旁路"]
    assert main_jobs and bypass_jobs
    assert all("品类" not in j["step_label"] for j in main_jobs)

    only_bypass = client.get("/api/jobs?domain_id=1&lane=品类旁路").json()
    assert all(j["lane"] == "品类旁路" for j in only_bypass)
    only_main = client.get("/api/jobs?domain_id=1&lane=主路径").json()
    assert all(j["lane"] == "主路径" for j in only_main)


def test_dashboard_keeps_bypass_counts_separate(client, prepared):
    pipeline.run_detail_to_category_summary(1)
    dash = client.get("/api/dashboard").json()
    # 主分层计数不含旁路
    assert "品类旁路汇总" not in dash["分层计数"]
    assert dash["旁路计数"]["品类旁路汇总"] > 0
    # 主任务面板与旁路任务面板分开
    assert all(j["通道"] == "主路径" for j in dash["最近任务"])
    assert all(j["通道"] == "品类旁路" for j in dash["最近旁路任务"])


def test_layer_endpoints_keep_tables_apart(client, prepared):
    pipeline.run_detail_to_category_summary(1)
    main_rows = client.get("/api/layers/summary?domain_id=1").json()
    bypass_rows = client.get("/api/layers/category_summary?domain_id=1").json()
    assert all("category" not in r for r in main_rows)
    assert all("category" in r for r in bypass_rows)
    assert len(bypass_rows) > len(main_rows)


def test_reconcile_report_rows_never_carry_bypass_fields(prepared):
    pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    pipeline.run_reconcile(1)
    for r in pipeline.list_reconcile(1):
        assert "lane" not in r
        assert "out_count" not in r
        assert all("品类" not in k for k in r["detail"].keys() - {"对账范围"})


# ---- 要求 5：主旁路隔离证明实体 + 按任务 id 只读接口 ----

def test_isolation_proof_table_exists(client):
    cols = _table_columns("lane_isolation_proofs")
    assert {
        "id",
        "domain_id",
        "job_id",
        "lane",
        "main_write_count",
        "bypass_write_count",
        "reconcile_layer",
        "bypass_outcome",
    } <= cols


def test_main_and_bypass_jobs_each_record_isolation_proof(client, prepared):
    main_proofs = client.get("/api/isolation-proofs/1").json()
    assert main_proofs["通道"] == "主路径"
    assert main_proofs["主写入数"] > 0
    assert main_proofs["旁路写入数"] == 0
    assert main_proofs["对账对象层名"] == "主汇总层"

    ok_job = pipeline.run_detail_to_category_summary(1)
    proof = client.get(f"/api/isolation-proofs/{ok_job['id']}").json()
    assert proof["通道"] == "品类旁路"
    assert proof["主写入数"] == 0
    assert proof["旁路写入数"] == ok_job["out_count"]
    assert proof["对账对象层名"] == "主汇总层"
    assert proof["旁路成败"] == "成功"


def test_bypass_failure_proof_marks_main_untouched(client, prepared):
    main_before = _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1")
    job = pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    proof = client.get(f"/api/isolation-proofs/{job['id']}").json()
    assert proof["旁路成败"] == "失败"
    assert proof["主写入数"] == 0
    assert proof["旁路写入数"] == 0
    assert proof["对账对象层名"] == "主汇总层"
    # 证明记录不改变主汇总层
    assert _scalar("SELECT COUNT(*) FROM layer_summary WHERE domain_id=1") == main_before


def test_isolation_proof_endpoint_is_read_only(client, prepared):
    job = pipeline.run_detail_to_category_summary(1)
    first = client.get(f"/api/isolation-proofs/{job['id']}").json()
    second = client.get(f"/api/isolation-proofs/{job['id']}").json()
    assert first == second
    assert _scalar(
        "SELECT COUNT(*) FROM lane_isolation_proofs WHERE job_id=?", (job["id"],)
    ) == 1


def test_isolation_proof_endpoint_404_for_unknown_job(client, prepared):
    assert client.get("/api/isolation-proofs/999999").status_code == 404


def test_reconcile_layer_name_never_bypass(prepared):
    src = _code_without_docs(pipeline.run_reconcile)
    assert "layer_summary_category" not in src
    report = pipeline.run_reconcile(1)
    assert report["detail"]["对账对象层名"] == "主汇总层"


def test_proofs_never_leak_into_reconcile_report(prepared):
    pipeline.run_detail_to_category_summary(1)
    report = pipeline.run_reconcile(1)
    for key in ("旁路写入数", "旁路成败", "主写入数"):
        assert key not in report["detail"]
        assert key not in report


def test_dashboard_lists_isolation_proofs(client, prepared):
    pipeline.run_detail_to_category_summary(1, simulate_failure=True)
    dash = client.get("/api/dashboard").json()
    proofs = dash["最近隔离证明"]
    assert proofs
    lanes = {p["通道"] for p in proofs}
    assert {"主路径", "品类旁路"} <= lanes
    assert any(p["旁路成败"] == "失败" for p in proofs)
    # 主报告字段仍不含旁路计数
    assert "品类旁路汇总" not in dash["分层计数"]


def test_legacy_summary_with_category_migrates_to_store_grain(tmp_path, monkeypatch):
    """老库主汇总表带品类列时，迁移后收敛为业务日+门店编码且不丢行。"""
    import sqlite3

    from app import seed

    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE domains (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
          biz_key TEXT NOT NULL, fixture_file TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '');
        CREATE TABLE layer_summary (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL,
          biz_day TEXT NOT NULL, store_code TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '', amount_sum REAL NOT NULL,
          qty_sum INTEGER NOT NULL, line_count INTEGER NOT NULL,
          UNIQUE(domain_id, biz_day, store_code, category));
        INSERT INTO domains(id, name, biz_key, fixture_file)
        VALUES (1, '门店日销售', '业务日+门店编码', '门店日销售-贴源.jsonl');
        INSERT INTO layer_summary(domain_id, biz_day, store_code, category, amount_sum, qty_sum, line_count)
        VALUES (1, '2026-11-10', 'S01', '饮料', 120.5, 10, 1),
               (1, '2026-11-10', 'S01', '零食', 86.0, 7, 1);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(seed, "connect", lambda: _legacy_connect(legacy_db))
    seed.init_db()

    conn = _legacy_connect(legacy_db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(layer_summary)")}
        assert "category" not in cols
        rows = conn.execute(
            "SELECT biz_day, store_code, amount_sum, qty_sum, line_count FROM layer_summary"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["store_code"] == "S01"
    assert abs(rows[0]["amount_sum"] - 206.5) < 1e-9
    assert rows[0]["line_count"] == 2


def _legacy_connect(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


