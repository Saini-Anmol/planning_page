"""
dataloader2.py — generate the BUILDING-schedule input CSVs from the database.

Pulls the two building master tables from MySQL and writes them as CSVs into the
same input folder used by the curing scheduler (default: ./inputs):

    building_cycle_times.csv   SAPMachineCode, MachineName, StageName, Norms, CycleTime_min
    building_allowable.csv     SKUCode, Machines (list literal of allowed machine codes)

Source tables
    Master_Building_Machine_Design_cycleTime   (per-machine cycle time + stage)
    Master_Building_Allowable_Machines_source  (SKU x machine Yes/No grid)

Notes
  - Cycle time is PER MACHINE (with a Stage Name: combined / stage1 / stage2),
    not per SKU. It is already net (Norms ~= 480 / CycleTime), so no efficiency
    derate is applied.
  - The allowable grid may contain a few machine columns with no cycle-time row
    (e.g. 6001-6004). They are kept in the Machines list here; the building
    scheduler drops them (and flags them) because they have no cycle time.

Usage
    python dataloader2.py                      # -> ./inputs
    python dataloader2.py  C:\\path\\to\\folder  # custom folder
"""

import os
import sys

import pandas as pd

from curing_LP import Config
from dataloader import get_engine

DEFAULT_INPUT_DIR = "inputs"
DB = Config.DB_NAME


def load_building_cycle_times(engine) -> pd.DataFrame:
    df = pd.read_sql(
        f"SELECT * FROM {DB}.Master_Building_Machine_Design_cycleTime", engine
    )
    df = df.rename(columns={
        "SAP Machine Code":     "SAPMachineCode",
        "Machine Name":         "MachineName",
        "Stage Name":           "StageName",
        "Cycle Time (Minutes)": "CycleTime_min",
    })
    df["StageName"] = df["StageName"].astype(str).str.strip().str.lower()
    return df[["SAPMachineCode", "MachineName", "StageName", "Norms", "CycleTime_min"]]


def load_building_allowable(engine) -> pd.DataFrame:
    df = pd.read_sql(
        f"SELECT * FROM {DB}.Master_Building_Allowable_Machines_source", engine
    )
    df = df.rename(columns={"SKU Code": "SKUCode"})
    mcols = [c for c in df.columns if str(c).isdigit()]
    df["Machines"] = df.apply(
        lambda r: [int(c) for c in mcols
                   if str(r[c]).strip().lower() == "yes"],
        axis=1,
    )
    df["SKUCode"] = df["SKUCode"].astype(str).str.strip()
    return df[["SKUCode", "Machines"]]


def load_building_changeover(engine) -> pd.DataFrame:
    df = pd.read_sql(
        f"SELECT * FROM {DB}.Master_Building_ChangeoverTime", engine
    )
    df = df.rename(columns={
        "MachineCode":              "MachineCode",
        "Machine Name":             "MachineName",
        "Same Size(Minutes)":       "SameSize_Min",
        "Different Size(Minutes)":  "DifferentSize_Min",
    })
    return df[["MachineCode", "MachineName", "SameSize_Min", "DifferentSize_Min"]]


def generate_building_inputs(input_dir: str = DEFAULT_INPUT_DIR) -> dict:
    os.makedirs(input_dir, exist_ok=True)
    engine = get_engine()

    print(f"\n[dataloader2] Target folder: {os.path.abspath(input_dir)}\n")
    written = {}
    jobs = {
        "building_cycle_times.csv": (load_building_cycle_times,
                                     "SAPMachineCode, MachineName, StageName, Norms, CycleTime_min"),
        "building_allowable.csv":   (load_building_allowable,
                                     "SKUCode, Machines(list)"),
        "building_changeover.csv":  (load_building_changeover,
                                     "MachineCode, MachineName, SameSize_Min, DifferentSize_Min"),
    }
    for fname, (fn, desc) in jobs.items():
        path = os.path.join(input_dir, fname)
        try:
            df = fn(engine)
            df.to_csv(path, index=False)
            written[fname] = len(df)
            print(f"  OK   {fname:26s} {len(df):>6,} rows   [{desc}]")
        except Exception as e:
            written[fname] = None
            print(f"  FAIL {fname:26s}  {type(e).__name__}: {e}")

    # quick stage summary
    try:
        ct = load_building_cycle_times(engine)
        counts = ct["StageName"].value_counts().to_dict()
        print(f"\n[dataloader2] Machine stages: {counts}")
    except Exception:
        pass

    ok = sum(1 for v in written.values() if v is not None)
    print(f"\n[dataloader2] Done: {ok}/{len(jobs)} building CSVs in "
          f"{os.path.abspath(input_dir)}")
    print("[dataloader2] Next: python building_schedule.py")
    return written


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT_DIR
    generate_building_inputs(folder)
