# Correction Log — curing_LP.py

A running record of bug fixes applied to [curing_LP.py](curing_LP.py), newest first.
Each correction has an ID (e.g. `C1`) that is also tagged inline in the source as a
`# [C1]` comment so the edited lines can be traced back here.

---

## C1 — Press output understated by 2× (units-per-cycle)
**Date:** 2026-06-09
**Severity:** High — wrong production estimate, halves effective fleet capacity.
**Status:** Fixed.

### Symptom
A PCR curing press carries `MOULDS_PER_PRESS = 2` moulds, each mould has
`CAVITIES_PER_MOULD = 1` cavity, so a press cures **2 tyres per cycle**.
The LP / rounder / schedule-builder path computed output using
`CAVITIES_PER_MOULD` **alone**, dropping the `MOULDS_PER_PRESS` factor, so it
modelled each press as producing only **1 tyre per cycle** — half the truth.

Two things were wrong at once and had to be fixed together:

1. **Config value** — `CAVITIES_PER_MOULD` was set to `2`. A PCR mould has a
   single cavity; the correct value is `1`.
2. **Output math** — every cycle⇄unit conversion used `CAVITIES_PER_MOULD`
   instead of `CAVITIES_PER_MOULD × MOULDS_PER_PRESS`.

Evidence it was a genuine inconsistency, not a deliberate convention:
- `units_per_cleaning_cycle()` already multiplied by **both** factors.
- The **continuity** path already produced 2 units/cycle (it multiplies by
  `Num_Moulds`). So the LP path disagreed with both the cleaning constant and
  the continuity path inside the same run.

### Impact before fix
- `Demand_Mins` overstated by 2× (each tyre booked a full cycle instead of ½).
- Reported `Units_Planned` / shift `Qty` understated by 2×.
- Net: presses booked at ~2× the time really needed → satisfiable demand shown
  as PARTIAL/UNMET, utilisation misleading, plan throughput ~half of achievable.

### Fix
1. Added a single source of truth in `Config`:
   ```python
   @classmethod
   def units_per_cycle(cls) -> int:
       return cls.CAVITIES_PER_MOULD * cls.MOULDS_PER_PRESS
   ```
   and rewrote `units_per_cleaning_cycle()` to use it.
2. Set `CAVITIES_PER_MOULD = 1` (was `2`).
3. Replaced bare `Config.CAVITIES_PER_MOULD` with `Config.units_per_cycle()` at
   every cycle⇄unit conversion.

### Files / lines touched (tagged `# [C1]`)
| Location | What changed |
|---|---|
| `Config.CAVITIES_PER_MOULD` | `2` → `1` |
| `Config.units_per_cycle()` | new helper |
| `Config.units_per_cleaning_cycle()` | now `NEW_MOULD_LIFE * units_per_cycle()` |
| `_prepare_skus` — `Demand_Mins` | `/ units_per_cycle()` |
| `run()` LP-remainder recompute | `/ units_per_cycle()` |
| `Rounder._row` — `Units_Planned` | `* units_per_cycle()` |
| `Rounder.round` top-up (×2 sites) | `needed / units_per_cycle()`, `extra_c * units_per_cycle()` |
| `ScheduleBuilder._split_block` (×4 sites) | all unit⇄cycle conversions |

### Verification
- `Config.units_per_cycle()` → **2**; `units_per_cleaning_cycle()` → **6000**.
- LP path (2/cycle), continuity path (`Num_Moulds` = 2/cycle), and cleaning
  cadence (6000 units = 3000 cycles × 2) now all agree.

### Left unchanged (already correct)
- Continuity production/cleaning math (uses `Num_Moulds` from running data).

### Caveat / assumption
Assumes both moulds on a press close in the **same** press stroke (parallel
cure) and that demand `Quantity` is in physical tyres. If moulds cure
sequentially, the time side would also need revisiting.

---

## C2 — Zero-width space broke compilation
**Date:** 2026-06-09
**Severity:** High — `SyntaxError`, file would not run.
**Status:** Fixed.

A stray `U+200B` (zero-width space) sat immediately before the module docstring
(`​"""`), causing `SyntaxError: invalid non-printable character U+200B`.
Removed the character; file now parses cleanly. (Discovered incidentally while
verifying C1.)

