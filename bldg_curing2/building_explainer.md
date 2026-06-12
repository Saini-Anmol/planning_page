# Green-Tyre Building Scheduler — Explainer

**File:** [building_schedule.py](building_schedule.py)
**Data loader:** [dataloader2.py](dataloader2.py)
**Feeds:** the curing scheduler ([curing_heuristic.py](curing_heuristic.py) / [curing_LP.py](curing_LP.py)).
**Output:** `output/PCR_Building_Schedule.xlsx` — same look & feel as the curing workbook.

This scheduler produces the **green tyres (GTs)** that the curing schedule consumes. It replaces the old static `GT_Inventory` assumption: instead of treating green tyres as already-on-hand stock, we now actually **build** them, synced day-by-day to what curing will cure.

---

## 1. The two production routes

A green tyre can be produced two ways (from `Master_Building_Machine_Design_cycleTime`, where each machine has a `Stage Name`):

- **Combined** — one machine turns components directly into a green tyre. `cycle_time = that machine's ct`.
- **Two-stage** — **stage-1** builds a *carcass* from some components; **stage-2** turns carcass + other components into the green tyre.

> **All GTs come out of either a combined machine or a stage-1+stage-2 pair** — never anywhere else.

### How two-stage is modelled (agreed spec)

The two stages are treated as **one single process**:
- The GT is scheduled on the **stage-2 machine** (the machine that actually emits the green tyre).
- Its effective cycle time is `max(stage-1 ct, stage-2 ct)` — the bottleneck stage governs throughput. For the stage-1 reference we use the **fastest** stage-1 machine allowed for that SKU.
- **Stage-1 carcass supply is assumed non-binding** (full component availability), so stage-1 machine capacity is *not* separately scheduled — only its speed feeds into the `max()`.

A SKU's **producer machines** are therefore: every allowed *combined* machine (at its own ct) plus every allowed *stage-2* machine (at `max(ct2, fastest-allowed-ct1)`), but only if the SKU also has at least one allowed stage-1 machine. The route label per SKU is `combined`, `two-stage`, `both`, or `none`.

