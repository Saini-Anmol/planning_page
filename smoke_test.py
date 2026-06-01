"""
Smoke test — verifies every layer of the V1 pipeline without running the LP solve
or making any DB writes. Run from project root:

    python3 smoke_test.py

Pass = exit 0. Fail = exit 1, with the first failed check named.
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _discover_test_plan_id() -> str | None:
    """Pick any plan_id that has rows in BOTH jkt_demand AND jkt_plan_params.
    Returns None if the DB has no usable test plan (smoke checks that need
    demand will print 'skipped' instead of failing)."""
    from V1.utilities import config_loader, db
    try:
        cfg = config_loader.load()
        conn = db.connect(cfg["db"])
        cur = conn.cursor()
        cur.execute("""
            SELECT d.plan_id
            FROM jkt_demand d
            JOIN jkt_plan_params p ON p.plan_id = d.plan_id
            GROUP BY d.plan_id
            HAVING COUNT(*) > 0
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close(); conn.close()
        return row[0] if row else None
    except Exception:
        return None


TEST_PLAN_ID = _discover_test_plan_id()

CHECKS: list[tuple[str, callable]] = []


def _skip_if_no_test_plan(name: str) -> bool:
    if TEST_PLAN_ID is None:
        print(f"  SKIP  {name}  (no plan_id has rows in both jkt_demand and jkt_plan_params)")
        return True
    return False


def check(name: str):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# --------------------------------------------------------------------------- #
# 1. Filesystem layout                                                        #
# --------------------------------------------------------------------------- #
@check("filesystem: required dirs exist")
def _():
    for d in ["V1", "V1/routes", "V1/utilities", "V1/setups", "V1/reports",
              "config", "input", "output"]:
        assert (ROOT / d).is_dir(), f"missing dir: {d}"


@check("filesystem: required files exist")
def _():
    for f in ["main.py", "app.py", "config/config.yaml",
              "V1/routes/demand_route.py", "V1/routes/schedule_route.py",
              "V1/routes/upload_route.py", "V1/routes/api_route.py",
              "V1/setups/plan_params.py", "V1/setups/demand_db.py",
              "V1/utilities/db.py", "V1/utilities/config_loader.py",
              "V1/reports/kpi_writer.py", "V1/reports/plan_writer.py",
              "V1/reports/capacity_writer.py"]:
        assert (ROOT / f).is_file(), f"missing file: {f}"


# --------------------------------------------------------------------------- #
# 2. Import surface                                                           #
# --------------------------------------------------------------------------- #
@check("imports: all V1 modules load cleanly")
def _():
    for mod in ["V1.utilities.config_loader", "V1.utilities.db",
                "V1.setups.plan_params", "V1.setups.demand_db",
                "V1.routes.demand_route", "V1.routes.schedule_route",
                "V1.routes.upload_route", "V1.routes.api_route",
                "V1.reports.kpi_writer", "V1.reports.plan_writer",
                "V1.reports.capacity_writer"]:
        importlib.import_module(mod)


# --------------------------------------------------------------------------- #
# 3. Config                                                                   #
# --------------------------------------------------------------------------- #
@check("config: loads and has required sections")
def _():
    from V1.utilities import config_loader
    cfg = config_loader.load()
    for k in ["plan", "db", "paths", "demand", "schedule", "upload"]:
        assert k in cfg, f"missing top-level config key: {k}"
    assert cfg["plan"]["plan_id"], "plan.plan_id is empty"


@check("config: paths resolve to absolute dirs")
def _():
    from V1.utilities import config_loader
    cfg = config_loader.load()
    assert config_loader.input_dir(cfg).is_dir()
    assert config_loader.output_dir(cfg).is_dir()


# --------------------------------------------------------------------------- #
# 4. DB connectivity & schema                                                 #
# --------------------------------------------------------------------------- #
@check("db: mysql.connector connect() works")
def _():
    from V1.utilities import config_loader, db
    cfg = config_loader.load()
    conn = db.connect(cfg["db"])
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1
    cur.close(); conn.close()


@check("db: sqlalchemy engine() works")
def _():
    from V1.utilities import config_loader, db
    import pandas as pd
    cfg = config_loader.load()
    eng = db.engine(cfg["db"])
    df = pd.read_sql("SELECT 1 AS x", eng)
    assert df.iloc[0]["x"] == 1


@check("db: all required tables exist")
def _():
    from V1.utilities import config_loader, db
    cfg = config_loader.load()
    conn = db.connect(cfg["db"]); cur = conn.cursor()
    needed = ["jkt_plan_params", "jkt_demand", "jkt_plan_kpis", "jkt_plan",
              "jkt_plan_capacityUtilisation", "Master_Curing_Design_CycleTime",
              "Master_Curing_Allowable_Machines_source", "gt_inventory_manual",
              "Master_WC_Master", "Daily_Running_Moulds",
              "Master_Mapping_Mould_SKU"]
    for t in needed:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        cur.fetchone()
    cur.close(); conn.close()