---

## C3 — Excel-export print crashed on Windows console
**Date:** 2026-06-09
**Severity:** Medium — `UnicodeEncodeError` after the workbook is saved.
**Status:** Fixed.

`ExcelExporter.export` ended with `print(f"... Saved → {self.path}")`. On a
Windows cp1252 console the `→` (U+2192) raises
`UnicodeEncodeError: 'charmap' codec can't encode character '→'`, aborting
the process *after* the `.xlsx` was already written (so the file was fine but the
run exited non-zero). Affects both the LP and heuristic paths. Replaced `→` with
`->`.

---

## Note — new HEURISTIC scheduler (not a correction)
**Date:** 2026-06-09

Added [curing_heuristic.py](curing_heuristic.py) — a greedy, priority-first
alternative to the LP that consumes the same `inputs/` folder and writes the
**same 5-sheet Excel format** via the shared `ExcelExporter`. It reuses Phases
0/1/2/5 and the reporting from `curing_LP.py`, replacing only the LP solve +
rounding with `HeuristicAllocator`. No `scipy` dependency. See
[curing_heuristic_explainer.md](curing_heuristic_explainer.md). All corrections
above (C1 units-per-cycle in particular) apply to it automatically since it
shares `Config` and the downstream phases.

---

## Note — green-tyre BUILDING scheduler (not a correction)
**Date:** 2026-06-09

Added [building_schedule.py](building_schedule.py) + [dataloader2.py](dataloader2.py)
— a JIT green-tyre building scheduler that supplies the curing schedule (replaces
the static `GT_Inventory`). Pulls two building masters into `inputs/`, runs curing
in-memory for the per-SKU daily consumption, then builds against it day-by-day
(combined machines, or stage-2 machines at `ct = max(stage1, stage2)`), writing a
6-sheet `output/PCR_Building_Schedule.xlsx` in the same style. See
[building_explainer.md](building_explainer.md). Added a `write_excel` flag to
`run_heuristic_from_csv` so building can reuse curing's result without re-writing
(or locking) the curing workbook. **Finding:** with the current 20 GT-producing
machines, building tops out at ~582k vs curing's 692k demand — flagged, not hidden.

---

## Note — COUPLED feasible plan + versioning (suite v6.0)
**Date:** 2026-06-09