Cycle times are **net** (the table's `Norms ≈ 480 ÷ ct`, i.e. tyres per 8-hour shift), so no efficiency derate is applied (`BUILD_EFFICIENCY = 1.0`, configurable). One building cycle = one green tyre (no moulds/cavities).

**Changeover (v6.2, size-aware in v6.3).** When a building machine switches SKU it loses changeover time from `Master_Building_ChangeoverTime`. The cost is **size-aware** (`BUILD_CO_MODE = "size"`): if the two SKUs share **rim size** — characters 9–10 of the SKU code (e.g. `1225119015010QSTL0` → `15` inch) — the cheaper `SameSize_Min` applies (~20–110 min); otherwise `DifferentSize_Min` (~88–180 min). The allocator and capacity envelope deduct it from machine capacity and prefer keeping a machine on its current SKU; the Shift Schedule shows `CHANGEOVER` rows. (`BUILD_CO_MODE` can be forced to `"different"`, `"same"`, or `"none"`.)

---

## 2. Inputs — `dataloader2.py`

Run once to pull the two building masters from the DB into the shared `inputs/` folder:

```bash
python dataloader2.py        # -> inputs/building_cycle_times.csv, inputs/building_allowable.csv
```

| File | Columns | Source table |
|---|---|---|
| `building_cycle_times.csv` | `SAPMachineCode, MachineName, StageName, Norms, CycleTime_min` | `Master_Building_Machine_Design_cycleTime` |
| `building_allowable.csv` | `SKUCode, Machines` (list of allowed machine codes) | `Master_Building_Allowable_Machines_source` (Yes/No grid → list) |
| `building_changeover.csv` | `MachineCode, MachineName, SameSize_Min, DifferentSize_Min` | `Master_Building_ChangeoverTime` |

The fleet: **18 combined + 15 stage-1 + 6 stage-2 = 39 machines**, i.e. **24 GT producers** (combined + stage-2). The four `6001–6004` (VMIExxium) combined machines were appended to the DB cycle-time table in v6.2, so they now come straight from the database.

Building demand is *not* a file — it is derived from the curing schedule (next section).

---

## 3. Sync with curing — day-by-day JIT

The building demand per SKU is exactly **what the curing schedule plans to cure** (not the raw curing demand). So the building run begins by running the curing scheduler in-memory to get its per-SKU, per-day consumption profile.

**Sync rule — JIT lead.** Cumulative GTs built must stay **ahead of** cumulative GTs cured, every day, per the horizon. A configurable `BUILD_LEAD_DAYS` (default **1**) makes building target curing's cumulative demand *one day ahead*, so a green tyre is on hand before curing needs it. `BUILD_LEAD_DAYS = 0` would be strict same-day JIT.

### Allocation algorithm (`BuildingScheduler.allocate`)

```
for each day d in the horizon:
    machine capacity = 1440 min/day  (3 shifts × 480)   # reset each day
    for each SKU, highest curing-Priority first:
        target = cumulative GTs curing needs through day (d + LEAD)
        need   = target − already-built
        if need ≤ 0: skip
        rank this SKU's producer machines: fastest ct first, then most spare capacity
        fill whole tyres into each machine until need is met or capacity runs out
        if need still > 0 after all machines: record a capacity shortfall (this SKU lags)
```

Greedy, priority-first, fastest-machine-first. High-priority SKUs claim the fast combined machines; lower-priority SKUs fall back to slower two-stage lines or go short. Nothing is silently dropped — shortfalls surface in the report.

The day's allocations are then laid out into shift-wise rows (A/B/C) by walking each machine's day from 07:00 and splitting blocks at shift boundaries (shared `_get_shift_fn`). Each row's `Date` is the **production day**, so night-shift C stays attributed to the day it belongs to.

---

## 4. Output — `output/PCR_Building_Schedule.xlsx`

Six sheets, styled like the curing workbook:

| Sheet | Content |
|---|---|
| **GT Supply** | Per SKU: required (= curing plan) vs built, gap, %, status (FULLY MET / PARTIAL / UNMET / UNSCHEDULABLE), route, producer-machine count, skip reason |
| **Machine Schedule** | Per building machine × SKU: stage, route, effective cycle time, units built, minutes, days |
| **Shift Schedule** | Time-stamped building rows (Date, Shift, Machine, SKU, Start/End, Qty, route) |
| **Machine Utilization** | Per machine: available/used/idle minutes, utilisation %, SKUs, units |
| **Daily Building** | GTs built per day, split by shift A/B/C, with running cumulative + TOTAL |
| **Sync Check** | Per day: built vs cured (today and cumulative), `Lead_Units` (cum built − cum cured), and OK/LAG status — the JIT scorecard |

---

## 5. How to run

```bash
python dataloader2.py            # once — generate building input CSVs
python building_schedule.py      # runs curing for the profile, then builds, writes output/
```
or from code:
```python
from building_schedule import run_building_from_csv
results = run_building_from_csv("inputs")        # returns the 6 DataFrames + writes xlsx
# or pass an existing curing run to avoid re-running it:
# run_building_from_csv("inputs", curing_results=my_curing_results)
```

**Dependencies:** `numpy`, `pandas`, `openpyxl` (no `scipy`).

---

## 6. Key finding on the current data — building is the bottleneck

On the live-pulled masters, the building fleet **cannot keep up with curing**:

```
GT required (by curing) : 692,024
GT built                : 581,985   (84.1%)
Fully met SKUs          : 69    Partial: 4    Unmet: 27    No-build-route: 2
Days building lags curing: 32 / 32
```

This is **not** a scheduler inefficiency — 18 of the 20 GT-producing machines run at ~**99.98% utilisation**. The structural reason:
- Only **20 machines** can emit a green tyre (14 combined + 6 stage-2) versus **170 curing presses**.
- Two-stage lines are slowed by `max(stage1, stage2)`: stage-1 cts run up to 5 min, dragging stage-2 effective cts well above their standalone 1.2–2.0 min.
- Combined capacity (~16–17k/day) + two-stage capacity (~2k/day) ≈ **~18.8k GT/day**, while curing consumes **~25.5k/day**.

So curing's 692k plan is **not actually deliverable** with today's building capacity — building tops out near **582k**. The `Sync Check` sheet shows the lag widening daily (to ~120k by month end). The highest-priority SKUs are fully supplied; the shortfall lands on the 27 lowest-priority UNMET SKUs.

**Resolution — the coupled plan.** This gap is now resolved by [coupled_plan.py](coupled_plan.py) (suite **v6.0**), which caps the curing plan to building's deliverable supply so the two schedules are **mutually feasible** (final: curing == building == ~563,932 tyres). See [coupled_plan_explainer.md](coupled_plan_explainer.md). Running `building_schedule.py` standalone still *follows curing and flags the gap* (this section); running `coupled_plan.py` produces the reconciled, feasible plan.

**Levers to lift coverage** (data/ops, not code): add building machines or shifts; speed up the slow stage-1 machines feeding two-stage lines.

---

## 7. Assumptions (confirm if any should change)

- Two-stage scheduled on the **stage-2 machine** with `ct = max(stage1, stage2)`; stage-1 not capacity-bound.
- Building demand = curing's **planned** output (not raw demand).
- **JIT lead = 1 day** (`BUILD_LEAD_DAYS`).
- Same 31-day / 3×8-shift horizon and `plan_start` as curing.
- **Building changeover** modelled from `Master_Building_ChangeoverTime`, size-aware (same-rim-size switches cheaper; v6.3).
- `6001–6004` (VMIExxium) included — now in the DB cycle-time table.
- Full component availability (only machine capacity and allowable SKU↔machine combinations constrain building).

---

## 8. Mental model in one line

> Run curing to learn how many green tyres of each SKU it will cure each day; then, highest-priority first, build those tyres a day ahead on the fastest machine that's allowed to make them — a combined machine, or a stage-2 machine running at the slower of its two stages — filling each day's capacity until demand is met or the (few) building machines are full; lay it out shift-by-shift and write the same-style workbook, with a JIT scorecard showing exactly where building can't keep pace.
