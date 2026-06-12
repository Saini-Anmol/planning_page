# PCR Curing Scheduler — Heuristic Variant — Explainer

**File:** [curing_heuristic.py](curing_heuristic.py)
**Companion to:** the LP scheduler [curing_LP.py](curing_LP.py) — see [curing_LP_explainer.md](curing_LP_explainer.md).
**Output:** identical Excel format to the LP scheduler (same 5 sheets, same columns).

This scheduler answers the same question as the LP version — *which SKU runs on which press, for how long, over the 31-day horizon* — but replaces the linear-programming core with a **fast greedy heuristic**. No `scipy` required.

---

## 1. Why a heuristic, and how it relates to the LP version

The LP scheduler ([curing_LP.py](curing_LP.py)) solves a globally optimal press-minute allocation, then rounds it to integer cycles. That's powerful but:
- needs `scipy` and builds large (dense) constraint matrices,
- is heavier than necessary when a good-enough, explainable plan is acceptable.

The heuristic produces a schedule in one greedy pass, is trivial to reason about ("highest-priority demand grabs the emptiest compatible press first"), and runs in well under a second on the full SKU set.

**Key design choice — maximum reuse.** Everything that is *not* the optimiser is shared with the LP code, so the two schedulers stay in lock-step and the output workbook is structurally identical:

| Stage | LP scheduler | Heuristic scheduler |
|---|---|---|
| Phase 0 — ETL | `ETL.load_*_from_csv` | **same** |
| Phase 1 — Prepare SKUs | `_prepare_skus` | **same** (inherited) |
| Phase 2 — Continuity | `_build_continuity` | **same** (inherited) |
| **Phase 3+4 — Allocation** | `LP_Solver` + `Rounder` | **`HeuristicAllocator`** ← the only new code |
| Phase 5 — Shift layout | `ScheduleBuilder` | **same** |
| Reporting | `_build_summary` / `_build_util` | **same** (inherited) |
| Excel export | `ExcelExporter` | **same** |

`HeuristicCuringScheduler` subclasses `JK_LP_Curing_Scheduler_v2` and overrides only `run()` to swap the LP+Rounder step for the allocator. Same `Config`, same press geometry (units-per-cycle correction **C1** applies here too), same changeover/cleaning rules.

---

## 2. The heuristic allocator (the heart of it)

[curing_heuristic.py](curing_heuristic.py) — `HeuristicAllocator.allocate()`. A **greedy, priority-first, first-fit** allocation.

It runs *after* continuity (Phase 2), so it only fills the press capacity that continuity hasn't already locked, and it only schedules the demand continuity didn't already cover.

### Algorithm

```
remaining_cap[press] = horizon_minutes − continuity_locked[press]

for each SKU, in descending Priority order:
    needed_cycles = ceil(remaining_demand / units_per_cycle)     # units→cycles
    candidates    = presses that are compatible AND mould-eligible
                    and still have ≥ 1 cycle of capacity
    rank candidates by:
        (1) is the press already set to run this SKU?  (continuity → no changeover)
        (2) then: most spare capacity first
    walk the ranked candidates:
        changeover = 300 min  if the press's previous SKU ≠ this SKU  else 0
        fit        = floor((capacity − changeover) / cycle_time)
        take       = min(needed_cycles, fit)         # whole cycles only
        commit 'take' cycles; subtract (take·cycle_time + changeover) from capacity
        record the press in this SKU's run order
        stop early once the SKU's demand is met
```

### Why these rules

- **Priority-first** — the highest-`Priority` SKUs claim capacity before lower ones, so scarce press-time goes to what matters most. SKUs that don't fit surface as PARTIAL/UNMET in the report (never silently dropped).
- **Same-SKU/continuity press first** — extends a press already running the SKU with **zero changeover**, the single biggest lever on changeover count.
- **Most-spare-capacity next** — concentrates a SKU onto as few presses as possible (fill the emptiest first), which limits fragmentation and therefore changeovers.
- **Whole cycles only** — a press cures in discrete cycles; `units_per_cycle = MOULDS_PER_PRESS × CAVITIES_PER_MOULD = 2` (correction **C1**).
- **Honest changeover accounting** — 300 min is charged to a press's budget exactly when its SKU changes, so a press is never over-committed.

