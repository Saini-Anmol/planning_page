"""
coupled_plan.py — mutually-feasible CURING + BUILDING plan (suite v6.0).

The green-tyre building fleet is the binding constraint: curing wants to cure
more GTs than building can supply. This orchestrator couples the two schedulers
so the final plan is mutually feasible — curing never plans to cure more of a
SKU than building can actually deliver.

Coupling (fixed-point iteration)
  1. Run curing against full demand            -> curing's press-capacity plan.
  2. Run building against that plan            -> building's deliverable supply.
  3. Cap each SKU's curing demand to building's deliverable; re-run curing.
  4. Re-run building against the capped curing plan.
  Repeat 3-4 until building's total output stops changing (converged).

The result: curing cures exactly what building can supply, building supplies
exactly what curing cures, prioritised by SKU priority.

Output (versioned)  output/v6.0/
  PCR_Curing_Schedule_v6.0.xlsx     final curing plan (6 sheets)
  PCR_Building_Schedule_v6.0.xlsx   final building plan (6 sheets)
  PCR_Integrated_Plan_v6.0.xlsx     executive summary, SKU reconciliation,
                                    daily plan, coupling log

Run
  python coupled_plan.py
"""

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from _version import VERSION_LABEL, run_stamp
from curing_LP import (Config, ExcelExporter, _get_shift_fn,
                       JK_LP_Curing_Scheduler_v2)
from curing_heuristic import run_heuristic_from_csv
from building_schedule import (
    run_building_from_csv, building_capacity_envelope, BuildingExcelExporter,
)

# Days curing lags building (0 = build & cure same day, build-first).
TIME_PHASE_LAG_DAYS = 0


# ══════════════════════════════════════════════════════════════════════════════
# COUPLING  (deterministic, single-pass — no oscillation)
# ══════════════════════════════════════════════════════════════════════════════
def _complete_cap(partial: dict, keys) -> dict:
    """Cap dict covering EVERY demand SKU (0 where absent) so none escapes."""
    return {s: int(partial.get(s, 0)) for s in keys}


def couple(input_dir="inputs", plan_start=None):
    """
    1  Curing vs full demand            -> curing's press/mould capacity.
    2  Building capacity envelope       -> max GTs building can supply per SKU.
    3  Curing capped to that envelope   -> first feasible curing plan.
    4  Building JIT vs that plan         -> building's real time-phased delivery.
    5  Curing capped to that delivery    -> FINAL curing = exactly what's built.
    6  Building JIT vs final curing      -> FINAL build (curing == build).
    All caps cover every demand SKU (0 where unbuildable), so nothing leaks.
    """
    if plan_start is None:
        plan_start = Config.PLAN_DATE

    print(f"\n{'#'*64}\n#  STEP 1/6  Curing vs full demand\n{'#'*64}")
    cure0 = run_heuristic_from_csv(input_dir, plan_start, write_excel=False)
    demand   = {s: int(d) for s, d in zip(cure0["demand_fulfillment"]["SKUCode"],
                                          cure0["demand_fulfillment"]["Demand"])}
    priority = dict(zip(cure0["demand_fulfillment"]["SKUCode"],
                        cure0["demand_fulfillment"]["Priority"]))

    print(f"\n{'#'*64}\n#  STEP 2/6  Building capacity envelope\n{'#'*64}")
    supply = building_capacity_envelope(input_dir, demand, priority)
    print(f"  [Couple] Building can supply {sum(supply.values()):,} GTs "
          f"(vs demand {sum(demand.values()):,})")

    print(f"\n{'#'*64}\n#  STEP 3/6  Curing capped to envelope\n{'#'*64}")
    cure_a = run_heuristic_from_csv(input_dir, plan_start,
                                    write_excel=False, demand_cap=supply)

    print(f"\n{'#'*64}\n#  STEP 4/6  Building JIT vs capped curing\n{'#'*64}")
    bld_a = run_building_from_csv(input_dir, plan_start,
                                  curing_results=cure_a, write_excel=False)

    print(f"\n{'#'*64}\n#  STEP 5/6  Curing re-capped to actual build\n{'#'*64}")
    deliver = _complete_cap(dict(zip(bld_a["gt_supply"]["SKUCode"],
                                     bld_a["gt_supply"]["GT_Built"])), demand)
    cure = run_heuristic_from_csv(input_dir, plan_start,
                                  write_excel=False, demand_cap=deliver)

    print(f"\n{'#'*64}\n#  STEP 6/6  Final building JIT\n{'#'*64}")
    bld = run_building_from_csv(input_dir, plan_start,
                                curing_results=cure, write_excel=False)

    log = pd.DataFrame([
        {"Step": "1 Curing (full demand)", "Description": "press/mould capacity",
         "Tyres": int(cure0["demand_fulfillment"]["Planned_Units"].sum())},
        {"Step": "2 Building envelope",     "Description": "max GT supply / SKU",
         "Tyres": int(sum(supply.values()))},
        {"Step": "3 Curing (env-capped)",   "Description": "first feasible plan",
         "Tyres": int(cure_a["demand_fulfillment"]["Planned_Units"].sum())},
        {"Step": "4 Building JIT",          "Description": "real time-phased build",
         "Tyres": int(bld_a["gt_supply"]["GT_Built"].sum())},
        {"Step": "5 Curing (final)",        "Description": "= what is actually built",
         "Tyres": int(cure["demand_fulfillment"]["Planned_Units"].sum())},
        {"Step": "6 Building (final)",       "Description": "final delivery",
         "Tyres": int(bld["gt_supply"]["GT_Built"].sum())},
    ])
    return {"cure": cure, "bld": bld, "cure0": cure0, "supply": supply, "log": log}


