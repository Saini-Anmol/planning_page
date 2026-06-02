# JK Tyre — PCR Curing Planning & Scheduling Pipeline (V1)

A production-grade LP-based scheduling pipeline for tyre curing operations. Given a
`plan_id`, the engine pulls demand and plan parameters from MySQL, computes a
priority score per SKU, runs a Linear Programming solver to allocate press
minutes optimally, and writes the resulting schedule back to the database.

**Three entry points (same pipeline, same outputs):**

- **HTTP API** — Flask app on port 5001 (for JS/UI backends)
- **CLI** — `python3 main.py --plan-id <id>` (for batch/dev)
- **Local test** — `python3 test_from_excel.py` (Excel demand input, no DB writes)

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [Pipeline phases](#pipeline-phases)
4. [API endpoint](#api-endpoint)
5. [KPIs computed](#kpis-computed)
6. [Configuration](#configuration)
7. [Database tables](#database-tables)
8. [Local testing](#local-testing)
9. [Docker deployment](#docker-deployment)
10. [Troubleshooting](#troubleshooting)
11. [Known constraints](#known-operational-constraints)
12. [Project layout](#project-layout)

---

## Quick start

```bash
# 1. Set up a virtual environment
cd /Users/anmolsaini/Documents/db_data_upload
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 2. Create your .env with DB credentials (template at .env.example)
cp .env.example .env
$EDITOR .env

# 3. Sanity check (no DB writes, ~5 seconds)
python3 smoke_test.py
# expect: "32 passed, 0 skipped, 0 failed"

# 4a. CLI — full pipeline for a plan
python3 main.py --plan-id <YOUR_PLAN_ID>

# 4b. OR — HTTP API
python3 app.py                                         # leave running
# from another terminal:
curl -X POST http://localhost:5001/app/v1/jkt/planning-scheduling/plan/generate-plan \
     -H "Content-Type: application/json" \
     -d '{"plan_id": "<YOUR_PLAN_ID>"}' --max-time 600
```

---

## Architecture

```
db_data_upload/
├── main.py                       # CLI entry — runs phases A→B→C
├── app.py                        # Flask app entry (port 5001)
├── test_from_excel.py            # Local LP test from Excel (no DB writes)
├── smoke_test.py                 # 32-check sanity test (zero side-effects)
├── Dockerfile                    # python:3.11-slim + gunicorn
├── requirements.txt              # pinned dependencies
├── config/config.yaml            # unified config (DB, weights, scheduler knobs)
├── .env                          # ★ DB credentials (gitignored)
├── .env.example                  # template for .env
│
├── V1/
│   ├── routes/                   # ── pipeline + HTTP handlers ──
│   │   ├── api_route.py          # POST /plan/generate-plan
│   │   ├── demand_route.py       # PHASE A — compute CPS per SKU
│   │   ├── schedule_route.py     # PHASE B — LP scheduler (1700 lines)
│   │   └── upload_route.py       # PHASE C — orchestrate 3 DB writers
│   ├── setups/                   # ── data loaders ──
│   │   ├── plan_params.py        # SELECT FROM jkt_plan_params
│   │   ├── demand_db.py          # SELECT FROM jkt_demand
│   │   └── plan_status.py        # append-only guard (409 if duplicate)
│   ├── utilities/                # ── shared helpers ──
│   │   ├── config_loader.py      # YAML + env-var override (.env)
│   │   ├── db.py                 # mysql.connector + sqlalchemy factories
│   │   ├── exceptions.py         # PipelineError (HTTP-aware)
│   │   └── time_utils.py         # now_ist() — IST (UTC+5:30) timestamps
│   └── reports/                  # ── DB writers ──
│       ├── kpi_writer.py         # → jkt_plan_kpis (1 row)
│       ├── plan_writer.py        # → jkt_plan (~14k rows)
│       └── capacity_writer.py    # → jkt_plan_capacityUtilisation (per date)
│
├── docs/
│   └── KPI_GUIDE.md              # KPI definitions + worked examples
│
├── input/                        # demand Excels (legacy / test fixtures)
└── output/                       # generated schedules + requirement summaries
```

---

## Pipeline phases

```
                  ┌──────────────────── Phase A ────────────────────┐
   jkt_demand     │  demand_route.run()                              │
   jkt_plan_params│  • read demand (per-SKU) from jkt_demand          │
        │        │  • read plan_params (dates, weights, market ranks)│
        ▼        │  • compute ConsolidatedPriorityScore per SKU      │
   ─────────────►│  • write requirement_summary_<plan_id>.xlsx       │
                  └─────────────────────┬────────────────────────────┘
                                        ▼
                  ┌──────────────────── Phase B ────────────────────┐
   6 master tables│ schedule_route.run()  (under _RUN_LOCK)           │
   (cycles,       │  • override Config.PLAN_DATE, PLANNING_DAYS,       │
   eligibility,   │    PRESS_EFFICIENCY, MAX_CHANGEOVERS_PER_SHIFT    │
   GT inventory,  │    from jkt_plan_params row                       │
   mould master)  │  • run LP solver (scipy.linprog)                 │
        │        │  • round continuous → integer cycles              │
        ▼        │  • build shift-wise schedule + changeovers        │
   ─────────────►│  • write PCR_Schedule_<plan_id>_*.xlsx (5 sheets) │
                  └─────────────────────┬────────────────────────────┘
                                        ▼
                  ┌──────────────────── Phase C ────────────────────┐
                  │ upload_route.run()                                │
                  │  • plan_status.assert_not_already_scheduled(...) │
                  │    (raises 409 if plan_id already has rows)      │
                  │  • kpi_writer.upload()                            │
                  │      → INSERT INTO jkt_plan_kpis                  │
                  │  • plan_writer.upload()                            │
                  │      → INSERT INTO jkt_plan                       │
                  │      (rounds each SKU total UP to even tyres)    │
                  │  • capacity_writer.upload()                       │
                  │      → INSERT INTO jkt_plan_capacityUtilisation  │
                  │      (productive minutes only, no CO/clean)      │
                  └──────────────────────────────────────────────────┘
```

---

## API endpoint

The full URL is exported as a constant in [V1/routes/api_route.py:43](V1/routes/api_route.py#L43).

```
POST http://<host>:5001/app/v1/jkt/planning-scheduling/plan/generate-plan
GET  http://<host>:5001/app/v1/jkt/planning-scheduling/health
```

### Request body

```json
{ "plan_id": "BTP_June_Plan_V1_124766" }
```

### Response — success (200)

```json
{
  "status": "success",
  "plan_id": "BTP_June_Plan_V1_124766",
  "elapsed_seconds": 142.7
}
```

### Response — errors

| HTTP | `stage` | When |
|------|---------|------|
| 400 | `validation` | Missing/empty/non-string `plan_id`, malformed JSON, plan_id > 50 chars |
| 404 | `plan_params` | `plan_id` not found in `jkt_plan_params` |
| 404 | `demand` | No rows in `jkt_demand` for this `plan_id` |
| 409 | `duplicate_check` | This `plan_id` is already scheduled (append-only guard) |
| 412 | `upload` | Schedule Excel went missing before Phase C OR Machine Utilization sheet has no machines |
| 422 | `demand` | All three CPS weights are zero |
| 500 | varies | Unexpected — returns last 6 lines of traceback |

All error responses are JSON: `{"status": "error", "stage": "...", "plan_id": "...", "message": "..."}`.

### Timeout note for JS clients

The LP solver can take 1-5 minutes. Set the client timeout accordingly:

```javascript
axios.post(url, body, { timeout: 600_000 })   // 10 minutes
```

Default `axios` / `fetch` timeouts (often 60s) will give up before the LP finishes.

---

## KPIs computed

Inserted into `jkt_plan_kpis` by `kpi_writer.upload()`.

| Column | Source | Formula |
|--------|--------|---------|
| `demandFulfillment` | computed from Demand Fulfillment sheet | `Σ min(planned_i/demand_i, 1.0) · (demand_i / Σ demand) × 100`. Each SKU capped at 100%. `planned` is rounded UP to next even tyre. The 'TOTAL' summary row is excluded. |
| `demandSKU` | `jkt_demand` table | `COUNT(DISTINCT skuCode) WHERE plan_id = ?` |
| `planSKU` | Demand Fulfillment sheet | Count of rows where `Planned_Units > 0`. Excludes the 'TOTAL' summary row AND SKUs with status `UNMET` / `UNSCHEDULABLE`. |
| `capacityUtilisation` | mean of per-date utilization | Productive minutes only (CO + mould-clean excluded). Denominator = 1440 min × 170 fleet machines. Capped at 100% per machine-day. |
| `curingChangeovers` | parsed from sheet's summary string | Integer count from `"Changeovers: N"` |
| `createdAt` | IST timezone | `now_ist()` — UTC+5:30, no daylight savings |
| `createdBy` | `config.yaml → upload.created_by` | Defaults to `"Algo8 AI"` |

See [docs/KPI_GUIDE.md](docs/KPI_GUIDE.md) for full formulas with worked examples.

---

## Configuration

`config/config.yaml` is the single source of truth for tunable parameters.
**DB credentials are NOT in this file** — they come from environment variables (loaded from `.env`).

### Environment variables (`.env`)

| Variable | Required | Purpose |
|----------|----------|---------|
| `JKT_DB_HOST` | ✅ | MySQL host |
| `JKT_DB_PORT` | ✅ | MySQL port (3306) |
| `JKT_DB_USER` | ✅ | MySQL user |
| `JKT_DB_PASSWORD` | ✅ | MySQL password |
| `JKT_DB_DATABASE` | ✅ | `jkplanningV1` |

`.env` is **gitignored** AND **dockerignored** — it never enters version control or Docker images.

### YAML knobs

| Section | Knob | Default | Purpose |
|---------|------|---------|---------|
| `plan` | `plan_id` | sample | Default plan_id used by CLI when `--plan-id` not given |
| `demand` | `default_weights.market` | 0.50 | CPS market weight when DB row is NULL |
| `demand` | `default_weights.quantity` | 0.20 | CPS quantity weight when DB row is NULL |
| `demand` | `default_weights.date` | 0.30 | CPS date weight when DB row is NULL |
| `demand` | `default_market_scores` | OE=7, Rep=1, … | CPS market scores when DB ranks are NULL |
| `demand` | `market_score_scale` | min=1, max=7 | Range used in normalization |
| `demand` | `market_aliases` | OE→oe, etc. | Mapping from free-text market → DB column name |
| `schedule` | `press_efficiency` | 0.94 | Press efficiency (cycle-time inflation factor) |
| `schedule` | `max_changeovers_per_shift` | 5 | Per-shift CO cap (used when DB `noOfChangeOver` is NULL) |
| `schedule` | `shift_start_hour` | 7 | Shift A starts at 07:00 |
| `upload` | `created_by` | "Algo8 AI" | Written to all `createdBy` columns |

### Per-plan DB overrides (`jkt_plan_params`)

The pipeline reads these from the plan's row and overrides YAML defaults when set. NULL or 0 → falls back to YAML.

| DB column | Maps to | Notes |
|-----------|---------|-------|
| `planStartDate` | `Config.PLAN_DATE` | **Required**, no fallback |
| `planEndDate` | `Config.PLANNING_DAYS` | **Required** (computed as `(end-start).days + 1`) |
| `marketWeightage` | CPS market weight | Renormalized to sum=1 with others |
| `quantityWeightage` | CPS quantity weight | Renormalized |
| `targetdateWeightage` | CPS date weight | Renormalized |
| `oe / re / st / defence / export / otr / government` | CPS market ranks | NULL → uses YAML `default_market_scores` |
| `efficiency` | `Config.PRESS_EFFICIENCY` | Stored as percentage (94 = 94%); converted to fraction (0.94) at runtime |
| `noOfChangeOver` | `Config.MAX_CHANGEOVERS_PER_SHIFT` | Direct mapping — value is per-SHIFT, not per-day |

---

## Database tables

### Read by the pipeline

| Table | Purpose |
|-------|---------|
| `jkt_plan_params` | Per-plan inputs: dates, weights, market ranks, efficiency, changeover cap |
| `jkt_demand` | Per-SKU demand (SKUCode, requirement, market, delivery date) |
| `Master_Curing_Design_CycleTime` | SKU → raw cure time |
| `Master_Curing_Allowable_Machines_source` | SKU → eligible machines matrix |
| `gt_inventory_manual` | Green-tyre on-hand inventory |
| `Master_WC_Master` | Work-centre / machine master |
| `Daily_Running_Moulds` | Snapshot of moulds currently on presses |
| `Master_Mapping_Mould_SKU` | Mould ↔ SKU compatibility |

### Written by the pipeline

| Table | Writer | Rows per run |
|-------|--------|--------------|
| `jkt_plan_kpis` | `kpi_writer` | 1 |
| `jkt_plan` | `plan_writer` | ~14,000 (one per scheduled slot) |
| `jkt_plan_capacityUtilisation` | `capacity_writer` | One per planning day (~30) |

The pipeline **never deletes or truncates**. Each `plan_id` can be scheduled exactly once — repeat calls return **HTTP 409 Conflict**. To re-run a plan_id, manually `DELETE WHERE plan_id=...` from the 3 output tables.

---

## Local testing

`test_from_excel.py` runs Phases A + B against an Excel demand file, writes the 5-sheet schedule to `output/`, and **skips Phase C entirely** — no DB writes.

```bash
python3 test_from_excel.py \
    --plan-id <plan_id>                         # for dates/weights/ranks from jkt_plan_params
    --book "input/<your-demand-file>.xlsx"
    --sheet "<sheet-name>"                       # default: "Sheet1"
    --efficiency 94                              # optional: override press efficiency for this run
```

The demand Excel must have row 1 headers:
`SKUCode | SKU Description | Requirement | Order Type | Market | Delivery date`

Rows with non-numeric, inf, NaN, or negative `Requirement` are silently skipped (count is printed). Comma-separated strings like `"2,362"` are cleaned to `2362` before parsing.

---

## Docker deployment

### Build locally (both architectures)

```bash
# arm64 (Apple Silicon)
docker build -t jkt-planning:v1 .

# amd64 (Intel/AMD — Windows, Intel Linux, AWS x86)
docker buildx build --platform linux/amd64 -t jkt-planning:v1-amd64 --load .
```

### Run

```bash
docker run --rm -p 5001:5001 \
    --env-file .env \
    -v $(pwd)/output:/app/output \
    --name jkt-api \
    jkt-planning:v1                   # or jkt-planning:v1-amd64 on Windows/Intel
```

### Docker Hub

```
Image: anmolsaini07/jkt-planning:v1-amd64
URL:   https://hub.docker.com/r/anmolsaini07/jkt-planning/tags
```

To pull and run on Windows / Intel Linux:

```powershell
docker pull anmolsaini07/jkt-planning:v1-amd64
docker run --rm -p 5001:5001 --env-file .env `
    -v ${PWD}/output:/app/output --name jkt-api `
    anmolsaini07/jkt-planning:v1-amd64
```

### Re-push after code changes

```bash
docker tag jkt-planning:v1-amd64 anmolsaini07/jkt-planning:v1-amd64
docker push anmolsaini07/jkt-planning:v1-amd64
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `404 plan_params` | `plan_id` missing from `jkt_plan_params` — INSERT a row first |
| `404 demand` | No rows in `jkt_demand` for this `plan_id` — seed demand first |
| `409 duplicate_check` | `plan_id` already scheduled — manually DELETE its rows from the 3 output tables to re-run |
| `412 upload` | Schedule Excel missing before Phase C, OR Machine Utilization sheet has no machines |
| Browser times out at 30/60s | JS client default timeout. Set 600_000 ms explicitly. |
| `Address already in use` on port 5001 | Stale Flask. `lsof -nP -iTCP:5001` → `kill <pid>` |
| `Cannot connect to the Docker daemon` | Docker Desktop not running. `open -a Docker` |
| `externally-managed-environment` on pip | Use a virtual env: `python3 -m venv .venv && source .venv/bin/activate` |
| amd64 build slow on M-series Mac | QEMU emulation — ~5–10 min vs 1 min native. One-time cost. |
| Smoke test fails on `_count_demand_skus` | `jkt_demand` empty for the auto-discovered test plan — DB state drift, not a code issue |

---

## Known operational constraints

| Constraint | Severity | Reason |
|------------|----------|--------|
| **Single-worker only** | Medium | LP scheduler's `Config` is process-global; `gunicorn --workers 1` enforced. Multi-worker would race on plan dates. |
| **Append-only — no in-place updates** | By design | Same plan_id rejected with HTTP 409. Manual DB DELETE required to retry. |
| **Synchronous API** | Low | LP holds the connection for 1–5 min. Clients need long timeouts. |
| **No auth / CORS / rate-limiting** | Low | Suitable for internal pilot. Add before exposing externally. |
| **Even-tyre rule** | By design | Plant produces tyres only in even counts — per-SKU totals are rounded UP to the next even number (+1 tyre to first slot if odd). |

---

## Project layout

(See [Architecture](#architecture) above for the full tree.)

### Key invariants

- **One source of truth per concept**:
  - DB credentials → `.env` (not in YAML, not in code)
  - YAML defaults → `config.yaml`
  - Per-plan overrides → `jkt_plan_params` row
- **Shared helpers** to prevent drift:
  - `_round_up_to_even()` in [kpi_writer.py](V1/reports/kpi_writer.py) — used by both writers
  - `compute_daily_utilisation()` in [capacity_writer.py](V1/reports/capacity_writer.py) — used by both daily and overall capacity KPIs
- **All `createdAt` timestamps**: IST via `now_ist()` regardless of host timezone

### Smoke test (`smoke_test.py`)

32 checks covering: filesystem, imports, config, DB connectivity, table presence, demand loaders, CPS math, KPI math, capacity math, Flask routes, error mapping. Zero side effects on the DB. Run before every Docker push.

### File-history note

Legacy V0 scripts kept for reference (safe to delete once V1 is proven):

- `demand_extract.py`, `demand_extract.yaml` — replaced by `V1/routes/demand_route.py`
- `jk_curing_lp_PCR.py` — copied unchanged into `V1/routes/schedule_route.py`
- `upload.ipynb` — logic split across `V1/reports/{kpi,plan,capacity}_writer.py`
