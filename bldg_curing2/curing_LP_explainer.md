# PCR Curing LP Scheduler — Explainer

**File:** [curing_LP.py](curing_LP.py)
**Product:** JK Tyre BTP — PCR (Passenger Car Radial) tyre curing schedule generator, version 4.
**Corrections:** see [CORRECTION_LOG.md](CORRECTION_LOG.md) for fixes applied after v4 (currently C1, C2).

This document explains *what the program does*, *how it does it*, and *how to run it*. It is written for two audiences: a planner who wants to understand the output, and an engineer who needs to maintain or extend the code.

---

## 1. What problem does it solve?

A tyre plant has a fleet of curing **presses** (machines) and a backlog of **SKUs** (tyre sizes/codes) to produce over a planning horizon (default **31 days**). Each SKU:

- can only be cured on certain **compatible presses**,
- needs a specific **mould** mounted (each press holds `MOULDS_PER_PRESS = 2` moulds, each mould has `CAVITIES_PER_MOULD = 1` cavity → `1 × 2 = 2` tyres produced per cure cycle — see correction **C1**),
- has a fixed **cycle time** (minutes per cure),
- has a **demand quantity** and a **priority score**.

Switching a press from one SKU to another costs a **changeover** (`CHANGEOVER_DURATION_MIN = 300` min = 5 hrs). Moulds wear out and must be **cleaned** every `NEW_MOULD_LIFE × units_per_cycle = 3000 × 2 = 6,000` units (`CLEANING_DURATION_MIN = 120` min).

The goal: **allocate press-minutes to SKUs so that the highest-priority demand is met, capacity is respected, and changeover waste is minimised.** The core allocation is solved as a **Linear Program (LP)**, then converted into a concrete, shift-by-shift schedule and exported to a formatted Excel workbook.

---

## 2. The five-phase architecture