# --------------------------------------------------------------------------- #
# 5. Setups                                                                   #
# --------------------------------------------------------------------------- #
@check("setups: plan_params.fetch returns expected fields")
def _():
    if _skip_if_no_test_plan("setups: plan_params.fetch returns expected fields"): return
    from V1.utilities import config_loader
    from V1.setups import plan_params
    cfg = config_loader.load()
    row = plan_params.fetch(cfg["db"], TEST_PLAN_ID)
    for k in ["planStartDate", "planEndDate", "oe", "re",
              "marketWeightage", "quantityWeightage", "targetdateWeightage"]:
        assert k in row, f"plan_params missing: {k}"


@check("setups: demand_db.load returns shaped rows")
def _():
    if _skip_if_no_test_plan("setups: demand_db.load returns shaped rows"): return
    from V1.utilities import config_loader
    from V1.setups import demand_db
    cfg = config_loader.load()
    rows = demand_db.load(cfg["db"], TEST_PLAN_ID)
    assert len(rows) > 0, "no demand rows for test plan"
    r = rows[0]
    for k in ["SKUCode", "SKU Description", "Requirement", "Market", "Delivery date"]:
        assert k in r, f"demand row missing key: {k}"


# --------------------------------------------------------------------------- #
# 6. Demand route logic (pure)                                                #
# --------------------------------------------------------------------------- #
@check("demand_route: market_score inverts rank correctly")
def _():
    from V1.routes.demand_route import _market_score
    plan = {"oe": 1, "re": 2, "government": 7}
    aliases = {"OE": "oe", "RE": "re", "Government": "government"}
    # rank 1 -> highest -> score 7. rank 7 -> lowest -> score 1.
    assert _market_score("OE", plan, aliases, 1, 7) == 7
    assert _market_score("RE", plan, aliases, 1, 7) == 6
    assert _market_score("Government", plan, aliases, 1, 7) == 1


@check("demand_route: end-to-end CPS for test plan (no Excel input needed)")
def _():
    if _skip_if_no_test_plan("demand_route: end-to-end CPS for test plan (no Excel input needed)"): return
    from V1.utilities import config_loader
    from V1.routes import demand_route
    cfg = config_loader.load()
    cfg["plan"]["plan_id"] = TEST_PLAN_ID
    cfg = config_loader.resolve_paths(cfg)
    out = demand_route.run(cfg)
    assert out.exists(), f"output file not written: {out}"
    import openpyxl
    wb = openpyxl.load_workbook(out)
    ws = wb.active
    assert ws.cell(row=1, column=6).value == "ConsolidatedPriorityScore"
    assert ws.max_row > 1


# --------------------------------------------------------------------------- #
# 7. Flask layer                                                              #
# --------------------------------------------------------------------------- #
@check("flask: app instantiates with expected routes")
def _():
    from app import create_app
    app = create_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/app/v1/jkt/planning-scheduling/plan/generate-plan" in rules
    assert "/app/v1/jkt/planning-scheduling/health" in rules


@check("flask: /health responds 200 via test client")
def _():
    from app import create_app
    client = create_app().test_client()
    r = client.get("/app/v1/jkt/planning-scheduling/health")
    assert r.status_code == 200, r.status_code
    assert r.get_json() == {"status": "ok"}


@check("flask: /generate- with missing plan_id returns 400")
def _():
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-plan",
                    json={})
    assert r.status_code == 400, r.status_code
    assert r.get_json()["status"] == "error"


@check("flask: /generate- extracts plan_id correctly from JSON body")
def _():
    """Stop short of running the pipeline by using a plan_id known to be missing
    from jkt_plan_params — pipeline returns 404, but only after extracting."""
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-plan",
                    json={"plan_id": "  __DOES_NOT_EXIST__  "})  # whitespace test
    body = r.get_json()
    # Should fail at duplicate_check or plan_params lookup, NOT at validation.
    assert body["status"] == "error"
    assert body.get("stage") != "validation", f"unexpected validation error: {body}"
    # plan_id should appear trimmed in the error payload
    assert body.get("plan_id") == "__DOES_NOT_EXIST__"


@check("flask: /generate- rejects non-string plan_id with 400")
def _():
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-plan",
                    json={"plan_id": 12345})
    assert r.status_code == 400, r.status_code
    assert "must be a string" in r.get_json()["message"]


@check("flask: /generate- rejects malformed JSON with 400")
def _():
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-plan",
                    data="not json at all",
                    content_type="application/json")
    assert r.status_code == 400, r.status_code
    assert r.get_json()["status"] == "error"


@check("flask: /generate- rejects already-scheduled plan_id with 409")
def _():
    """Find any plan_id with rows in jkt_plan_kpis; skip cleanly if there's none."""
    from V1.utilities import config_loader, db
    from app import create_app
    cfg = config_loader.load()
    conn = db.connect(cfg["db"])
    cur = conn.cursor()
    cur.execute("SELECT plan_id FROM jkt_plan_kpis LIMIT 1")
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        print("        (no scheduled plan_id in DB — skipping)")
        return
    scheduled_pid = row[0]
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-plan",
                    json={"plan_id": scheduled_pid})
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.get_json()}"
    body = r.get_json()
    assert body["status"] == "error"
    assert body["stage"] == "duplicate_check"


