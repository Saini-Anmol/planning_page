"""
curing_heuristic.py — JK Tyre PCR Curing scheduler, HEURISTIC variant.

Same inputs, same Excel output as curing_LP.py — but Phase 3 (LP solve) and
Phase 4 (rounding) are replaced by a single greedy, priority-first allocator.
No scipy required.

It deliberately REUSES the tested machinery from curing_LP:
    Config, ETL, MouldTracker            (config + data loading)
    JK_LP_Curing_Scheduler_v2._prepare_skus / _build_continuity
                                          (Phase 1 + Phase 2, unchanged)
    ScheduleBuilder                       (Phase 5 — shift-wise layout)
    _build_summary / _build_util          (reporting)
    ExcelExporter                         (identical workbook format)

Only the allocation step is new (HeuristicAllocator), so the output workbook is
structurally identical to the LP scheduler's — same 5 sheets, same columns.

Run:
    python curing_heuristic.py                  # reads ./inputs, writes xlsx
    from curing_heuristic import run_heuristic_from_csv
    run_heuristic_from_csv("inputs")
"""

import math
import os
from collections import defaultdict
from datetime import datetime

import pandas as pd

from curing_LP import (
    Config,
    ETL,
    MouldTracker,
    ScheduleBuilder,
    ExcelExporter,
    JK_LP_Curing_Scheduler_v2,
)


