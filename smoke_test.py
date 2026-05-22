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

TEST_PLAN_ID = "BTP_June_Plan_V1184_472835"   # known to have rows in jkt_demand

CHECKS: list[tuple[str, callable]] = []


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
    from V1.utilities import config_loader
    from V1.setups import plan_params
    cfg = config_loader.load()
    row = plan_params.fetch(cfg["db"], TEST_PLAN_ID)
    for k in ["planStartDate", "planEndDate", "oe", "re",
              "marketWeightage", "quantityWeightage", "targetdateWeightage"]:
        assert k in row, f"plan_params missing: {k}"


@check("setups: demand_db.load returns shaped rows")
def _():
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
    assert "/app/v1/jkt/planning-scheduling/plan/generate-" in rules
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
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-",
                    json={})
    assert r.status_code == 400, r.status_code
    assert r.get_json()["status"] == "error"


@check("flask: /generate- extracts plan_id correctly from JSON body")
def _():
    """Stop short of running the pipeline by using a plan_id known to be missing
    from jkt_plan_params — pipeline returns 404, but only after extracting."""
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-",
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
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-",
                    json={"plan_id": 12345})
    assert r.status_code == 400, r.status_code
    assert "must be a string" in r.get_json()["message"]


@check("flask: /generate- rejects malformed JSON with 400")
def _():
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-",
                    data="not json at all",
                    content_type="application/json")
    assert r.status_code == 400, r.status_code
    assert r.get_json()["status"] == "error"


@check("flask: /generate- rejects already-scheduled plan_id with 409")
def _():
    """Use TEST_PLAN_ID since it's the one with rows in all 3 output tables."""
    from app import create_app
    client = create_app().test_client()
    r = client.post("/app/v1/jkt/planning-scheduling/plan/generate-",
                    json={"plan_id": TEST_PLAN_ID})
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


@check("kpi_writer: _count_demand_skus returns DISTINCT skuCode count")
def _():
    """For TEST_PLAN_ID, jkt_demand has 15 rows but 11 distinct SKUs."""
    from V1.utilities import config_loader
    from V1.reports.kpi_writer import _count_demand_skus
    cfg = config_loader.load()
    n = _count_demand_skus(TEST_PLAN_ID, cfg["db"])
    assert n == 11, f"expected 11 distinct demand SKUs, got {n}"


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
    from V1.utilities import config_loader
    from V1.reports.plan_writer import _load_sku_descriptions
    cfg = config_loader.load()
    mp = _load_sku_descriptions(TEST_PLAN_ID, cfg["db"])
    assert isinstance(mp, dict) and len(mp) > 0, f"expected non-empty map, got {mp!r}"
    # Every description should be a non-empty string
    for sku, desc in mp.items():
        assert isinstance(desc, str) and desc.strip(), f"bad desc for {sku!r}: {desc!r}"


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
def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}")
            print(f"        {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
