# JK Tyre — PCR Curing Planning & Scheduling Pipeline (V1)

Modular pipeline that consumes a `plan_id`, computes per-SKU priority scores,
runs a Linear Programming (LP) curing scheduler, and writes the resulting
schedule back into `jkplanningV1.*` MySQL tables. Exposes both a CLI and an
HTTP API.

---

## Architecture

```
db_data_upload/
├── main.py                            CLI entry — runs phases A/B/C
├── app.py                             Flask app entry — port 5001
├── smoke_test.py                      No-side-effect health check
├── config/
│   └── config.yaml                    Unified config (DB, plan, weights, knobs)
├── V1/
│   ├── routes/                        Pipeline + HTTP route handlers
│   │   ├── demand_route.py            Phase A — CPS from jkt_demand + jkt_plan_params
│   │   ├── schedule_route.py          Phase B — LP solver (1681 lines, internal)
│   │   ├── upload_route.py            Phase C — orchestrates 3 DB writers
│   │   └── api_route.py               POST /plan/generate- handler
│   ├── setups/                        Data loaders
│   │   ├── plan_params.py             Read jkt_plan_params row
│   │   └── demand_db.py               Read jkt_demand rows
│   ├── utilities/                     Shared helpers
│   │   ├── config_loader.py           YAML + path resolution
│   │   └── db.py                      mysql.connector + sqlalchemy factories
│   └── reports/                       DB writers
│       ├── kpi_writer.py              → jkt_plan_kpis
│       ├── plan_writer.py             → jkt_plan
│       └── capacity_writer.py         → jkt_plan_capacityUtilisation
├── input/                             Legacy reference Excel files
└── output/                            Generated schedules and intermediates
```

---

## Pipeline overview

| Phase | Module                   | Reads                                                                                              | Writes                                                                  |
| ----- | ------------------------ | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **A** | `demand_route`           | `jkt_plan_params`, `jkt_demand`                                                                    | `output/requirement_summary_<plan_id>.xlsx`                             |
| **B** | `schedule_route`         | requirement_summary + 6 master tables (`Master_*`, `gt_inventory_manual`, `Daily_Running_Moulds`)  | `output/PCR_Schedule_<plan_id>_<start>_<n>days.xlsx`                    |
| **C** | `upload_route`           | schedule Excel                                                                                     | `jkt_plan_kpis`, `jkt_plan`, `jkt_plan_capacityUtilisation`             |

### Phase A — ConsolidatedPriorityScore (CPS)

For each SKU in `jkt_demand` filtered by `plan_id`, compute:

```
CPS(s) = w_market * norm_market(s) + w_qty * norm_req(s) + w_date * norm_date(s)
```

- `norm_market` — maps `Market` value (OE/Replacement/Export/…) to the rank column in `jkt_plan_params` (`oe`/`re`/`export`/…), then inverts so rank 1 → score 7 → `norm=1.0`.
- `norm_req` — min-max normalize `requirement` across the plan.
- `norm_date` — inverted urgency: `(ttt_max − ttt) / (ttt_max − ttt_min)` where `ttt = (deliveryDate − planStartDate).days`. Missing dates fall back to plan end (least urgent).
- **Weights**: DB wins (`marketWeightage`/`quantityWeightage`/`targetdateWeightage`); YAML defaults fill NULL/0; renormalized to sum=1.
- **Output**: `requirement_summary_<plan_id>.xlsx` with `SKUCode | SKU Description | Order Type | Market | Requirement | ConsolidatedPriorityScore`.

### Phase B — LP scheduler

Original `jk_curing_lp_PCR.py` (kept intact) split into 5 internal phases:

1. ETL from DB (`Master_Curing_Design_CycleTime`, `Master_Curing_Allowable_Machines_source`, `gt_inventory_manual`, `Master_WC_Master`, `Daily_Running_Moulds`, `Master_Mapping_Mould_SKU`)
2. LP solve (scipy.linprog) — globally optimal press-minute allocation
3. Rounding — continuous LP solution → integer cycles
4. Schedule build — shift-wise row-level schedule + changeovers
5. Excel export — 5-sheet workbook (Demand Fulfillment / Machine Schedule / Shift Schedule / Machine Utilization / Mould Tracker)

Driven by `Config.PLAN_DATE` and `Config.PLANNING_DAYS`, which `schedule_route.run()` overrides from `jkt_plan_params` at call time.

### Phase C — DB upload

Reads the schedule Excel and inserts:

| Writer              | Source sheet         | Target table                       | Rows                                  |
| ------------------- | -------------------- | ---------------------------------- | ------------------------------------- |
| `kpi_writer`        | Demand Fulfillment   | `jkt_plan_kpis`                    | 1                                     |
| `plan_writer`       | Shift Schedule       | `jkt_plan`                         | ~14k (every scheduled slot)           |
| `capacity_writer`   | Shift Schedule       | `jkt_plan_capacityUtilisation`     | one per date (midnight-split EndTime–StartTime; per-machine capped at 100%) |

---

## Setup

### 1. Python dependencies

```bash
python3 -m pip install --user \
    flask mysql-connector-python sqlalchemy pymysql \
    openpyxl pandas numpy scipy pyyaml
```

### 2. Configure

Edit `config/config.yaml` — the only knob you usually touch is `plan.plan_id`. DB creds, scheduler constants, market aliases, fallback weights all live here.

