# CLAUDE.md — context for AI assistants / future developers

This file gives an AI assistant (or a new human contributor) the minimum context
needed to make safe, correct changes to this codebase. **Read this BEFORE editing.**

---

## 1. What this project does

A Linear-Programming-based **curing schedule generator** for JK Tyre's PCR
(Passenger Car Radial) plant. Given a `plan_id`, it:

1. Reads per-SKU demand and plan parameters from MySQL
2. Computes a `ConsolidatedPriorityScore` per SKU
3. Solves an LP that allocates ~170 presses' minutes optimally over a month
4. Writes results back to MySQL in 4 tables and produces a 5-sheet Excel report

It's exposed as a Flask HTTP API on port 5001, and also runnable from a CLI.

The same engine runs in two **modes** — `planning` (jkt_* tables) and `simulation`
(jkt_sim_* tables) — selected per request. See §13 for the simulation pipeline.

---

## 2. Three entry points, one pipeline

```
main.py (CLI)        ─┐
app.py (Flask API)   ─┼─► V1.routes.{demand_route, schedule_route, upload_route}.run(cfg)
test_from_excel.py   ─┘                  (Phases A → B → C)
```

All three call the same `run(cfg)` functions. Don't add behavior that's specific
to one entry point — put logic in the route modules.

`app.py` registers **two** Flask blueprints: the planning one from
`V1.routes.api_route` (`/plan/generate-plan`) and the simulation one from
`simulation.routes.api_route` (`/simulation/generate-plan`). Both funnel into the
same `run(cfg)` functions; only `cfg["mode"]` (and thus the table names + output
filenames) differs. See §13.

---

## 3. Pipeline phases

```
Phase A — demand_route.run(cfg)
  • SELECT * FROM jkt_demand WHERE plan_id = ?
  • SELECT * FROM jkt_plan_params WHERE plan_id = ?
  • Compute CPS per SKU (market score + qty + date factors)
  • Write output/requirement_summary_<plan_id>.xlsx

Phase B — schedule_route.run(cfg)  [serialized by _RUN_LOCK]
  • Override Config.PLAN_DATE, PLANNING_DAYS, PRESS_EFFICIENCY,
    MAX_CHANGEOVERS_PER_SHIFT from the plan_params row
  • Run the legacy LP scheduler (jk_curing_lp_PCR.py copied as schedule_route.py)
  • Write output/PCR_Schedule_<plan_id>_<start>_<n>days.xlsx (5 sheets)

Phase C — upload_route.run(cfg)
  • plan_status.assert_not_already_scheduled() — 409 if duplicate
  • kpi_writer.upload()           → jkt_plan_kpis (1 row)
  • plan_writer.upload()          → jkt_plan (~14k rows, even-tyre rule applied)
  • capacity_writer.upload()      → jkt_plan_capacityUtilisation (per-date)
  • infeasibility_writer.upload() → jkt_plan_Infeasibility (1 row per at-risk SKU:
                                    unmet/partial/unschedulable demand + SKUs whose
                                    cycle time is "NA" = default 15-min cure, run
                                    through (raw+buffer)/efficiency like any SKU)
```

Phase-B & Phase-C output filenames carry a `{mode_tag}` (`""` planning, `sim_`
simulation) so a /plan and a /simulation run for the same plan_id never overwrite
each other's Excel. See §13.

---

## 4. Critical files (in priority order)

