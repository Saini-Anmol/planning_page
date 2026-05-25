# JK Tyre — PCR Curing Planning & Scheduling Pipeline (V1)

Modular pipeline that consumes a `plan_id`, reads demand from `jkt_demand`,
computes per-SKU `ConsolidatedPriorityScore`, runs a Linear Programming (LP)
curing scheduler, and writes the resulting schedule back into the
`jkplanningV1.*` MySQL tables.

Exposes three entry points:
- **CLI** — `python3 main.py --plan-id <id>`
- **HTTP API** — Flask app on port 5001, drivable by your JS backend
- **Docker** — single container, published to Docker Hub for cross-platform deployment

---

## Architecture

```
db_data_upload/
├── main.py                            CLI entry — runs phases A/B/C
├── app.py                             Flask app entry — port 5001
├── smoke_test.py                      No-side-effect health check (21+5 checks)
├── Dockerfile                         python:3.11-slim + gunicorn (single worker)
├── requirements.txt                   Pinned Python deps
├── .env                               DB credentials (gitignored, dockerignored)
├── .env.example                       Template for .env (committed)
├── .gitignore                         Excludes secrets and runtime artifacts
├── .dockerignore                      Excludes secrets and legacy files from image
├── config/
│   └── config.yaml                    Unified config (plan, scheduler knobs, weights)
├── V1/
│   ├── routes/                        Pipeline + HTTP route handlers
│   │   ├── api_route.py               POST /plan/generate-plan handler + URL constants
│   │   ├── demand_route.py            Phase A — CPS from jkt_demand + jkt_plan_params
│   │   ├── schedule_route.py          Phase B — LP solver (1681 lines, threading.Lock serialized)
│   │   └── upload_route.py            Phase C — orchestrates 3 DB writers
│   ├── setups/                        Data loaders
│   │   ├── plan_params.py             Read jkt_plan_params row (404 if missing)
│   │   ├── demand_db.py               Read jkt_demand rows
│   │   └── plan_status.py             Append-only guard (409 if plan_id already scheduled)
│   ├── utilities/                     Shared helpers
│   │   ├── config_loader.py           YAML + env-var override (auto-loads .env)
│   │   ├── db.py                      mysql.connector + sqlalchemy factories
│   │   └── exceptions.py              PipelineError class (HTTP-aware)
│   └── reports/                       DB writers
│       ├── kpi_writer.py              → jkt_plan_kpis
│       ├── plan_writer.py             → jkt_plan (with skuDescription enriched from jkt_demand)
│       └── capacity_writer.py         → jkt_plan_capacityUtilisation (170-machine fleet, plan-window filtered)
├── input/                             Legacy reference Excel files (no longer read)
└── output/                            Generated schedules and intermediates
```

---

## API endpoint