### Output (identical contract to `Rounder.round`)

- `df_mach` — machine-level rows: `Machine, SKUCode, Priority, CycleTime_min, Cycles, Units_Planned, Mins_Used, Days_Used`.
- `machine_sku_order` — `{press: [sku, sku, …]}` run order, consumed by `ScheduleBuilder` to place changeovers and cleanings.

Because this matches what the rounder emitted, **Phase 5 and all reporting/export run unchanged.**

---

## 3. What it does NOT do (versus the LP)

- **No global optimisation.** Greedy choices are locally good but not provably optimal; a clever reshuffle might fit slightly more demand or cut a few changeovers. In practice the gap is small and the plan is far easier to explain.
- **No LP-style fractional smearing.** It never splits a SKU across many presses for a theoretical optimum; it deliberately concentrates.
- **No top-up pass.** The LP rounder floors then tops up; the heuristic allocates the full demand directly in priority order, so a separate top-up isn't needed.

Everything else — continuity, mould eligibility (permissive/strict), mould cleaning every `units_per_cleaning_cycle` units, the `MAX_CHANGEOVERS_PER_SHIFT` cap, shift A/B/C splitting, horizon truncation — is **inherited unchanged** from the LP code path.

---

## 4. Inputs and output

**Inputs** — exactly the same `inputs/` CSV folder the LP `run_from_csv` uses (see [curing_LP_explainer.md](curing_LP_explainer.md) §5):
`demand.csv` (`SKUCode, Quantity, Priority`), `cycle_times.csv`, `machine_allowable.csv`, `gt_inventory.csv`, `mould_master.csv`, and optional `running_moulds.csv`. Generate the masters with [dataloader.py](dataloader.py).

**Output** — `output/PCR_Curing_Heuristic_Schedule.xlsx`, the same styled sheets as the LP scheduler:
`Demand Fulfillment`, `Machine Schedule`, `Shift Schedule`, `Machine Utilization`, `Mould Tracker`, and **`Daily Curing`** (total tyres cured per day, split by shift A/B/C, with a running cumulative). The workbook is written into an `output/` folder.

---

## 5. How to run

```bash
# masters already generated into ./inputs by dataloader.py, demand.csv in place
python curing_heuristic.py
```
or from code:
```python
from curing_heuristic import run_heuristic_from_csv
results = run_heuristic_from_csv("inputs")          # returns the 5 DataFrames + writes xlsx
```
`run_heuristic_from_csv(input_dir, plan_start=None, output_path=...)` mirrors `run_from_csv`. `plan_start` defaults to today at 07:00; the `__main__` block uses `Config.PLAN_DATE`.

**Dependencies:** `numpy`, `pandas`, `openpyxl`. (No `scipy` — that's only the LP path.)

---

## 6. Example run (current `inputs/`)

A full run on the live-pulled masters + the converted demand produced:

```
Total demand    : 718,128
Units planned   : 692,024  (96.4%)
Gap             :  26,104
Avg press util  : 88.5%
Changeovers     :     117
Mould cleans    :      69
Fully met SKUs  :      78
Partial SKUs    :      24
Unmet SKUs      :       7
Unschedulable   :      11
```

(The 11 unschedulable SKUs lack a cycle time, a machine mapping, or a compatible mould — each is reported with a `Skip_Reason` on the Demand-Fulfillment sheet, identical to the LP scheduler's treatment.)

---

## 7. Mental model in one line

> Continuity keeps the presses doing what they already do; then, highest-priority SKU first, drop each SKU's demand into the emptiest compatible press it can legally use — reusing the press it's already on when possible to avoid a changeover — filling whole cycles until demand is met or the presses are full; then lay it out shift-by-shift and write the same workbook the LP scheduler does.