| File | What to know |
|---|---|
| [V1/routes/api_route.py](V1/routes/api_route.py) | Thin HTTP layer. `generate_plan()` runs A→B→C in sequence inside one try/except. URL constants near the top. |
| [V1/routes/schedule_route.py](V1/routes/schedule_route.py) | **1700-line LEGACY LP code — do NOT refactor internals.** Only `_run_locked()` near the bottom is our wrapper. DB overrides for `noOfChangeOver` and `efficiency` are applied here. |
| [V1/routes/demand_route.py](V1/routes/demand_route.py) | CPS computation. `_market_score()` inverts DB ranks (low rank = high priority → high score). |
| [V1/reports/kpi_writer.py](V1/reports/kpi_writer.py) | Writes `jkt_plan_kpis`. Computes KPIs from the Demand Fulfillment sheet + DB. Many helpers: `_is_real_sku_row`, `_count_planned_skus`, `_demand_weighted_fulfillment`, `_overall_capacity_utilisation`, `_round_up_to_even`, `_safe_number`. |
| [V1/reports/plan_writer.py](V1/reports/plan_writer.py) | Writes `jkt_plan`. Applies even-tyre rule via `_round_sku_totals_up_to_even`. Uses kpi_writer's `_round_up_to_even` helper. |
| [V1/reports/capacity_writer.py](V1/reports/capacity_writer.py) | Writes `jkt_plan_capacityUtilisation`. **Excludes** CHANGEOVER + mould-clean rows from busy time (productive utilization only). Shared `compute_daily_utilisation()` is used by both daily and overall calc. |
| [V1/reports/infeasibility_writer.py](V1/reports/infeasibility_writer.py) | Writes `jkt_plan_Infeasibility` (one row per at-risk SKU). Reuses kpi_writer's `_is_real_sku_row` / `_safe_number` / `_round_up_to_even`. Creates the table via `CREATE TABLE IF NOT EXISTS` (idempotent, non-destructive). Flags `defaultCycleTime=1` for SKUs whose Demand-Fulfillment `CycleTime_min` is `"NA"` (default 15-min cure, scheduled at the buffer/efficiency-adjusted ~18 min). Reports the **effective** cycleTime (read from the Shift Schedule). |
| [config/config.yaml](config/config.yaml) | Single source for tunable parameters, incl. the `tables:` block with `mode_token` (planning="" / simulation="sim_"). **DB credentials NOT here** — they come from `.env`. |
| [V1/utilities/config_loader.py](V1/utilities/config_loader.py) | `load(mode=...)` → `apply_mode()` resolves logical→physical table names into `cfg["tbl"]`. `mode_file_tag()` returns the `{mode_tag}` used in output filenames. |
| [simulation/](simulation/) | Thin simulation layer (§13). Routes re-export V1's `run()`; `sim_status` wraps `plan_status`. Same engine, jkt_sim_* tables. |
| [smoke_test.py](smoke_test.py) | 45 checks. Run before every commit. Zero DB side-effects. |

---

## 5. Conventions to follow

### Imports / Python version

- Python 3.9+ minimum (Docker uses 3.11). Always `from __future__ import annotations` at top of new files.
- No `typing.Optional` — use `X | None`.

### Time

**All `createdAt` timestamps use IST (UTC+5:30).** Always call `now_ist()` from `V1.utilities.time_utils` — never `datetime.now()`. The helper strips tzinfo so it's safe for MySQL `DATETIME` columns.

### Errors

- Raise `PipelineError(message, stage, status_code)` from `V1.utilities.exceptions` for any expected error condition.
- The HTTP layer catches `PipelineError` → returns the `status_code` as the HTTP code.
- Never `raise SystemExit(...)` in pipeline code — it kills the Flask worker.
- Don't use bare `except:` — always catch specific exceptions.

### Workbooks

- Always wrap `openpyxl.load_workbook(...)` and `openpyxl.Workbook()` in `try/finally: wb.close()` — file-handle leak otherwise.

### Numbers from Excel

Use `_safe_number()` in `kpi_writer.py` when reading numeric cells. It tolerates `#REF!`, `nan`, `inf`, comma-strings, etc. Plain `int()` / `float()` will crash on bad data.

### Even-tyre rule

The plant produces tyres in even counts only. Both `plan_writer` and `kpi_writer` must call the SHARED helper `_round_up_to_even()` in `kpi_writer.py`. Never reimplement the math locally.

### Concurrency

- `schedule_route.run()` is serialized by `_RUN_LOCK` because the legacy LP code uses a global `Config` class. Don't call it from multiple threads expecting parallelism.
- Don't change `gunicorn --workers 1` in the Dockerfile without also refactoring `Config` to be instance-based.

### Append-only invariant

The pipeline NEVER deletes or updates DB rows. Each `plan_id` is generated exactly once. The `plan_status.assert_not_already_scheduled()` enforces this. Don't add UPDATE / DELETE statements without a really good reason.

### SQL safety

Always use parameterized queries (`%s` placeholders). Never use f-strings or `.format()` to interpolate values into SQL.

---

## 6. Database schema (the parts the pipeline touches)

### Read

| Table | Key columns |
|---|---|
| `jkt_plan_params` | plan_id (PK), planStartDate, planEndDate, oe/re/st/defence/export/otr/government (ranks), marketWeightage, quantityWeightage, targetdateWeightage, efficiency (%), noOfChangeOver (per-shift) |
| `jkt_demand` | plan_id, skuCode, skuDescription, requirement, market, deliveryDate |
| `Master_Curing_Design_CycleTime`, `Master_Curing_Allowable_Machines_source`, `gt_inventory_manual`, `Master_WC_Master`, `Daily_Running_Moulds`, `Master_Mapping_Mould_SKU` | scheduler master data |

### Write

