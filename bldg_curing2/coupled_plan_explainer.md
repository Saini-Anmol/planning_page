# Coupled Curing + Building Plan — Explainer (suite v6.3)

**File:** [coupled_plan.py](coupled_plan.py)
**Builds on:** [curing_heuristic.py](curing_heuristic.py) (curing) + [building_schedule.py](building_schedule.py) (building).
**Output:** `output/v6.3/` — three workbooks (curing, building, integrated summary).

> **v6.1** added **time-phasing** (§2a): the plan is not just quantity-feasible but **time-feasible** — curing never cures a green tyre before building has made it (0/31 lag days).
> **v6.2** added **building changeover** time; **v6.3** makes it **size-aware** (same-rim-size SKU switches are cheaper).

The curing fleet can cure more green tyres than the building fleet can supply — building is the binding constraint. A curing plan that ignores this is *not deliverable*: it schedules tyres for which no green tyre exists. This module **couples** the two schedulers so the final plan is **mutually feasible** — curing cures exactly what building can build, and building builds exactly what curing cures.

---

## 1. The problem it fixes

Run the two schedulers independently and you get an inconsistency:

| | Tyres |
|---|---|
| Curing demand | 718,128 |
| Curing **can cure** (press/mould capacity) | 692,024 |
| Building **can supply** (machine capacity) | ~571,000 |

So curing's 692k plan over-promises by ~120k green tyres that building can't make. The coupled plan resolves this to a single feasible number both fleets agree on.

---

## 2. How the coupling works (deterministic, 6 steps)

The naïve fixed-point — *cap curing to building's delivery, re-run, repeat* — **oscillates**: capping high-priority SKUs frees curing presses, which curing pours into lower-priority SKUs that building also can't supply. The fix is a **deterministic capacity envelope** plus **complete caps** (every demand SKU is capped, 0 where unbuildable, so none can leak).

```
1  Curing vs full demand          → curing's press/mould capacity per SKU
2  Building CAPACITY ENVELOPE      → max GTs building can make of each SKU over the
                                     whole horizon, allocated greedily by priority
                                     across shared machines (no timing). Covers EVERY
                                     demand SKU (0 if unbuildable).
3  Curing capped to the envelope   → first feasible curing plan (≤ supply everywhere)
4  Building JIT vs that plan        → building's real, time-phased delivery
5  Curing re-capped to that         → FINAL curing = exactly what was actually built
   actual delivery (complete cap)
6  Building JIT vs final curing     → FINAL build  (curing total == building total)
```

