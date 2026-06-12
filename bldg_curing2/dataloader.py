"""
dataloader.py — generate the curing-scheduler input CSVs from the database.

Pulls the five MASTER datasets from MySQL and writes them as CSV files into an
input folder (default: ./inputs). It reuses the SQL already defined in
curing_LP.ETL, so the queries stay in one place.

DEMAND is NOT generated here — you add it manually as <folder>/demand.csv.
Running this writes a demand_TEMPLATE.csv showing the exact format. See the
DEMAND FORMAT block at the bottom of this file.

The CSVs written here are consumption-ready by the scheduler:
    from curing_LP import run_from_csv
    run_from_csv("inputs")          # reads this folder end-to-end

Usage
-----
    python dataloader.py                      # -> ./inputs
    python dataloader.py  C:\\path\\to\\folder  # custom folder

Credentials come from curing_LP.Config (override with env vars
DB_SERVER / DB_NAME / DB_USER / DB_PASSWORD if you prefer not to hardcode).
"""

import os
import sys

import pandas as pd

from curing_LP import Config, ETL

try:
    from sqlalchemy import create_engine
except ImportError:  # pragma: no cover
    create_engine = None


DEFAULT_INPUT_DIR = "inputs"


# ── demand template (the format YOU fill in manually) ──────────────────────────
DEMAND_COLUMNS = ["SKUCode", "Quantity", "Priority"]
DEMAND_EXAMPLE_ROWS = [
    # SKUCode (str)   Quantity (int, tyres)   Priority (float, higher = first)
    ["1010001234", 1200, 0.95],
    ["1010005678",  800, 0.80],
    ["1010009999",  450, 0.55],
]


def get_engine():
    """Build the SQLAlchemy engine, preferring env vars over Config."""
    if create_engine is None:
        raise ImportError("sqlalchemy not installed. `pip install sqlalchemy pymysql`")
    server = os.getenv("DB_SERVER",   Config.DB_SERVER)
    name   = os.getenv("DB_NAME",     Config.DB_NAME)
    user   = os.getenv("DB_USER",     Config.DB_USER)
    pwd    = os.getenv("DB_PASSWORD", Config.DB_PASSWORD)
    return create_engine(f"mysql+pymysql://{user}:{pwd}@{server}/{name}")


def write_demand_template(input_dir: str):
    """Write demand_TEMPLATE.csv (never overwrites an existing demand.csv)."""
    tmpl = os.path.join(input_dir, "demand_TEMPLATE.csv")
    pd.DataFrame(DEMAND_EXAMPLE_ROWS, columns=DEMAND_COLUMNS).to_csv(tmpl, index=False)
    print(f"  wrote {tmpl:42s} (template — copy to demand.csv and fill in)")

    demand = os.path.join(input_dir, "demand.csv")
    if os.path.exists(demand):
        print(f"  kept  {demand:42s} (already present — left untouched)")


def generate_inputs(input_dir: str = DEFAULT_INPUT_DIR,
                    tyre_type: str = Config.TYRE_TYPE) -> dict:
    """Pull masters from the DB and write them as CSVs into input_dir."""
    os.makedirs(input_dir, exist_ok=True)
    etl = ETL(get_engine(), tyre_type)

    # name -> (callable, description). Each loader returns a clean DataFrame.
    loaders = {
        "cycle_times.csv":       (etl.load_cycle_times,      "SKUCode, CycleTime_min"),
        "machine_allowable.csv": (etl.load_machine_allowable, "SKUCode, Machines(list)"),
        "gt_inventory.csv":      (etl.load_gt_inventory,      "SKUCode, GT_Inventory"),
        "running_moulds.csv":    (etl.load_running_moulds,    "Machine, SKUCode, MouldNos, MouldLife_remaining, Num_Moulds"),
        "mould_master.csv":      (etl.load_mould_master,      "mould master (MouldNo / Matl.Code / Active Flag ...)"),
    }

    written = {}
    print(f"\n[dataloader] Target folder: {os.path.abspath(input_dir)}")
    print(f"[dataloader] Tyre type    : {tyre_type}\n")

    for fname, (fn, desc) in loaders.items():
        path = os.path.join(input_dir, fname)
        try:
            df = fn()
            df.to_csv(path, index=False)
            written[fname] = len(df)
            print(f"  OK   {fname:24s} {len(df):>7,} rows   [{desc}]")
        except Exception as e:  # one bad table shouldn't kill the rest
            written[fname] = None
            print(f"  FAIL {fname:24s}  {type(e).__name__}: {e}")

    print()
    write_demand_template(input_dir)

    ok   = sum(1 for v in written.values() if v is not None)
    print(f"\n[dataloader] Done: {ok}/{len(loaders)} master CSVs written to "
          f"{os.path.abspath(input_dir)}")
    print("[dataloader] Next:")
    print("  1. copy demand_TEMPLATE.csv -> demand.csv and fill in your demand")
    print(f"  2. run:  python -c \"from curing_LP import run_from_csv; "
          f"run_from_csv(r'{input_dir}')\"")
    return written


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT_DIR
    generate_inputs(folder)


# ══════════════════════════════════════════════════════════════════════════════
# DEMAND FORMAT  (you create  <folder>/demand.csv  by hand)
# ──────────────────────────────────────────────────────────────────────────────
#   Column     Type    Required  Meaning
#   --------   -----   --------  ----------------------------------------------
#   SKUCode    str     yes       SKU / SAP code. MUST match the codes in
#                                 cycle_times.csv, machine_allowable.csv and
#                                 mould_master.csv, or the SKU is dropped as
#                                 unschedulable.
#   Quantity   int     yes       Tyres required over the horizon (physical
#                                 units). Rows with Quantity <= 0 are ignored.
#   Priority   float   yes       Scheduling priority; HIGHER is scheduled first
#                                 (the LP minimises unmet demand-minutes and the
#                                 rounder/top-up walk SKUs in priority order).
#
#   - One row per SKU. If a SKU appears more than once, sum the quantities
#     yourself first (the DB path groups by SKUCode; the CSV path does not).
#   - Plain CSV, comma-separated, UTF-8, with the header row.
#
#   Example demand.csv:
#       SKUCode,Quantity,Priority
#       1010001234,1200,0.95
#       1010005678,800,0.80
#       1010009999,450,0.55
# ══════════════════════════════════════════════════════════════════════════════