The program runs as a pipeline. The orchestrator is `JK_LP_Curing_Scheduler_v2.run()` ([curing_LP.py:1227](curing_LP.py#L1227)).

```
Phase 0  ETL          Load & clean inputs (DB or Excel)
Phase 1  Prepare      Build the SKU table, flag what is schedulable
Phase 2  Continuity   Keep presses running their CURRENT mould/SKU first
Phase 3  LP Solve     Globally optimal continuous allocation of press-minutes
Phase 4  Rounding     Convert fractional LP minutes to whole integer cycles
Phase 5  Build + Export  Lay out shift-wise rows; write the Excel file
```

Each phase is a class. Data flows one way: DataFrames out of one phase feed the next.

---

## 3. Configuration — the `Config` class

[curing_LP.py:73](curing_LP.py#L73). All tunable constants live here. Key ones:

| Setting | Default | Meaning |
|---|---|---|
| `PLANNING_DAYS` | 31 | Length of the horizon |
| `SHIFTS_PER_DAY` / `HOURS_PER_SHIFT` | 3 / 8 | Three 8-hour shifts (A/B/C) |
| `SHIFT_START_HOUR` | 7 | Shift A starts at 07:00 |
| `CAVITIES_PER_MOULD` / `MOULDS_PER_PRESS` | 1 / 2 | Press geometry → **2** units per cure cycle (C1) |
| `NEW_MOULD_LIFE` | 3000 | Cures a fresh mould survives before cleaning |
| `CHANGEOVER_DURATION_MIN` | 300 | Time lost switching SKUs on a press |
| `CLEANING_DURATION_MIN` | 120 | Mould cleaning downtime |
| `LOAD_UNLOAD_BUFFER_MIN` | 2.3 | Handling time added to raw cure time |
| `PRESS_EFFICIENCY` | 0.9 | De-rates effective cycle time |
| `MAX_CHANGEOVERS_PER_SHIFT` | 5 | Caps changeovers scheduled in any one shift |
| `CHANGEOVER_PENALTY_WEIGHT` | 0.01 | LP nudge against fragmenting a SKU across presses |
| `PERMISSIVE_MOULD_ELIGIBILITY` | True | See §4 — counts movable moulds |
| `PLAN_DATE` | 2026-05-01 07:00 | Schedule start used by the DB entry point |

Helper methods:
- `avail_mins()` → total minutes one press has in the horizon (`31 × 3 × 8 × 60 = 44,640`).
- `units_per_cycle()` → tyres cured per press cycle (`CAVITIES_PER_MOULD × MOULDS_PER_PRESS = 2`). Added in correction **C1** as the single source of truth for every cycle⇄unit conversion.
- `units_per_cleaning_cycle()` → units between mould cleanings (`NEW_MOULD_LIFE × units_per_cycle() = 6,000`).

**Effective cycle time** is derived in ETL as `round((raw_cure + 2.3) / 0.9)` — i.e. raw cure plus load/unload buffer, inflated for 90% efficiency.

---

## 4. Mould eligibility — the `MouldTracker`

[curing_LP.py:127](curing_LP.py#L127). A ledger keyed by mould ID. Each entry records which SKUs the mould can make (`compatible_skus`), its remaining life, and which press it is currently locked to (`assigned_machine`, `None` = free).

It answers two questions used to decide whether a SKU may go on a press:

- **`can_assign(sku)`** — *strict*: are there ≥ 2 **free** moulds for this SKU right now?
- **`can_schedule(sku)`** — *permissive*: are there ≥ 2 **total** moulds (free or locked) for this SKU anywhere?

`get_eligible_machines_with_moulds(sku, candidates, continuity_machines)` returns the presses a SKU is allowed on:

- **Permissive mode** (`PERMISSIVE_MOULD_ELIGIBILITY = True`, the default): if enough physical moulds exist *anywhere*, allow **all** compatible presses — this assumes an operator can physically move a mould from an idle-eligible press. This is the v4 fix that lets idle presses be used even when a SKU's moulds are currently mounted elsewhere.
- **Strict mode:** only continuity presses, plus any press where ≥ 2 moulds are actually free.

Continuity presses (the press already running this SKU) **always** pass eligibility.

`assign_moulds` picks the highest-life free moulds and locks them; `release_moulds` frees them. The `summary` property produces the "Mould Tracker" Excel sheet.

---

## 5. Inputs and their sources

The scheduler consumes **six datasets** plus a few run parameters. Every dataset can be supplied three ways — live from **MySQL** (`run_from_database` → `ETL.load_*`), from **Excel files** (`run_from_excel` → `ETL.load_*_from_excel`), or from a **CSV folder** (`run_from_csv` → `ETL.load_*_from_csv`, populated by `dataloader.py`). Five are required; running moulds is optional (but needed for continuity).

> The CSV folder is the recommended workflow: run `dataloader.py` once to pull the masters from the DB into `inputs/*.csv`, add your own `demand.csv`, then `run_from_csv("inputs")`. Canonical CSV filenames: `demand.csv`, `cycle_times.csv`, `machine_allowable.csv`, `gt_inventory.csv`, `mould_master.csv`, `running_moulds.csv`.

### Data inputs

| # | Input | DB source table | Default Excel file | Key columns (as used) | What it drives |
|---|---|---|---|---|---|
| 1 | **Demand** | a local **CSV** (`demand_csv` arg) grouped per SKU | `Demand_for_Curing_Schedule3_pcr.xlsx` | `SKUCode`, `Quantity` (from `Updated_Requirement`, summed), `Priority` (from `ConsolidatedPriorityScore`, max) | How much of each SKU to make and its priority; `Quantity ≤ 0` dropped |
| 2 | **Cycle times** | `Master_Curing_Design_CycleTime` | `Master_Curing_Design_CycleTime_pcr.xlsx` | `SKUCode` (`Sapcode`), `Cure Time` → `CycleTime_min` = `round((raw + 2.3) / 0.9)` | Minutes per cure cycle |
| 3 | **Machine allowable** | `Master_Curing_Allowable_Machines_source` | `curing_pcr_machine_allowable.xlsx` | `SKU Code`, one yes/no column per press → `Machines` (list of press IDs) | Which presses each SKU may run on |
| 4 | **GT inventory** | `gt_inventory_manual` | `GT_Inventory_pcr.xlsx` | `sizeCode` → `SKUCode`, `gtInventory` → `GT_Inventory` | Green-tyre stock on hand (carried into schedule rows) |
| 5 | **Mould master** | `Master_Mapping_Mould_SKU` (`Active Flag = True`) | `Master_Mapping_Mould_SKU.xlsx` | `MouldNo`/`Mould`, `Matl.Code` | Mould↔SKU compatibility — populates the `MouldTracker` ledger |
| 6 | **Running moulds** *(optional)* | `Daily_Running_Moulds` + `Master_WC_Master` | *(none by default — pass `running_path`)* | `Machine`, `SKUCode`, `MouldNos`, `MouldLife_remaining` (= `3000 − used`, floored at 0), `Num_Moulds` | Continuity — what each press is curing **right now**, kept running before the LP allocates |

### Run parameters

- **`plan_start`** — schedule start datetime. DB entry point uses `Config.PLAN_DATE = 2026-05-01 07:00`; defaults to *today* at 07:00 if omitted.
- **`demand_csv`** (DB mode only) — path to the demand CSV. Currently hardcoded and machine-specific at [curing_LP.py:1583](curing_LP.py#L1583) (`C:\Users\Pranjay\Downloads\Book4(Sheet4).csv`).
- **`output_path`** — destination Excel workbook.
- **`Config.*` constants** ([curing_LP.py:73](curing_LP.py#L73)) — horizon, shift layout, press geometry (`MOULDS_PER_PRESS`, `CAVITIES_PER_MOULD`), changeover/cleaning durations, mould-eligibility policy, and DB credentials.

### How they combine

`Demand` (1) is the spine. Each SKU is joined to its cycle time (2), eligible presses (3), GT inventory (4), and mould availability (5). Running moulds (6) carve out **continuity** first; the LP then fills the remaining press-time. A SKU survives into the plan only if it has **all three** of: a cycle time, ≥1 eligible press, and ≥`MOULDS_PER_PRESS` compatible moulds — otherwise it is dropped with a `Skip_Reason`.

> Note: in DB mode the five master datasets come from MySQL, but **demand is always a local CSV** — so that file path is effectively a required input too.

---

## 6. Phase 0 — ETL (`ETL` class)

[curing_LP.py:278](curing_LP.py#L278). Loads the six inputs above, from either a **MySQL database** (`load_*` instance methods, run via `run_from_database`) or **Excel files** (`load_*_from_excel` static methods, run via `run_from_excel`):

| Data | Columns produced | Notes |
|---|---|---|
| **Demand** | `SKUCode, Quantity, Priority` | Grouped/summed per SKU; rows with 0 qty dropped |
| **Cycle times** | `SKUCode, CycleTime_min` | Computed with the efficiency formula above |
| **Machine allowable** | `SKUCode, Machines` (list of press IDs) | DB version pivots "yes/no" columns into a list |
| **GT inventory** | `SKUCode, GT_Inventory` | Green-tyre stock on hand |
| **Running moulds** | `Machine, SKUCode, MouldNos, MouldLife_remaining, Num_Moulds` | What each press is *currently* curing — drives continuity |
| **Mould master** | mould↔SKU compatibility | Feeds the `MouldTracker` ledger |

The DB methods also dump each table to a `load_*.xlsx` file as a side effect (useful for debugging / re-running offline).

> Note: running-mould life is computed as `3000 − used`, floored at 0, so it represents *remaining* cures.

---

## 7. Phase 1 — Prepare SKU table (`_prepare_skus`)

[curing_LP.py:932](curing_LP.py#L932). Joins demand, cycle time, machine list, GT inventory, and mould availability into one table. For each SKU it computes:

- `Demand_Mins` = `ceil(qty / cavities_per_mould) × cycle_time` — total press-minutes the SKU needs.
- `Presses_Needed` = `Demand_Mins / avail_mins` — fractional presses required.
- `Schedulable` — true only if the SKU has a cycle time **and** at least one compatible press **and** a usable mould (a SKU already running counts automatically; otherwise `can_schedule`/`can_assign` per the policy).
- `Skip_Reason` — why an unschedulable SKU was dropped (`No cycle time` / `No machine mapping` / `No compatible mould available`).

Output: `df_valid` (schedulable only, sorted by priority then by fewest eligible machines — scarce-flexibility SKUs first) and `df_all` (everything, for the fulfillment report). `all_machines` is the sorted union of every press that appears in any SKU's allowable list.

---

## 8. Phase 2 — Continuity (`_build_continuity`)

[curing_LP.py:991](curing_LP.py#L991). **Presses already running a SKU keep running it first**, before the LP touches them. This avoids a pointless changeover at t=0.

For each currently-running SKU:
1. Gather the presses running it, compute each press's max producible units over the horizon.
2. Allocate the SKU's demand across those presses (proportional to capacity; the last press absorbs the remainder). If the group can't cover demand, the shortfall (`demand_remainder`) is handed to the LP.
3. Emit **continuity production rows** block-by-block, inserting a `MOULD_CLEAN` row whenever a mould hits its life limit.
4. Record `locked_mins[machine]` — minutes that press is committed to continuity (so the LP sees reduced capacity), and `continuity_last_sku[machine]` — the SKU left mounted (so later phases avoid a spurious changeover when the LP keeps the same SKU).

The big hard-coded `machines = [4401, …]` list at [curing_LP.py:1128](curing_LP.py#L1128) ensures **every** real press ID exists as a key in `locked_mins` (defaulting idle presses to 0 locked minutes) so the LP and rounder never KeyError on a press that had no continuity.

The remainder logic then trims `df_lp`: SKUs fully covered by continuity are removed from the LP; partially covered SKUs have their demand reduced to the remainder ([curing_LP.py:1263](curing_LP.py#L1263)).

---

## 9. Phase 3 — LP Solve (`LP_Solver`)

[curing_LP.py:448](curing_LP.py#L448). The heart of the optimisation, solved with SciPy's `linprog` (HiGHS solver).

**Decision variables** (`n_vars = S×M + S`):
- `x[s, m]` — minutes SKU *s* runs on press *m* (one per SKU×machine pair).
- `u[s]` — **unmet** demand-minutes for SKU *s* (a slack variable, one per SKU).

**Objective (minimise):**
```
Σ u[s]                              ← primary: minimise total unmet demand
+ penalty × Σ x[s,m]/Demand_Mins[s] ← tiny tie-breaker: prefer fewer, fuller presses
```
The penalty (`0.01`) is small enough not to override demand fulfilment, but it discourages smearing one SKU thinly across many presses (which would later cause more changeovers).

**Constraints:**
- **Capacity** (one per press): `Σ_s x[s,m] ≤ avail_mins − locked_mins[m]`.
- **Demand** (one per SKU): `Σ_m x[s,m] + u[s] ≥ Demand_Mins[s]` (written as `−Σx − u ≤ −Demand_Mins`).

**Bounds / eligibility:** `x[s,m]` is forced to `0` if press *m* is not mould-eligible for SKU *s*; otherwise capped at the SKU's demand-minutes. This is where `MouldTracker.get_eligible_machines_with_moulds` gates the solution.

The result is a **continuous** (fractional) allocation: globally optimal, but it may say "run SKU X for 1,234.7 minutes on press 5" — not yet realisable.

---

## 10. Phase 4 — Rounding (`Rounder`)

[curing_LP.py:546](curing_LP.py#L546). Turns fractional LP minutes into whole **integer cycles**, while accurately accounting for changeover time. Two passes:

**Pass A — floor & charge changeovers per press:**
- Floor each `x[s,m]` to whole cycles (`int(mins / cycle_time)`).
- Group assignments by press. Order them: continuity SKU first (if still present), then by priority.
- Walk the list charging a changeover (300 min) **only for SKUs actually kept** on that press. If capacity runs out and a SKU is dropped, **no** changeover is charged for it — this is the v4 fix that recovers capacity the old version wasted by reserving CO up front for SKUs it then trimmed.

**Pass B — greedy top-up:**
- Some demand may still be unmet after flooring. Walk SKUs by priority; for each, find compatible presses with spare capacity (most-idle first) and add extra cycles, charging a changeover only if the press's last SKU differs.

Output: `df_sched` (machine-level rows: machine, SKU, cycles, units, minutes, days) and `machine_sku_order` (the run order per press, used by the builder to place changeovers). The console prints total units, total changeover hours, changeover count, and residual press-minutes.

---

## 11. Phase 5 — Build the shift schedule (`ScheduleBuilder`)

[curing_LP.py:726](curing_LP.py#L726). Converts the machine-level plan into concrete **time-stamped rows**, one per (shift slice / event).

For each press, starting from when its continuity work ends:
1. Deduplicate consecutive same-SKU entries (top-up can append duplicates) to avoid fake changeovers.
2. Insert a `CHANGEOVER` row when the SKU changes — placed in the next shift that still has changeover budget (`MAX_CHANGEOVERS_PER_SHIFT`, via `_next_co_slot`).
3. Lay down production with `_split_block`, which:
   - splits a production block at **shift boundaries** (so each row sits in one shift A/B/C),
   - inserts a `MOULD_CLEAN` row every `units_per_cleaning_cycle` (6,000) units,
   - never runs past `plan_end`.

Continuity rows (from Phase 2) are split into shifts by `con_split_into_shifts` and merged in. Everything is sorted by machine then start time.

`_get_shift_fn` ([curing_LP.py:1504](curing_LP.py#L1504)) maps a timestamp to shift **A** (07:00–15:00), **B** (15:00–23:00), or **C** (23:00–07:00) and returns that shift's end time.

Special SKU codes used as event markers in the output: `CHANGEOVER`, `MOULD_CLEAN`.

---

## 12. Reporting & Excel export (`ExcelExporter`)

[curing_LP.py:1311](curing_LP.py#L1311). Builds a styled `.xlsx` workbook with six sheets:

| Sheet | Source | Content |
|---|---|---|
| **Demand Fulfillment** | `_build_summary` | Per-SKU demand vs planned, gap, % met, status (FULLY MET / PARTIAL / UNMET / UNSCHEDULABLE), skip reason |
| **Machine Schedule** | `df_mach` | Per press×SKU: cycles, units, minutes, days |
| **Shift Schedule** | `df_shift` | The detailed time-stamped rows (the operational schedule) |
| **Machine Utilization** | `_build_util` | Used vs idle minutes, utilisation %, cycles, units per press |
| **Mould Tracker** | `tracker.summary` | Each mould's SKUs, life, and assignment |
| **Daily Curing** | `_daily_table(df_shift)` | Total tyres cured per day, split by shift A/B/C, with a running cumulative and a TOTAL row (excludes changeover & cleaning rows) |

A KPI banner (demand, planned, gap, fulfillment %, avg utilisation, changeover/cleaning counts) is written across the top of the main sheets. Status and utilisation cells are colour-coded (green/amber/red).

---

## 13. How to run it

Three entry points at the bottom of the file.

### From a CSV folder — `run_from_csv()` (recommended workflow)
Generate the master CSVs once with the **`dataloader.py`** helper, drop in your own `demand.csv`, then run:
```bash
python dataloader.py                 # writes ./inputs/*.csv from the DB
#   copy inputs/demand_TEMPLATE.csv -> inputs/demand.csv and fill it in
python -c "from curing_LP import run_from_csv; run_from_csv('inputs')"
```
`dataloader.py` pulls the five masters from MySQL (reusing `ETL`'s SQL) and writes consumption-ready CSVs; `run_from_csv(input_dir)` reads them back — parsing the list columns (`Machines`, `MouldNos`) and forcing `SKUCode` to string so mould↔SKU matching works — and runs all five phases. `running_moulds.csv` is optional. The expected files and the **demand format** (`SKUCode, Quantity, Priority`) are documented at the bottom of `dataloader.py`.

### From Excel files — `run_from_excel()` ([curing_LP.py:1522](curing_LP.py#L1522))
No database needed. Point it at the five input workbooks:
```python
from curing_LP import run_from_excel
results = run_from_excel(
    demand_path  = "Demand_for_Curing_Schedule3_pcr.xlsx",
    cycles_path  = "Master_Curing_Design_CycleTime_pcr.xlsx",
    allow_path   = "curing_pcr_machine_allowable.xlsx",
    gt_path      = "GT_Inventory_pcr.xlsx",
    mould_path   = "Master_Mapping_Mould_SKU.xlsx",
    running_path = None,                 # optional: current running moulds
    output_path  = "PCR_Curing_LP_v4_Schedule.xlsx",
)
```

### From the database — `run_from_database()` ([curing_LP.py:1549](curing_LP.py#L1549))
This is what `if __name__ == "__main__"` runs. It connects to MySQL using the `Config.DB_*` credentials, pulls every table live, and needs a demand CSV path:
```python
results = run_from_database(
    demand_csv = r"C:\path\to\demand.csv",
    plan_start = Config.PLAN_DATE,
)
```
Requires `sqlalchemy` + `pymysql`. If `sqlalchemy` is missing the function raises and tells you to use the Excel path instead.

**Dependencies:** `numpy`, `pandas`, `scipy`, `openpyxl`, and (for DB mode / `dataloader.py`) `sqlalchemy` + a MySQL driver.

All three entry points return a `results` dict (the five DataFrames) *and* write the Excel file.

---

## 14. Version history (from the file header)

- **v4** — (1) Rounder now charges changeover only for SKUs actually kept, recovering wasted capacity; (2) permissive mould eligibility lets idle presses be used when moulds can be moved; (3) removed debug code and a redundant hardcoded-machines hack inside continuity.
- **v3** — continuity→LP changeover insertion, continuity-aware mould eligibility, rounder capacity-key fix, top-up changeover accounting.

### Corrections applied after v4 — see [CORRECTION_LOG.md](CORRECTION_LOG.md)

- **C1 — units-per-cycle** *(production estimate, high severity).* The LP/rounder/builder path counted output using `CAVITIES_PER_MOULD` alone, dropping the `MOULDS_PER_PRESS` factor, and `CAVITIES_PER_MOULD` was mis-set to `2`. A PCR press is 2 moulds × 1 cavity = **2 tyres/cycle**. Fixed by setting `CAVITIES_PER_MOULD = 1`, adding `Config.units_per_cycle()` as the one true cycle⇄unit conversion, and routing all eight conversion sites through it. The LP path, the continuity path, and the cleaning cadence now all agree at 2 units/cycle.
- **C2 — zero-width space** *(compilation, high severity).* A stray `U+200B` before the module docstring caused a `SyntaxError`; removed.

Source lines changed by a correction carry an inline `# [C1]`-style tag for traceability.

---

## 15. Mental model in one paragraph

Load the data; let presses keep doing what they're already doing (continuity); ask an LP to optimally fill the *remaining* press-time with the highest-priority unmet demand, respecting which moulds can go where; round that ideal answer into whole press cycles while honestly paying for every changeover; lay the result out shift-by-shift with cleanings and changeovers in the right places; and finally write a colour-coded Excel plan plus utilisation and fulfilment scorecards.