| Table | Key columns |
|---|---|
| `jkt_plan_kpis` | plan_id (1 row), demandFulfillment, demandSKU, planSKU, capacityUtilisation, curingChangeovers, createdAt, createdBy |
| `jkt_plan` | plan_id, skuCode, skuDescription, date, shift, startTime, endTime, qty, cycleTime, remarks, createdAt, createdBy |
| `jkt_plan_capacityUtilisation` | plan_id, date, capacityUtilisation, createdAt, **creatdBy** (typo in schema — keep as-is) |
| `jkt_plan_Infeasibility` | id (PK, auto), plan_id, skuCode, skuDescription, demand, plannedUnits, gap, fulfillmentPct, status, reason, cycleTime (effective CT used), **defaultCycleTime** (1 = CT was "NA"; default 15-min cure scheduled at ~18 min via buffer/efficiency), createdAt, createdBy. Auto-created by `infeasibility_writer` if absent. |

> **Mode note:** in simulation mode every read/write table above resolves to its
> `jkt_sim_*` counterpart (e.g. `jkt_sim_demand`, `jkt_sim_plan_Infeasibility`) via
> `cfg["tbl"]`. Never hardcode `"jkt_..."` — read `cfg["tbl"]["<logical>"]`. See §13.

---

## 7. KPI definitions (canonical — see also docs/KPI_GUIDE.md)

| KPI | How |
|---|---|
| `demandFulfillment` | `Σ min(planned_i/demand_i, 1.0) · (demand_i / Σ demand)` × 100. Per-SKU cap at 100%. Each `planned` rounded UP to next even. TOTAL row excluded. |
| `demandSKU` | `COUNT(DISTINCT skuCode) FROM jkt_demand WHERE plan_id=?` |
| `planSKU` | Demand Fulfillment sheet rows where `Planned_Units > 0`. Excludes TOTAL row + UNMET + UNSCHEDULABLE. |
| `capacityUtilisation` | Mean of per-date utilization. Per-date = `Σ over machines of min(busy/1440, 1.0) / 170`. **Busy excludes CHANGEOVER + mould-clean.** |
| `curingChangeovers` | Parsed from Demand Fulfillment sheet's row 2 summary: `"Changeovers: N"` |

Daily `jkt_plan_capacityUtilisation` and overall `jkt_plan_kpis.capacityUtilisation` use the SAME `compute_daily_utilisation()` function so they can never disagree.

---

## 8. Common tasks

### Run the smoke test (before every commit)

```bash
source .venv/bin/activate
python3 smoke_test.py
# expect "45 passed, 0 skipped, 0 failed"
```

### Local LP test (no DB writes)

```bash
python3 test_from_excel.py --plan-id <id> --book input/<file>.xlsx --sheet <name>
```

### Start the API server

```bash
python3 app.py    # listens on http://0.0.0.0:5001
```

### Trigger a pipeline run via API

```bash
curl -X POST http://localhost:5001/app/v1/jkt/planning-scheduling/plan/generate-plan \
     -H "Content-Type: application/json" \
     -d '{"plan_id": "<id>"}' --max-time 600
```

### Re-run a `plan_id` (manual cleanup required)

```sql
DELETE FROM jkt_plan_kpis                WHERE plan_id='<id>';
DELETE FROM jkt_plan                     WHERE plan_id='<id>';
DELETE FROM jkt_plan_capacityUtilisation WHERE plan_id='<id>';
DELETE FROM jkt_plan_Infeasibility       WHERE plan_id='<id>';
```

For a **simulation** re-run, delete the same rows from the `jkt_sim_*` tables
(`jkt_sim_plan_kpis`, `jkt_sim_plan`, `jkt_sim_plan_capacityUtilisation`,
`jkt_sim_plan_Infeasibility`).

### Rebuild + push Docker images

```bash
docker build -t jkt-planning:v1 .
docker buildx build --platform linux/amd64 -t jkt-planning:v1-amd64 --load .
docker tag jkt-planning:v1-amd64 anmolsaini07/jkt-planning:v1-amd64
docker push anmolsaini07/jkt-planning:v1-amd64
```

---

## 9. Gotchas — read these before debugging