The full HTTP URL is exported as a constant in [V1/routes/api_route.py:43](V1/routes/api_route.py#L43):

```
POST http://<host>:5001/app/v1/jkt/planning-scheduling/plan/generate-plan
GET  http://<host>:5001/app/v1/jkt/planning-scheduling/health
```

Production host: `35.208.174.2:5001`. Locally: `localhost:5001`.

### Request body

```json
{ "plan_id": "BTP_June_Plan_V_384072" }
```

### Response shape

**Success (200):**
```json
{
  "status": "success",
  "plan_id": "BTP_June_Plan_V_384072",
  "elapsed_seconds": 53.42
}
```

**Errors** — `status: "error"` with appropriate HTTP code:

| Code | When | `stage` |
|---|---|---|
| 400 | Missing/empty/non-string `plan_id`, malformed JSON | `validation` |
| 404 | `plan_id` not in `jkt_plan_params`, or no rows in `jkt_demand` | `plan_params` / `demand` |
| 409 | This `plan_id` is already scheduled (append-only guard) | `duplicate_check` |
| 412 | Schedule Excel went missing before upload | `upload` |
| 422 | All three CPS weights are zero | `demand` |
| 500 | Unexpected — returns last 6 lines of traceback | varies |

---

## Pipeline phases

| Phase | Module                   | Reads                                                                                              | Writes                                                                  |
| ----- | ------------------------ | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **A** | `demand_route`           | `jkt_plan_params`, `jkt_demand`                                                                    | `output/requirement_summary_<plan_id>.xlsx`                             |
| **B** | `schedule_route`         | requirement_summary + 6 master tables                                                              | `output/PCR_Schedule_<plan_id>_<start>_<n>days.xlsx`                    |
| **C** | `upload_route`           | schedule Excel                                                                                     | `jkt_plan_kpis`, `jkt_plan`, `jkt_plan_capacityUtilisation`             |

### Phase A — ConsolidatedPriorityScore (CPS)

For each SKU in `jkt_demand` filtered by `plan_id`:

```
CPS(s) = w_market · norm_market(s) + w_qty · norm_req(s) + w_date · norm_date(s)
```

- **norm_market** — maps `Market` value (`OE` / `Replacement` / `Export` / …) to the rank column in `jkt_plan_params` (`oe`/`re`/`export`/…), then inverts so rank 1 → score 7 → norm 1.0.
- **norm_req** — min-max normalize across the plan.
- **norm_date** — inverted urgency: `(ttt_max − ttt) / (ttt_max − ttt_min)`. Missing dates fall back to plan end (least urgent).
- **Weights** — DB `marketWeightage` / `quantityWeightage` / `targetdateWeightage` win; YAML defaults fill NULL/0; renormalized to sum=1.

### Phase B — LP scheduler

Original 1,595-line scheduler (`jk_curing_lp_PCR.py`) wrapped into `V1/routes/schedule_route.py`. Internals unchanged. Wrapper:
- Fetches `planStartDate` / `planEndDate` from `jkt_plan_params` at call time
- Pushes config values into the scheduler's legacy `Config` class
- Holds `threading.Lock` around the LP solve so concurrent requests serialize

Five internal steps:
1. ETL from 6 DB master tables
2. LP solve (`scipy.optimize.linprog`)
3. Round continuous → integer cycles
4. Build shift-wise schedule + changeover slots
5. Excel export (5 sheets)

### Phase C — DB upload

| Writer | Source sheet | Target | Rows / run |
|---|---|---|---|
| `kpi_writer` | Demand Fulfillment | `jkt_plan_kpis` | 1 (demandSKU from jkt_demand, planSKU from sheet) |
| `plan_writer` | Shift Schedule | `jkt_plan` | one per scheduled slot (skuDescription enriched from jkt_demand) |
| `capacity_writer` | Shift Schedule | `jkt_plan_capacityUtilisation` | one per planning day (170-machine fleet denominator, plan-window filtered, midnight-split) |

---

## Setup (local development)

### 1. Python dependencies

```bash
python3 -m pip install --user -r requirements.txt
```

Tested on Python 3.9–3.11. (Docker image uses 3.11.)

### 2. Configure secrets

Copy the template and fill in the DB credentials:

```bash
cp .env.example .env
$EDITOR .env
```

The `.env` file is gitignored AND dockerignored — it never leaves your machine. The pipeline reads it at startup via [V1/utilities/config_loader.py](V1/utilities/config_loader.py).

### 3. Set the plan_id

Edit `config/config.yaml`:

```yaml
plan:
  plan_id: BTP_June_Plan_V_384072
```

This is overridden at runtime by `--plan-id` (CLI) or the request body (API).

### 4. Smoke-test the install

```bash
python3 smoke_test.py
```

Expected output:
```
26 passed, 0 failed
```

or, when `jkt_demand` has no usable plan_id (DB drift):

```
21 passed, 5 skipped, 0 failed
```

5 SKIP results are expected when no plan_id has rows in *both* `jkt_demand` and `jkt_plan_params` — the test reports clearly and exits 0.

---

## Running

### CLI (batch / development)

```bash
python3 main.py                                       # full pipeline using config.yaml plan_id
python3 main.py --plan-id BTP_June_Plan_V_384072      # override plan_id
python3 main.py --phase A                             # only Phase A
python3 main.py --phase B
python3 main.py --phase C
python3 main.py --config custom.yaml                  # alternate config
```

### HTTP API (Flask dev server)

```bash
python3 app.py
# listening on http://0.0.0.0:5001
```

Trigger a run from another terminal:

```bash
curl -X POST http://localhost:5001/app/v1/jkt/planning-scheduling/plan/generate-plan \
     -H "Content-Type: application/json" \
     -d '{"plan_id": "BTP_June_Plan_V_384072"}' \
     --max-time 600
```

Health check:
```bash
curl http://localhost:5001/app/v1/jkt/planning-scheduling/health
# {"status":"ok"}
```

### Docker

**Build locally:**
```bash
docker build -t jkt-planning:v1 .                     # native arch (arm64 on M-series Mac)
```

**Run:**
```bash
docker run --rm -p 5001:5001 \
    --env-file .env \
    -v $(pwd)/output:/app/output \
    --name jkt-api \
    jkt-planning:v1
```

The container exposes the same URL on the same port. `--env-file .env` injects the DB credentials at runtime (they are NOT baked into the image).

**Pull from Docker Hub** (no build required on the target machine):
```bash
docker pull anmolsaini07/jkt-planning:v1-amd64        # for Windows / Intel Linux
docker run --rm -p 5001:5001 --env-file .env -v $(pwd)/output:/app/output --name jkt-api anmolsaini07/jkt-planning:v1-amd64
```

Hub repo: https://hub.docker.com/r/anmolsaini07/jkt-planning

**Cross-architecture build:**
```bash
# amd64 (Windows, Intel Linux, AWS x86)
docker buildx build --platform linux/amd64 -t jkt-planning:v1-amd64 --load .

# Multi-arch push (one tag works everywhere)
docker buildx build --platform linux/amd64,linux/arm64 -t <hubuser>/jkt-planning:v1 --push .
```

---

## Configuration reference (`config/config.yaml`)

| Section                          | Purpose                                                                       |
| -------------------------------- | ----------------------------------------------------------------------------- |
| `plan.plan_id`                   | Default plan_id for CLI runs. API overrides via request body.                 |
| `db.{host,port,user,password,database}` | **Empty placeholders** — real values come from `JKT_DB_*` env vars (set in `.env`) |
| `paths.{input_dir, output_dir}`  | Project-root-relative paths                                                   |
| `demand.market_aliases`          | Free-text market name → `jkt_plan_params` column name                         |
| `demand.default_weights`         | Fallbacks for missing/zero DB weights. Always renormalized to sum=1.          |
| `demand.market_score_scale`      | Min/max market score range (1–7)                                              |
| `schedule.*`                     | LP scheduler constants pushed into legacy `Config` class at runtime           |
| `upload.created_by`              | String written to all `createdBy` columns                                     |

Environment variables (set in `.env`):

| Variable | Required | Used for |
|---|---|---|
| `JKT_DB_HOST` | ✅ | MySQL host |
| `JKT_DB_PORT` | ✅ | MySQL port (default 3306) |
| `JKT_DB_USER` | ✅ | MySQL user |
| `JKT_DB_PASSWORD` | ✅ | MySQL password |
| `JKT_DB_DATABASE` | ✅ | Database name (`jkplanningV1`) |

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
| `jkt_plan`                         | `plan_writer`       | one per scheduled slot (~1k–14k) |
| `jkt_plan_capacityUtilisation`     | `capacity_writer`   | one per planning day          |

The pipeline **never** deletes or truncates. The append-only guard in
[V1/setups/plan_status.py](V1/setups/plan_status.py) rejects re-runs of the
same `plan_id` with HTTP 409. To re-run a plan_id, manually delete its rows
from the 3 output tables first.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `plan_id=... not found in jkt_plan_params` (404) | Add a row to `jkt_plan_params` or pick a real `plan_id` |
| `No demand rows in jkt_demand for plan_id=...` (404) | Seed `jkt_demand` for this plan_id |
| `plan_id=... already has N rows in jkt_plan_kpis` (409) | Append-only guard. Manually `DELETE FROM jkt_plan_kpis / jkt_plan / jkt_plan_capacityUtilisation WHERE plan_id=...` then re-run |
| `Address already in use` on port 5001 | Stale process. `lsof -nP -iTCP:5001 -sTCP:LISTEN` → `kill <PID>` |
| LP runs but Shift Schedule sheet is empty | Mould eligibility too strict — verify `schedule.permissive_mould_eligibility: true` |
| Browser request times out at 30/60s | Sync endpoint, LP takes minutes. Use `curl --max-time 600` or set `proxy_read_timeout 600s` in nginx |
| `Cannot connect to the Docker daemon` | Docker Desktop not running. `open -a Docker` and wait for the whale icon |
| Build fails `Could not find a version that satisfies pymysql==X.Y.Z` | Pinned version doesn't exist on PyPI for that platform. Check `requirements.txt` |
| amd64 build slow on M-series Mac | Expected — QEMU emulation. ~5-10 min vs 1 min native. Worth it once per build. |

---

## Known operational constraints

| | Severity | Note |
|---|---|---|
| **Single-worker only** | Medium | The LP scheduler's `Config` class is process-global; `gunicorn --workers 1` enforced in Dockerfile. Multi-worker would race on plan dates. |
| **Append-only (no re-runs)** | By design | Same plan_id rejected with 409. Manual DB cleanup required to retry. |
| **Sync API** | Low | LP holds the HTTP connection for ~1–5 min. Clients (curl, axios) need a long timeout. |
| **No auth / CORS / rate-limiting** | Low | Fine for internal pilot. Add `flask-cors` + token check before exposing externally. |
| **`jkt_demand` schema lacks `Order Type`** | Cosmetic | Output Excel's `Order Type` column is always None. |

---

## File-history note

These legacy files predate the V1 architecture and are kept as reference. Safe to delete once V1 is validated end-to-end:

- `demand_extract.py`, `demand_extract.yaml` — standalone CPS extractor (now in `V1/routes/demand_route.py`)
- `jk_curing_lp_PCR.py` — original 1,595-line LP scheduler (copied into `V1/routes/schedule_route.py`)
- `upload.ipynb` — manual DB-upload notebook (logic now in `V1/reports/`)