# ══════════════════════════════════════════════════════════════════════════════
# HEURISTIC ALLOCATOR  (replaces LP_Solver + Rounder)
# ══════════════════════════════════════════════════════════════════════════════
class HeuristicAllocator:
    """
    Greedy, priority-first press-minute allocation.

    Strategy (per SKU, highest Priority first):
      1. Find the presses the SKU may run on (compatible AND mould-eligible).
      2. Rank them: a press already set to run this SKU (continuity — no
         changeover) first, then the press with the MOST spare capacity. This
         concentrates each SKU on as few presses as possible, which keeps the
         changeover count and fragmentation low.
      3. Walk that ranked list, filling whole cycles into each press until the
         SKU's demand is met or capacity runs out. A 300-min changeover is
         charged only when the press's previous SKU differs from this one.

    Output mirrors Rounder.round exactly:
        df_mach            — machine-level rows (Machine, SKUCode, Cycles, ...)
        machine_sku_order  — {machine: [sku, sku, ...]} run order for the builder
    so ScheduleBuilder / reporting / ExcelExporter consume it unchanged.
    """

    def __init__(self):
        self.avail_mins = Config.avail_mins()
        self.co_mins    = Config.CHANGEOVER_DURATION_MIN

    def _row(self, mach, sku, cycles, ct, priority):
        actual_min = cycles * ct
        return {
            "Machine":       mach,
            "SKUCode":       sku,
            "Priority":      round(priority, 4),
            "CycleTime_min": ct,
            "Cycles":        cycles,
            "Units_Planned": cycles * Config.units_per_cycle(),
            "Mins_Used":     round(actual_min, 1),
            "Days_Used":     round(actual_min / (Config.SHIFTS_PER_DAY
                                  * Config.HOURS_PER_SHIFT * 60), 2),
        }

    def allocate(
        self,
        df_lp: pd.DataFrame,
        all_machines: list,
        mould_tracker: MouldTracker,
        locked_machine_mins: dict,
        continuity_last_sku: dict | None = None,
    ) -> tuple[pd.DataFrame, dict]:
        continuity_last_sku = continuity_last_sku or {}
        upc = Config.units_per_cycle()

        # remaining capacity per press after continuity locks
        machine_cap = {
            m: max(self.avail_mins - locked_machine_mins.get(str(m), 0.0), 0.0)
            for m in all_machines
        }

        # continuity presses per SKU (for mould eligibility — they always pass)
        cont_machs_by_sku: dict[str, set] = defaultdict(set)
        for mach_str, sku in continuity_last_sku.items():
            cont_machs_by_sku[sku].add(mach_str)
            try:
                cont_machs_by_sku[sku].add(int(mach_str))
            except (ValueError, TypeError):
                pass

        machine_sku_order: dict = {}
        planned: dict = defaultdict(int)
        final: list = []
        co_count = 0

        def last_sku(m):
            order = machine_sku_order.get(m)
            if order:
                return order[-1]
            return continuity_last_sku.get(str(m))

        # SKUs by Priority desc (df_lp inherits the prep sort; re-sort to be safe)
        sku_rows = sorted(df_lp.itertuples(index=False), key=lambda r: -r.Priority)

        for row in sku_rows:
            sku   = row.SKUCode
            ct    = row.CycleTime_min
            if not ct:
                continue
            demand = int(row.Demand)
            needed_cycles = math.ceil(max(demand - planned[sku], 0) / upc)
            if needed_cycles <= 0:
                continue

            compatible = list(row.Machines)
            eligible = mould_tracker.get_eligible_machines_with_moulds(
                sku, compatible, cont_machs_by_sku.get(sku, set())
            )

            cands = [m for m in eligible if machine_cap.get(m, 0.0) >= ct]
            # continuity / same-SKU press first (CO-free), then most spare cap
            cands.sort(key=lambda m: (0 if last_sku(m) == sku else 1,
                                      -machine_cap[m]))

            for m in cands:
                if needed_cycles <= 0:
                    break
                prev = last_sku(m)
                co   = self.co_mins if (prev is not None and prev != sku) else 0.0
                avail = machine_cap[m] - co
                if avail < ct:
                    continue
                fit = int(avail / ct)
                cyc = min(needed_cycles, fit)
                if cyc <= 0:
                    continue
                machine_cap[m] -= cyc * ct + co
                if co > 0:
                    co_count += 1
                planned[sku]  += cyc * upc
                needed_cycles -= cyc
                machine_sku_order.setdefault(m, []).append(sku)
                final.append(self._row(m, sku, cyc, ct, row.Priority))

        df_mach = pd.DataFrame(final)
        residual = sum(machine_cap.values())
        units = int(df_mach["Units_Planned"].sum()) if not df_mach.empty else 0
        print(f"  [Heuristic] Rows: {len(df_mach)} | Units: {units:,} | "
              f"Changeovers: {co_count} | "
              f"CO time: {co_count * self.co_mins / 60:.1f} hrs | "
              f"Residual press-mins: {residual:,.0f}")
        return df_mach, machine_sku_order


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR — reuse the LP scheduler's phases, swap in the heuristic
# ══════════════════════════════════════════════════════════════════════════════
class HeuristicCuringScheduler(JK_LP_Curing_Scheduler_v2):
    """Same pipeline as the LP scheduler; Phase 3+4 = HeuristicAllocator."""

    def run(
        self,
        df_demand:     pd.DataFrame,
        df_cycles:     pd.DataFrame,
        df_allow:      pd.DataFrame,
        df_gt:         pd.DataFrame,
        mould_tracker: MouldTracker,
        df_running:    pd.DataFrame = None,
        plan_start:    datetime     = None,
    ) -> dict:
        if plan_start is None:
            plan_start = datetime.now().replace(
                hour=Config.SHIFT_START_HOUR, minute=0, second=0, microsecond=0
            )

        print("\n" + "=" * 64)
        print("  JK Tyre PCR Curing — HEURISTIC Scheduler")
        print(f"  Plan start  : {plan_start:%Y-%m-%d %H:%M}")
        print(f"  Horizon     : {Config.PLANNING_DAYS} days "
              f"({self.avail_mins:,.0f} min/press)")
        print(f"  Units/cycle : {Config.units_per_cycle()} "
              f"({Config.MOULDS_PER_PRESS} moulds x {Config.CAVITIES_PER_MOULD} cavity)")
        print(f"  Changeover  : {Config.CHANGEOVER_DURATION_MIN} min | "
              f"Max/shift: {Config.MAX_CHANGEOVERS_PER_SHIFT}")
        print(f"  Mould clean : {Config.CLEANING_DURATION_MIN} min every "
              f"{Config.units_per_cleaning_cycle():,} units")
        print("=" * 64)

        print("\n[Phase 1] Preparing SKU table...")
        df_valid, df_all, all_machines = self._prepare_skus(
            df_demand, df_cycles, df_allow, df_gt, mould_tracker, df_running
        )

        print("\n[Phase 2] Building continuity blocks...")
        continuity_rows, locked_mins, demand_remainder, continuity_last_sku = \
            self._build_continuity(df_running, df_valid, df_gt, plan_start)

        # remaining demand after continuity (same logic as the LP orchestrator)
        df_lp = df_valid.copy()
        for sku, remainder in demand_remainder.items():
            mask = df_lp["SKUCode"] == sku
            if remainder == 0:
                df_lp = df_lp[~mask]
            else:
                df_lp.loc[mask, "Demand"] = remainder
                df_lp.loc[mask, "Demand_Mins"] = (
                    math.ceil(remainder / Config.units_per_cycle())
                    * df_lp.loc[mask, "CycleTime_min"].values[0]
                )
        print(f"  [Input] {len(df_lp)} SKUs | "
              f"Remaining demand: {df_lp['Demand'].sum():,.0f} units")

        print("\n[Phase 3+4] Heuristic allocation...")
        allocator = HeuristicAllocator()
        df_mach, machine_sku_order = allocator.allocate(
            df_lp, all_machines, mould_tracker, locked_mins, continuity_last_sku
        )

        print("\n[Phase 5] Building shift-wise schedule...")
        builder  = ScheduleBuilder(plan_start)
        df_shift = builder.build(df_mach, machine_sku_order, df_gt,
                                 continuity_rows, continuity_last_sku)

        df_summary = self._build_summary(df_all, df_mach, df_shift)
        df_util    = self._build_util(df_mach, df_shift, all_machines)
        self._print_results(df_summary, df_util, df_shift)

        return {
            "machine_schedule":    df_mach,
            "shift_schedule":      df_shift,
            "demand_fulfillment":  df_summary,
            "machine_utilization": df_util,
            "mould_tracker":       mould_tracker.summary,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT — same CSV folder as run_from_csv
# ══════════════════════════════════════════════════════════════════════════════
def run_heuristic_from_csv(
    input_dir:   str = "inputs",
    plan_start:  datetime = None,
    output_dir:  str = "output",
    output_name: str = "PCR_Curing_Heuristic_Schedule.xlsx",
    write_excel: bool = True,
    demand_cap:  dict = None,
) -> dict:
    def p(fname: str) -> str:
        return os.path.join(input_dir, fname)

    output_path = os.path.join(output_dir, output_name)

    required = ["demand.csv", "cycle_times.csv", "machine_allowable.csv",
                "gt_inventory.csv", "mould_master.csv"]
    missing = [f for f in required if not os.path.exists(p(f))]
    if missing:
        raise FileNotFoundError(
            f"Missing required input(s) in '{input_dir}': {missing}"
        )

    print(f"\n[Phase 0] ETL from CSV folder: {os.path.abspath(input_dir)}")
    df_demand  = ETL.load_demand_from_csv(p("demand.csv"))
    if demand_cap is not None:
        # Coupled re-plan: cap each SKU's curing demand to what building can
        # actually supply, so curing never plans more GTs than exist.
        df_demand = df_demand.copy()
        df_demand["Quantity"] = [
            min(int(q), int(demand_cap.get(s, q)))
            for s, q in zip(df_demand["SKUCode"], df_demand["Quantity"])
        ]
        df_demand = df_demand[df_demand["Quantity"] > 0].reset_index(drop=True)
    df_cycles  = ETL.load_cycle_times_from_csv(p("cycle_times.csv"))
    df_allow   = ETL.load_machine_allowable_from_csv(p("machine_allowable.csv"))
    df_gt      = ETL.load_gt_inventory_from_csv(p("gt_inventory.csv"))
    df_mould_m = ETL.load_mould_master_from_csv(p("mould_master.csv"))

    running_csv = p("running_moulds.csv")
    df_running  = (ETL.load_running_moulds_from_csv(running_csv)
                   if os.path.exists(running_csv) else None)

    tracker = MouldTracker()
    tracker.load_from_df(
        df_mould_m,
        df_running if df_running is not None
        else pd.DataFrame(columns=["Machine", "SKUCode", "MouldNos",
                                    "MouldLife_remaining"]),
    )

    scheduler = HeuristicCuringScheduler()
    results   = scheduler.run(df_demand, df_cycles, df_allow, df_gt,
                              tracker, df_running, plan_start)
    if write_excel:
        os.makedirs(output_dir, exist_ok=True)
        ExcelExporter(output_path).export(results)
    return results


if __name__ == "__main__":
    run_heuristic_from_csv("inputs", plan_start=Config.PLAN_DATE)
