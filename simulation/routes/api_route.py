"""
HTTP layer for the SIMULATION page.

Endpoint:
    POST /app/v1/jkt/planning-scheduling/simulation/generate-plan
    body: {"plan_id": "<id>"}

The simulation pipeline reads from jkt_sim_* tables and writes to jkt_sim_*
output tables. It reuses V1's planning engine internals — the LP solver,
KPI/plan/capacity writers, and post-processing are all shared. Only the
table names (and this entry point) are different.

Mode resolution is handled by V1.utilities.config_loader.apply_mode("simulation"),
which inserts "sim_" right after "jkt_" in every logical table name.
"""
from __future__ import annotations

import time
import traceback

from flask import Blueprint, jsonify, request

from simulation.routes import demand_route, schedule_route, upload_route
from simulation.setups import sim_status
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


# =============================================================================
# PUBLIC API ENDPOINT — simulation page
# =============================================================================
API_HOST          = "35.208.174.2"
API_PORT          = 5001
API_URL_PREFIX    = "/app/v1/jkt/planning-scheduling"
API_SIMULATE_PATH = "/simulation/generate-plan"

GENERATE_URL = f"http://{API_HOST}:{API_PORT}{API_URL_PREFIX}{API_SIMULATE_PATH}"

bp = Blueprint("simulation", __name__, url_prefix=API_URL_PREFIX)

# Mirror jkt_sim_plan_params.plan_id VARCHAR(50). Keep in sync with the column.
_PLAN_ID_MAX_LEN = 50


def _extract_plan_id(req) -> tuple[str | None, str | None]:
    """Pull plan_id out of the request body. Returns (plan_id, error_message)."""
    payload = req.get_json(silent=True, force=True)
    if payload is None:
        return None, "request body is not valid JSON"
    if not isinstance(payload, dict):
        return None, "request body must be a JSON object like {\"plan_id\": \"...\"}"

    plan_id = payload.get("plan_id")
    if plan_id is None:
        return None, "missing 'plan_id' in request body"
    if not isinstance(plan_id, str):
        return None, f"'plan_id' must be a string, got {type(plan_id).__name__}"

    plan_id = plan_id.strip()
    if not plan_id:
        return None, "'plan_id' is empty after trimming whitespace"
    if len(plan_id) > _PLAN_ID_MAX_LEN:
        return None, f"'plan_id' is {len(plan_id)} chars; max allowed is {_PLAN_ID_MAX_LEN}"

    return plan_id, None


@bp.route(API_SIMULATE_PATH, methods=["POST"])
def generate_simulation():
    """Run the simulation pipeline (mode=simulation → jkt_sim_* tables)."""
    plan_id, err = _extract_plan_id(request)
    if err:
        return jsonify({"status": "error", "stage": "validation", "message": err}), 400

    cfg = config_loader.load(mode="simulation")
    cfg["plan"]["plan_id"] = plan_id
    cfg = config_loader.resolve_paths(cfg)

    t0 = time.time()
    stage = "init"
    try:
        stage = "duplicate_check"
        sim_status.assert_not_already_simulated(cfg["db"], plan_id, cfg["tbl"])

        stage = "demand"
        demand_route.run(cfg)

        stage = "schedule"
        schedule_route.run(cfg)

        stage = "upload"
        upload_route.run(cfg)
    except PipelineError as e:
        return jsonify({
            "status":  "error",
            "stage":   e.stage or stage,
            "mode":    "simulation",
            "plan_id": plan_id,
            "message": str(e),
        }), e.status_code
    except Exception as e:
        return jsonify({
            "status":  "error",
            "stage":   stage,
            "mode":    "simulation",
            "plan_id": plan_id,
            "message": str(e),
            "trace":   traceback.format_exc().splitlines()[-6:],
        }), 500

    return jsonify({
        "status":          "success",
        "mode":            "simulation",
        "plan_id":         plan_id,
        "elapsed_seconds": round(time.time() - t0, 2),
    })