# ══════════════════════════════════════════════════════════════════════════════
# TIME-PHASING  — pace curing to building's daily output (no early curing)
# ══════════════════════════════════════════════════════════════════════════════
def _time_phase(cure, bld, lag_days=TIME_PHASE_LAG_DAYS):
    """
    Re-time the curing schedule so that, per SKU, the green tyres cured each day
    never exceed those built by then. Building is the master timeline; curing is
    paced to building's daily per-SKU production (optionally lagged). Totals and
    press assignments are unchanged — only the timing spreads out.
    """
    upc = Config.units_per_cycle()

    # building's daily production per SKU (the supply we must not outrun)
    b = bld["shift_schedule"].copy()
    b = b[~b["SKUCode"].isin(["CHANGEOVER", "MOULD_CLEAN"])]
    b["Date"] = pd.to_datetime(b["Date"]).dt.date
    build_daily: dict = defaultdict(lambda: defaultdict(int))
    for sku, day, qty in zip(b["SKUCode"], b["Date"], b["Qty"]):
        build_daily[sku][day] += int(qty)

    # curing press assignment per SKU, from the FULL shift schedule so that
    # continuity production (Phase 2 — not in machine_schedule) is included.
    prod = cure["shift_schedule"]
    prod = prod[~prod["SKUCode"].isin(["CHANGEOVER", "MOULD_CLEAN"])]
    agg = (prod.groupby(["SKUCode", "Machine"])
               .agg(units=("Qty", "sum"), ct=("CycleTime_min", "max"))
               .reset_index())
    sku_presses: dict = defaultdict(list)
    for _, r in agg.iterrows():
        sku_presses[r["SKUCode"]].append(
            (r["Machine"], int(r["units"]), float(r["ct"])))
    sku_total = {s: sum(u for _, u, _ in v) for s, v in sku_presses.items()}

    # distribute each SKU's daily cure quota across its assigned presses
    press_day: dict = defaultdict(list)            # (machine, cure_day) -> [(sku,q,ct)]
    for sku, daily in build_daily.items():
        presses = sku_presses.get(sku)
        tot = sku_total.get(sku, 0)
        if not presses or tot <= 0:
            continue
        for bday, units in sorted(daily.items()):
            cday = bday + timedelta(days=lag_days)
            assigned = 0
            for i, (m, pu, ct) in enumerate(presses):
                q = (units - assigned) if i == len(presses) - 1 else round(units * pu / tot)
                assigned += q
                if q > 0:
                    press_day[(m, cday)].append((sku, q, ct))

    # lay out shift-wise production rows, sequentially within each press-day
    rows = []
    for (m, day), items in press_day.items():
        cursor = datetime(day.year, day.month, day.day, Config.SHIFT_START_HOUR)
        for sku, q, ct in items:
            cycles = math.ceil(q / upc)
            block_end = cursor + timedelta(minutes=cycles * ct)
            inner, produced = cursor, 0
            while inner < block_end:
                shift, shift_end = _get_shift_fn(inner)
                slice_end = min(shift_end, block_end)
                dur = (slice_end - inner).total_seconds() / 60
                if dur <= 0:
                    inner = slice_end
                    continue
                qty = (q - produced) if slice_end == block_end else int(dur / ct) * upc
                rows.append({
                    "Date": day, "Shift": shift, "Machine": m, "SKUCode": sku,
                    "StartTime": inner, "EndTime": slice_end, "Qty": qty,
                    "CycleTime_min": round(ct, 2), "GT_Inventory": 0,
                    "Remarks": "Curing (paced to build)",
                })
                produced += qty
                inner = slice_end
            cursor = block_end

    new_shift = pd.DataFrame(rows).sort_values(["Machine", "StartTime"]) \
        if rows else pd.DataFrame(columns=["Date", "Shift", "Machine", "SKUCode",
                                           "StartTime", "EndTime", "Qty",
                                           "CycleTime_min", "GT_Inventory", "Remarks"])

    helper = JK_LP_Curing_Scheduler_v2()
    all_m  = list(cure["machine_utilization"]["Machine"])
    new_util = helper._build_util(cure["machine_schedule"], new_shift, all_m)

    out = dict(cure)
    out["shift_schedule"]      = new_shift
    out["machine_utilization"] = new_util
    return out