Added [coupled_plan.py](coupled_plan.py) + [_version.py](_version.py). Since
building (~571k) can't supply curing's 692k plan, the coupled orchestrator makes
the two **mutually feasible**: it computes building's per-SKU capacity envelope,
caps curing to it (then to building's actual JIT delivery), and re-solves until
curing total == building total (final: **563,932** each). Earlier naïve
"cap-and-re-run" oscillated (capping freed curing presses that flowed to other
unbuildable SKUs); fixed with a deterministic envelope + **complete caps** (every
demand SKU capped, 0 where unbuildable). Enabled by a `demand_cap` param on
`run_heuristic_from_csv`. Versioned output now lands in `output/v6.0/`
(curing, building, integrated summary); the version label drives workbook titles
via `_version.VERSION_LABEL`. See [coupled_plan_explainer.md](coupled_plan_explainer.md).

---

## C4 — Time-phased coupling: curing was scheduled ahead of building
**Date:** 2026-06-09
**Severity:** High — plan was quantity-feasible but NOT time-feasible.
**Status:** Fixed (suite v6.1).

The v6.0 coupled plan matched curing and building **totals** per SKU but not their
**daily timing**: curing front-loaded (~25k/day from day 1) while building (the
bottleneck) spread ~18-25k/day, so on day 1 curing cured ~6k green tyres that had
not been built yet (Sync Check showed LAG ~31/31 days). Fixed in
[coupled_plan.py](coupled_plan.py) `_time_phase()`: curing is re-paced to
building's daily per-SKU output (build-first, configurable `TIME_PHASE_LAG_DAYS`),
so cumulative cured never exceeds cumulative built. Result: **0/31 lag days**,
curing daily == building daily, totals 565,888 each.

Two sub-issues found and fixed while doing this:
- **Continuity production is absent from `machine_schedule`** (it's emitted in
  Phase 2, only the heuristic allocation lands in `df_mach`). The first
  time-phase pass pulled press assignments from `machine_schedule` and silently
  dropped 26 continuity SKUs (~268k units). Now derives press assignments from
  the full curing **shift schedule**.
- **Building efficiency made explicit:** `BUILD_EFFICIENCY = 1.0` (theoretical,
  per spec) in [building_schedule.py](building_schedule.py) — `effective ct =
  ct / BUILD_EFFICIENCY`. No derate applied; lower it to model OEE.

**Data provenance note:** `inputs/building_cycle_times.csv` contains 4 combined
machines `6001-6004` (VMIExxium01-04) that are **not in the DB table** (added to
the CSV manually). Building therefore has **24 GT producers** (18 combined + 6
stage-2), not 20. Re-running `dataloader2.py` from the DB would drop them — keep
them in the CSV or add them to the DB.

---

## C5 — Building changeover time + 6001-6004 appended to DB (v6.2)
**Date:** 2026-06-09
**Severity:** Modelling completeness.
**Status:** Done.

Two data/model changes:
1. **Building changeover.** `Master_Building_ChangeoverTime` (per-machine Same/
   Different-size minutes) is now pulled by [dataloader2.py](dataloader2.py) into
   `inputs/building_changeover.csv` and charged whenever a building machine
   switches SKU (`BUILD_CO_MODE = "different"` by default). The allocator and
   capacity envelope both deduct changeover from machine capacity and prefer
   keeping a machine on its current SKU; the building Shift Schedule now shows
   `CHANGEOVER` rows. Effect: ~1,860 changeover-hours, feasible plan
   **565,888 → 553,024** (78.9% → 77.0% of demand).
2. **6001-6004 in DB.** The four VMIExxium combined machines (previously only in
   the CSV) were appended to `Master_Building_Machine_Design_cycleTime` in the
   database, so `dataloader2.py` now regenerates them from the DB (the manual CSV
   addition is no longer needed). Building has 24 GT producers (18 combined + 6
   stage-2).

Verified: building machine-day minutes stay <= 1440 INCLUDING changeover;
time-phasing still holds (0/31 days curing exceeds building); curing == building
== 553,024.

---

## C6 — Size-aware building changeover (v6.3)
**Date:** 2026-06-09
**Severity:** Modelling refinement.
**Status:** Done.

Building changeover is now **size-aware**: rim size = characters 9–10 of the SKU
code (e.g. `1225119015010QSTL0` → 15"). A machine switching between two SKUs of
the **same** rim size pays the cheaper `SameSize_Min` (~20–110 min); a different
size pays `DifferentSize_Min` (~88–180 min). `BUILD_CO_MODE = "size"` (was
`"different"`); can still be forced to `different`/`same`/`none`. The changeover
map now carries both values per machine; `_co()` and the shift builder decide by
size; `_sku_size()` extracts the size.

Effect vs the all-different v6.2: changeover hours **1,858 → 1,427** (462
same-size + 535 diff-size switches), feasible plan **553,024 → 559,438**
(77.0% → 77.9% of demand). Still time-feasible (0/31), curing == building.

---

## Known issues NOT yet fixed (flagged, awaiting decision)
These were identified during review but are **not** addressed in this pass:

- **DB credentials hardcoded** in `Config` (server/user/password in plaintext).
- **Dense LP matrices** (`np.zeros`) — memory blows up at large SKU counts;
  switch `A_ub` to `scipy.sparse`.
- **LP ignores changeover/cleaning time** in capacity; horizon overflow in the
  builder is silently truncated, so `df_mach` can overstate vs `df_shift`.
- **Worn moulds treated as fresh** in the LP/builder path (`_split_block` uses
  the fixed cleaning cycle, ignoring per-mould `life_remaining`).
- **`ct` defaults to 15** when a cycle time is missing, making the
  "No cycle time" skip reason dead code.
- **`warnings.filterwarnings("ignore")`** globally hides real warnings.
- **Hardcoded 170-machine list** still inside `_build_continuity` (the v4
  changelog claims it was removed).
- **int/str machine-key juggling** is fragile.