Steps 3–4 establish feasibility against building's *capacity*; steps 5–6 tighten curing down to building's *time-phased JIT delivery* (slightly below the capacity envelope, because a 1-day build-lead can't be fully achieved when building runs at ~100% utilisation). After step 6 the two plans match to the tyre.

**Why the envelope, not iteration:** the envelope is computed once and covers all SKUs, so capping is a single deterministic projection rather than a feedback loop. No oscillation, no convergence tolerance to tune.

## 2a. Time-phasing (v6.1) — no curing before building

Steps 1–6 make the **totals** feasible, but curing front-loads (presses run flat-out from day 1) while building spreads at its capacity — so day-by-day curing would cure green tyres before they exist. `_time_phase()` fixes this: building is the **master timeline**, and curing is **re-paced to building's daily per-SKU output** (build-first; `TIME_PHASE_LAG_DAYS`, default 0). Each SKU's daily cure quota = that SKU's GTs built that day, distributed across the curing presses it was assigned (taken from the full curing **shift** schedule, so continuity production is included). The result: `cumulative cured ≤ cumulative built` every day — **0/31 lag days** — and curing's daily output equals building's. Totals are unchanged.

---

## 3. Output — `output/v6.0/`

| File | Contents |
|---|---|
| `PCR_Curing_Schedule_v6.0.xlsx` | Final **feasible** curing plan — the 6 curing sheets (incl. Daily Curing) |
| `PCR_Building_Schedule_v6.0.xlsx` | Final building plan — the 6 building sheets (incl. Sync Check) |
| `PCR_Integrated_Plan_v6.0.xlsx` | **Executive Summary**, **SKU Reconciliation**, **Daily Plan**, **Coupling Log** |

### The integrated summary
- **Executive Summary** — version, run stamp, demand vs curing-capacity vs building-capacity vs the final feasible number, utilisations, the binding constraint, lag days, and SKU counts by binding type.
- **SKU Reconciliation** — per SKU: `Demand → Curing_Capacity → Building_Capacity → Final_Cured → Final_Built → Gap_vs_Demand`, plus the **Binding_Constraint** label (Demand met / Building-limited / Curing-limited). `Final_Cured == Final_Built` for every SKU, proving consistency.
- **Daily Plan** — day-by-day GTs built vs cured (today + cumulative), the JIT `Lead_Units`, and OK/LAG status.
- **Coupling Log** — the six-step tyre counts, so the cascade `692k → 571k → … → 564k` is auditable.

---

## 4. Result on current data

```
Original demand                : 718,128
Curing-only capacity           : 692,024
Building-only capacity (envelope): 572,525
FEASIBLE plan (cured = built)  : 559,438   (77.9% of demand)
  final curing planned         : 559,438
  final building built         : 559,438      ← matched, and time-feasible
Binding constraint             : Building (green-tyre supply)
Days curing exceeds building   : 0 / 31       (time-phased)
Building changeovers           : ~997  (~1,427 hrs: 462 same-size, 535 diff-size)
```

The feasible plan is **559,438 tyres** — what the plant can actually both build and cure, day by day, after size-aware changeover losses. Curing and building agree exactly and curing never runs ahead of building. The shortfall vs demand (≈159k) is almost entirely **building capacity** (incl. ~1,427 changeover-hours); the `SKU Reconciliation` sheet attributes every SKU's gap to building, curing, or "demand met".

**Building fleet note:** **24 GT-producing machines** — 18 combined (incl. `6001–6004` VMIExxium, now in the DB) + 6 stage-2 — plus 15 stage-1 carcass feeders. No efficiency derate (`BUILD_EFFICIENCY = 1.0`, theoretical, per spec); changeover from `Master_Building_ChangeoverTime` (`BUILD_CO_MODE = "different"`).

**Levers to lift the 78.5%** (data/ops decisions, not code): add or speed up building machines (especially the slow stage-1 machines that drag two-stage lines via `max(stage1, stage2)`); add building shifts; or accept the feasible plan and reprioritise which SKUs absorb the building shortfall.

---

## 5. How to run

```bash
python dataloader.py            # curing masters -> inputs/  (once)
python dataloader2.py           # building masters -> inputs/ (once)
#   add inputs/demand.csv
python coupled_plan.py          # -> output/v6.0/  (three workbooks)
```
or:
```python
from coupled_plan import run_coupled_plan
res = run_coupled_plan("inputs")     # returns coupled dfs, reconciliation, kpis, output_dir
```
**Dependencies:** `numpy`, `pandas`, `openpyxl` (no `scipy`).

---

## 6. Versioning

Central version lives in [_version.py](_version.py) (`SUITE_VERSION`). Outputs are written to `output/v<version>/`, and the version label appears in workbook titles and the Executive Summary. Version history:

- **v1–v4** — curing LP scheduler (internal LP versions).
- **v5.0** — units-per-cycle correction (C1), CSV data pipeline, heuristic curing scheduler, Daily Curing tab.
- **v6.0** — green-tyre building scheduler (JIT) + this coupled, mutually-feasible plan. **Current.**

See also: [curing_LP_explainer.md](curing_LP_explainer.md), [curing_heuristic_explainer.md](curing_heuristic_explainer.md), [building_explainer.md](building_explainer.md), [CORRECTION_LOG.md](CORRECTION_LOG.md).

---

## 7. Mental model in one line

> Find out how much curing *could* cure and how much building *could* build of each tyre; cap curing to what building can actually deliver (capping every SKU, zero where it can't be built, so nothing slips through); then settle the two against each other until the curing plan and the building plan are the same number — the tyres the plant can truly both build and cure — and write it all up, versioned, with a per-SKU reconciliation that says exactly what limits each tyre.