| Symptom | Likely cause |
|---|---|
| Same plan_id produces same KPIs across regenerates | Expected — LP is deterministic given same inputs |
| Different plan_ids with same params produce identical KPIs | Demand-table state changed between runs — check jkt_demand |
| `MAX_CHANGEOVERS_PER_SHIFT` change doesn't affect output | At low changeover demand the cap doesn't bind. Try cap=1 to see effect. |
| Day 1 utilization is ~70% — looks wrong | Plan starts at 07:00; day 1 calendar has only ~17 productive hours |
| 100% utilization on a day | LP scheduled every press to full 1440 min. Valid if the day has no CO/clean. |
| `planSKU > demandSKU` | Bug — should be impossible. Check that TOTAL row + UNMET rows are excluded from planSKU. |
| pip install fails with `externally-managed-environment` | Use a venv: `python3 -m venv .venv && source .venv/bin/activate` |
| Docker build fails on numpy compile | Wrong Python version (e.g., 3.14). Use 3.11 venv to match Dockerfile. |
| pymysql version error | `requirements.txt` was wrong earlier. Verify `pymysql==1.2.0`, not `1.4.6`. |

---

## 10. Things NOT to change without discussion

| | Reason |
|---|---|
| **The 1700-line LP scheduler internals** in `V1/routes/schedule_route.py` (lines 1-1640) | Legacy, complex, working. Only `_run_locked()` and the override block (~1700) are our additions. |
| **Append-only behavior** (`plan_status.assert_not_already_scheduled`) | Product decision — manual cleanup required to re-run. |
| **`creatdBy` typo** in `jkt_plan_capacityUtilisation` column | Matches the actual DB schema. Fixing requires a coordinated DB migration. |
| **Hardcoded port 5001** | Matches the JS backend's expectation. |
| **URL path `/app/v1/jkt/planning-scheduling/plan/generate-plan`** | Locked in with the consuming JS backend. |
| **Docker `--workers 1`** | LP uses process-global Config state; multi-worker would race. |

---

## 11. Files you can safely ignore

| File | Why |
|---|---|
| `demand_extract.py`, `demand_extract.yaml` | Legacy V0 — superseded by `V1/routes/demand_route.py` |
| `jk_curing_lp_PCR.py` | Legacy V0 — copied verbatim as `V1/routes/schedule_route.py` |
| `upload.ipynb` | Legacy V0 — logic now in `V1/reports/{kpi,plan,capacity}_writer.py` |
| `load_*.xlsx`, `df_shift*.xlsx`, `output/*.xlsx` | Generated artifacts |
| `.venv/` | Local virtual environment |
| Files in `input/` | Reference Excels for ad-hoc testing |

---

## 12. Where to read more

- [README.md](README.md) — user-facing project documentation
- [docs/KPI_GUIDE.md](docs/KPI_GUIDE.md) — KPI formulas with worked numerical examples
- Source code comments — especially in `V1/reports/*.py` (well-commented helpers)

---

## 13. Simulation mode (jkt_sim_* tables)

The simulation page runs the **exact same engine** as planning against a parallel
set of tables, so users can try "what-if" plans without touching production data.

### How it works (the mode system)

- `config_loader.load(mode="planning"|"simulation")` calls `apply_mode()`, which
  reads `config.yaml → tables.mode_token` (`planning: ""`, `simulation: "sim_"`)
  and builds `cfg["tbl"]` = `{logical_name → physical_table}`. The token is
  inserted right after a leading `jkt_`:
  `jkt_demand → jkt_sim_demand`, `jkt_plan_Infeasibility → jkt_sim_plan_Infeasibility`.
- **Every** reader/writer takes table names from `cfg["tbl"][...]` (with a `jkt_*`
  default). Nothing hardcodes a physical name — that's what makes one codebase
  serve both modes. If you add a table, add it to `config.yaml → tables:` too.
- Output Excel filenames carry `mode_file_tag(cfg)` (`""` / `"sim_"`) so the two
  pipelines never overwrite each other in `output/` for the same plan_id.

### The `simulation/` package

| File | Role |
|---|---|
| `simulation/routes/api_route.py` | Blueprint `simulation` → `POST /app/v1/jkt/planning-scheduling/simulation/generate-plan`. Calls `config_loader.load(mode="simulation")`, then the shared A→B→C `run()`s. |
| `simulation/routes/{demand,schedule,upload}_route.py` | **Thin re-exports** of V1's `run()` — `from V1.routes.X import run`. No logic. They exist so the simulation entry points are visible at a glance. |
| `simulation/setups/sim_status.py` | `assert_not_already_simulated()` — wraps V1's `plan_status.assert_not_already_scheduled()` with the 3 sim output tables. |

So new pipeline logic only ever goes in `V1/` — the simulation layer inherits it
automatically via the re-exports + the mode-resolved table names. Don't duplicate
engine logic into `simulation/`.

### Concurrency note

Planning and simulation share `V1.routes.schedule_route._RUN_LOCK`, so a concurrent
`/plan` + `/simulation` request can't corrupt the LP's process-global `Config`.