def _joint_sync(bld, cure):
    """Day-by-day built vs cured (cumulative) after time-phasing."""
    def daily(df):
        d = df[~df["SKUCode"].isin(["CHANGEOVER", "MOULD_CLEAN"])].copy()
        d["Date"] = pd.to_datetime(d["Date"]).dt.date
        return d.groupby("Date")["Qty"].sum().astype(int).to_dict()

    built = daily(bld["shift_schedule"])
    cured = daily(cure["shift_schedule"])
    days  = sorted(set(built) | set(cured))
    rows, cb, cc = [], 0, 0
    for d in days:
        b, c = int(built.get(d, 0)), int(cured.get(d, 0))
        cb += b; cc += c
        rows.append({"Date": d, "Built_Today": b, "Cured_Today": c,
                     "Cum_Built": cb, "Cum_Cured": cc,
                     "Lead_Units": cb - cc, "Status": "OK" if cb >= cc else "LAG"})
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# RECONCILIATION + KPIs
# ══════════════════════════════════════════════════════════════════════════════
def _reconcile(coupled):
    cure0 = coupled["cure0"]
    cure, bld = coupled["cure"], coupled["bld"]

    demand   = dict(zip(cure0["demand_fulfillment"]["SKUCode"],
                        cure0["demand_fulfillment"]["Demand"]))
    prio     = dict(zip(cure0["demand_fulfillment"]["SKUCode"],
                        cure0["demand_fulfillment"]["Priority"]))
    cure_cap = dict(zip(cure0["demand_fulfillment"]["SKUCode"],
                        cure0["demand_fulfillment"]["Planned_Units"]))
    bld_cap  = coupled["supply"]
    final_cure = dict(zip(cure["demand_fulfillment"]["SKUCode"],
                          cure["demand_fulfillment"]["Planned_Units"]))
    final_bld  = dict(zip(bld["gt_supply"]["SKUCode"], bld["gt_supply"]["GT_Built"]))

    rows = []
    for sku, dem in demand.items():
        dem = int(dem)
        cc  = int(cure_cap.get(sku, 0))
        bc  = int(bld_cap.get(sku, 0))
        fc  = int(final_cure.get(sku, 0))
        fb  = int(final_bld.get(sku, 0))
        final = min(fc, fb) if (fc and fb) else max(fc, fb)
        if final >= dem and dem > 0:
            binding = "Demand met"
        elif dem == 0:
            binding = "No demand"
        elif bc <= cc:
            binding = "Building-limited"
        else:
            binding = "Curing-limited"
        rows.append({
            "SKUCode": sku, "Priority": round(prio.get(sku, 0.0), 4),
            "Demand": dem,
            "Curing_Capacity": cc, "Building_Capacity": bc,
            "Final_Cured": fc, "Final_Built": fb,
            "Gap_vs_Demand": max(dem - final, 0),
            "Binding_Constraint": binding,
        })
    df = pd.DataFrame(rows).sort_values("Priority", ascending=False).reset_index(drop=True)
    return df