### 3. Verify

```bash
python3 smoke_test.py
```

Should print `16 passed, 0 failed`. Validates filesystem, imports, DB connectivity, table presence, and the Flask route surface — without running the LP solver or modifying any DB tables.

---

## Running

### CLI (batch / development)

```bash
python3 main.py                                       # full pipeline with config.yaml plan_id
python3 main.py --plan-id BTP_June_Plan_V1184_472835  # override plan_id
python3 main.py --phase A                             # only Phase A
python3 main.py --phase B
python3 main.py --phase C
python3 main.py --config custom.yaml                  # alternate config
```

### HTTP API

Start the server:

```bash
python3 app.py
# listening on http://localhost:5001
```

**Generate plan:**

```bash
curl -X POST http://localhost:5001/app/v1/jkt/planning-scheduling/plan/generate- \
     -H "Content-Type: application/json" \
     -d '{"plan_id": "BTP_June_Plan_V1184_472835"}'
```

Synchronous — blocks for the full LP solve (~1–5 min depending on plan size).

Success response (200):
```json
{
  "status": "success",
  "plan_id": "BTP_June_Plan_V1184_472835",
  "elapsed_seconds": 142.7,
  "schedule_file": "output/PCR_Schedule_<plan_id>_<start>_<n>days.xlsx"
}
```

Error response (500):
```json
{
  "status": "error",
  "stage": "demand|schedule|upload",
  "plan_id": "…",
  "message": "human-readable reason",
  "trace": ["last 6 lines of traceback"]
}
```

Missing `plan_id` returns 400.

**Health check:**

```bash
curl http://localhost:5001/app/v1/jkt/planning-scheduling/health
# {"status": "ok"}
```

---

## Configuration reference

`config/config.yaml`:

| Section                          | Purpose                                                                       |
| -------------------------------- | ----------------------------------------------------------------------------- |
| `plan.plan_id`                   | Primary key in `jkt_plan_params`. Single source of truth.                     |
| `db.{host,port,user,password,database}` | MySQL connection. Currently hardcoded — see Gaps for hardening notes.  |
| `paths.{input_dir, output_dir}`  | Project-root-relative paths.                                                  |
| `demand.market_aliases`          | Free-text market name → `jkt_plan_params` column name.                        |
| `demand.default_weights`         | Fallbacks for missing/zero DB weights. Always renormalized to sum=1.          |
| `demand.market_score_scale`      | Min/max market score range (1–7).                                             |
| `schedule.*`                     | LP scheduler constants pushed into `Config` class at runtime.                 |
| `upload.created_by`              | String written to all `createdBy` columns.                                    |

---

## Database tables touched

**Read:**

| Table                                      | Purpose                                  |
| ------------------------------------------ | ---------------------------------------- |
| `jkt_plan_params`                          | Plan-level inputs (dates, ranks, weights)|
| `jkt_demand`                               | Per-SKU demand (req/market/delivery)     |
| `Master_Curing_Design_CycleTime`           | SKU → cure time                          |
| `Master_Curing_Allowable_Machines_source`  | SKU → eligible machines matrix           |
| `gt_inventory_manual`                      | Green-tyre on-hand inventory             |
| `Master_WC_Master`                         | Work-centre / machine master             |
| `Daily_Running_Moulds`                     | Snapshot of moulds currently on presses  |
| `Master_Mapping_Mould_SKU`                 | Mould ↔ SKU compatibility                |

**Written:**

| Table                              | Writer              | Rows per run                  |
| ---------------------------------- | ------------------- | ----------------------------- |
| `jkt_plan_kpis`                    | `kpi_writer`        | 1                             |
| `jkt_plan`                         | `plan_writer`       | ~14k                          |
| `jkt_plan_capacityUtilisation`     | `capacity_writer`   | one per planning day          |

---

## Known gaps & hardening backlog

See section below: _"Architecture status & gaps"_ for a complete list.

---

## Troubleshooting

| Symptom                                                            | Fix                                                                                                          |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `plan_id=... not found in jkt_plan_params`                         | Plan row missing in `jkt_plan_params`. Add the row or pick a real `plan_id`.                                 |
| `No demand rows in jkt_demand for plan_id=...`                     | `jkt_demand` table has no rows tagged with this `plan_id`. Seed `jkt_demand` first.                          |
| LP solver runs but produces empty schedule                         | Mould eligibility likely too strict — check `schedule.permissive_mould_eligibility: true` in config.         |
| `ImportError: sqlalchemy not installed`                            | `python3 -m pip install --user sqlalchemy pymysql`                                                           |
| Flask 500 with `stage: upload`                                     | Duplicate insert. The pipeline is **not idempotent** — see Gaps.                                             |
| Browser request times out at 30/60s                                | Sync endpoint, LP solve takes minutes. Increase `proxy_read_timeout` if fronted by nginx, or use `curl`.    |

---

## File-history note

These legacy files were the V0 working scripts and are kept as reference:

- `demand_extract.py`, `demand_extract.yaml` — standalone CPS extractor (now in `V1/routes/demand_route.py`)
- `jk_curing_lp_PCR.py` — original 1595-line LP scheduler (copied unchanged into `V1/routes/schedule_route.py`)
- `upload.ipynb` — manual DB-upload notebook (logic now in `V1/reports/`)

Safe to delete once the V1 pipeline has been validated end-to-end in your environment.