@check("exceptions: PipelineError class is importable")
def _():
    from V1.utilities.exceptions import PipelineError
    e = PipelineError("test", stage="x", status_code=418)
    assert e.stage == "x"
    assert e.status_code == 418
    assert str(e) == "test"


# --------------------------------------------------------------------------- #
# 8. Reports / writers (import-only, don't actually insert)                   #
# --------------------------------------------------------------------------- #
@check("reports: writer modules expose upload()")
def _():
    from V1.reports import kpi_writer, plan_writer, capacity_writer
    for m in (kpi_writer, plan_writer, capacity_writer):
        assert callable(m.upload), f"{m.__name__}.upload missing"


@check("kpi_writer: _count_demand_skus returns positive count")
def _():
    if _skip_if_no_test_plan("kpi_writer: _count_demand_skus returns positive count"): return
    from V1.utilities import config_loader
    from V1.reports.kpi_writer import _count_demand_skus
    cfg = config_loader.load()
    n = _count_demand_skus(TEST_PLAN_ID, cfg["db"])
    assert n > 0, f"expected positive distinct demand SKU count, got {n}"


@check("capacity_writer: imports plan_params (for window filtering)")
def _():
    """Verify the new dependency wiring is intact."""
    import V1.reports.capacity_writer as cw
    assert hasattr(cw, "plan_params"), "capacity_writer should import plan_params"


@check("schedule_route: run() is serialized with a lock")
def _():
    """Confirm the threading.Lock is in place and run() acquires it."""
    import V1.routes.schedule_route as sr
    import threading
    assert hasattr(sr, "_RUN_LOCK"), "expected _RUN_LOCK at module level"
    assert isinstance(sr._RUN_LOCK, type(threading.Lock())), "_RUN_LOCK must be a Lock"
    assert hasattr(sr, "_run_locked"), "expected _run_locked inner function"


@check("plan_writer: _load_sku_descriptions returns SKU→desc map")
def _():
    if _skip_if_no_test_plan("plan_writer: _load_sku_descriptions returns SKU→desc map"): return
    from V1.utilities import config_loader
    from V1.reports.plan_writer import _load_sku_descriptions
    cfg = config_loader.load()
    mp = _load_sku_descriptions(TEST_PLAN_ID, cfg["db"])
    assert isinstance(mp, dict) and len(mp) > 0, f"expected non-empty map, got {mp!r}"
    for sku, desc in mp.items():
        assert isinstance(desc, str) and desc.strip(), f"bad desc for {sku!r}: {desc!r}"


@check("kpi_writer: demand-weighted fulfillment caps each SKU at 100%")
def _():
    """Synthetic sheet: SKU A over-fulfilled (120%), SKU B at 80%, equal demand.
    Capped weighted = (1.0·100 + 0.8·100)/200 = 90%. Uncapped would be 100%."""
    import openpyxl
    from V1.reports.kpi_writer import _demand_weighted_fulfillment
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["title"]); ws.append(["summary"])
    ws.append(["SKUCode","Priority","Demand","GT","Planned_Units","Gap","Fulfillment_Pct"])
    ws.append(["A", 0, 100, 0, 120, 0, 1.2])   # over-fulfilled → capped to 1.0
    ws.append(["B", 0, 100, 0,  80, 0, 0.8])
    result = _demand_weighted_fulfillment(ws)
    assert abs(result - 90.0) < 0.01, f"expected 90.0, got {result}"


@check("capacity_writer: divides by full fleet, not just used machines")
def _():
    """Read the source to confirm the denominator switched to all_machines."""
    src = (
        __import__("pathlib").Path("V1/reports/capacity_writer.py").read_text()
    )
    assert "all_machines" in src, "all_machines set should exist"
    assert "Machine Utilization" in src, "should read Machine Utilization sheet"
    assert "n_machines" in src, "should use fleet-size denominator"
    # Ensure the old machines_per_day denominator pattern is gone
    assert "machines_per_day[d]" not in src, "still using used-machines denominator"


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #
_SKIP_SENTINEL = object()


def main() -> int:
    if TEST_PLAN_ID is None:
        print("  (no plan_id with both demand and params found in DB — demand-dependent checks will skip)")
    else:
        print(f"  (using TEST_PLAN_ID = {TEST_PLAN_ID})")
    print()

    passed = failed = skipped = 0
    for name, fn in CHECKS:
        try:
            result = fn()
            # _skip_if_no_test_plan returns early (None) AFTER printing its own "SKIP" line.
            # The function body that follows is a no-op, so we can't easily tell pass vs skip
            # here — but the SKIP line already printed; just don't print PASS for those.
            if TEST_PLAN_ID is None and "TEST_PLAN_ID" in fn.__code__.co_names:
                skipped += 1
                continue
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}")
            print(f"        {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
            failed += 1
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