def _kpis(coupled, recon):
    cure, bld = coupled["cure"], coupled["bld"]
    cure0     = coupled["cure0"]
    demand_tot   = int(recon["Demand"].sum())
    cure_cap_tot = int(cure0["demand_fulfillment"]["Planned_Units"].sum())
    bld_cap_tot  = int(sum(coupled["supply"].values()))
    final_cure   = int(cure["demand_fulfillment"]["Planned_Units"].sum())
    final_built  = int(bld["gt_supply"]["GT_Built"].sum())
    cure_util = round(cure["machine_utilization"]["Utilization_Pct"].mean(), 1)
    bld_util  = round(bld["machine_utilization"]["Utilization_Pct"].mean(), 1)
    lag_days  = int((bld["sync_check"]["Status"] == "LAG").sum())
    n_days    = len(bld["sync_check"])
    feasible  = min(final_cure, final_built)
    return [
        ("Plan version",                 VERSION_LABEL),
        ("Run timestamp",                run_stamp()),
        ("Planning horizon (days)",      f"{Config.PLANNING_DAYS}"),
        ("Coupling steps",               f"{len(coupled['log'])}"),
        ("", ""),
        ("Original demand (tyres)",      f"{demand_tot:,}"),
        ("Curing-only capacity",         f"{cure_cap_tot:,}"),
        ("Building-only capacity",       f"{bld_cap_tot:,}"),
        ("", ""),
        ("FEASIBLE plan (cured = built)", f"{feasible:,}"),
        ("  vs demand",                  f"{feasible/demand_tot*100:.1f}%" if demand_tot else "-"),
        ("  final curing planned",       f"{final_cure:,}"),
        ("  final building built",       f"{final_built:,}"),
        ("", ""),
        ("Binding constraint",           "Building (green-tyre supply)"),
        ("Avg curing press util",        f"{cure_util}%"),
        ("Avg building machine util",    f"{bld_util}%"),
        ("Days building lags curing",    f"{lag_days} / {n_days}"),
        ("", ""),
        ("Building-limited SKUs",        f"{(recon['Binding_Constraint']=='Building-limited').sum()}"),
        ("Curing-limited SKUs",          f"{(recon['Binding_Constraint']=='Curing-limited').sum()}"),
        ("Fully demand-met SKUs",        f"{(recon['Binding_Constraint']=='Demand met').sum()}"),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATED SUMMARY WORKBOOK
# ══════════════════════════════════════════════════════════════════════════════
class IntegratedExporter(BuildingExcelExporter):
    # extend status colours for binding / sync labels
    STATUS_FC = {**ExcelExporter.STATUS_FC,
                 "Demand met": "green", "Building-limited": "amber",
                 "Curing-limited": "blue", "No demand": "lgrey",
                 "OK": "green", "LAG": "red", "CONVERGED": "green",
                 "iterating": "amber"}

    def export_integrated(self, coupled, recon, kpis):
        sub = f"{VERSION_LABEL}  |  {run_stamp()}  |  coupled curing + building plan"
        with pd.ExcelWriter(self.path, engine="openpyxl") as writer:
            # 1. Executive Summary (Metric / Value)
            df_kpi = pd.DataFrame(kpis, columns=["Metric", "Value"])
            self._sheet(writer, df_kpi, "Executive Summary",
                        "INTEGRATED PLAN — EXECUTIVE SUMMARY", sub,
                        [34, 30], name_col=1, bold_cols=(1,))

            # 2. SKU Reconciliation
            self._sheet(writer, recon, "SKU Reconciliation",
                        "PER-SKU RECONCILIATION (demand -> curing -> building)", sub,
                        [26, 10, 12, 15, 16, 13, 13, 14, 18],
                        bold_cols=(6, 7), status_col=9, name_col=1)

            # 3. Daily Plan (building vs curing)
            sync = coupled["bld"]["sync_check"]
            self._sheet(writer, sync, "Daily Plan",
                        "DAILY PLAN — GTs BUILT vs CURED (cumulative)", sub,
                        [14, 13, 13, 14, 14, 12, 10],
                        bold_cols=(6,), status_col=7, name_col=1)

            # 4. Coupling Log
            self._sheet(writer, coupled["log"], "Coupling Log",
                        "COUPLING STEPS (demand -> curing -> building envelope -> feasible)",
                        sub, [26, 26, 14], bold_cols=(3,), name_col=1)

        print(f"\n  [Export] Saved -> {self.path}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
def run_coupled_plan(input_dir="inputs", plan_start=None, output_root="output"):
    if plan_start is None:
        plan_start = Config.PLAN_DATE

    coupled = couple(input_dir, plan_start)

    # time-phase curing to building's daily output (no curing before building)
    print(f"\n{'#'*64}\n#  TIME-PHASING  curing paced to building daily supply\n{'#'*64}")
    coupled["cure"] = _time_phase(coupled["cure"], coupled["bld"])
    sync = _joint_sync(coupled["bld"], coupled["cure"])
    coupled["bld"]["sync_check"] = sync          # building book shows phased sync too
    lag = int((sync["Status"] == "LAG").sum())
    print(f"  [Phase] Days curing exceeds building supply: {lag} / {len(sync)} "
          f"(target 0)")

    recon = _reconcile(coupled)
    kpis  = _kpis(coupled, recon)

    out_dir = os.path.join(output_root, VERSION_LABEL)
    os.makedirs(out_dir, exist_ok=True)

    # final, mutually-feasible standalone workbooks
    ExcelExporter(os.path.join(out_dir, f"PCR_Curing_Schedule_{VERSION_LABEL}.xlsx")) \
        .export(coupled["cure"])
    BuildingExcelExporter(os.path.join(out_dir, f"PCR_Building_Schedule_{VERSION_LABEL}.xlsx")) \
        .export(coupled["bld"])
    IntegratedExporter(os.path.join(out_dir, f"PCR_Integrated_Plan_{VERSION_LABEL}.xlsx")) \
        .export_integrated(coupled, recon, kpis)

    _print_final(kpis, out_dir)
    return {"coupled": coupled, "reconciliation": recon, "kpis": kpis,
            "output_dir": out_dir}


def _print_final(kpis, out_dir):
    print(f"\n{'='*64}\n  INTEGRATED PLAN {VERSION_LABEL} — FINAL\n{'='*64}")
    for k, v in kpis:
        if k:
            print(f"  {k:32s}: {v}")
    print(f"{'='*64}")
    print(f"  Outputs in: {os.path.abspath(out_dir)}")
    print(f"{'='*64}")


if __name__ == "__main__":
    run_coupled_plan("inputs", plan_start=Config.PLAN_DATE)
