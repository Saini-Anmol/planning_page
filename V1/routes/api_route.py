"""
HTTP layer — wraps the demand → schedule → upload pipeline.

Endpoint:
    POST /app/v1/jkt/planning-scheduling/plan/generate-
    body: {"plan_id": "<id>"}

Returns JSON. Synchronous: response arrives after the full pipeline completes
(can take several minutes; LP solve dominates).
"""
from __future__ import annotations

import time
import traceback

from flask import Blueprint, jsonify, request

from V1.routes import demand_route, schedule_route, upload_route
from V1.setups import plan_status
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


bp = Blueprint("planning", __name__, url_prefix="/app/v1/jkt/planning-scheduling")

# Mirrors jkt_plan_params.plan_id VARCHAR(50). Keep in sync if the DB column changes.
_PLAN_ID_MAX_LEN = 50


def _extract_plan_id(req) -> tuple[str | None, str | None]:
    """Pull plan_id out of the request body. Returns (plan_id, error_message)."""
    # force=True parses JSON even when the client forgets Content-Type: application/json.
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


@bp.route("/plan/generate-", methods=["POST"])
def generate_plan():
    plan_id, err = _extract_plan_id(request)
    if err:
        return jsonify({"status": "error", "stage": "validation", "message": err}), 400

    cfg = config_loader.load()
    cfg["plan"]["plan_id"] = plan_id
    cfg = config_loader.resolve_paths(cfg)

    t0 = time.time()
    stage = "init"
    try:
        stage = "duplicate_check"
        plan_status.assert_not_already_scheduled(cfg["db"], plan_id)

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
            "plan_id": plan_id,
            "message": str(e),
        }), e.status_code
    except Exception as e:
        return jsonify({
            "status":  "error",
            "stage":   stage,
            "plan_id": plan_id,
            "message": str(e),
            "trace":   traceback.format_exc().splitlines()[-6:],
        }), 500

    return jsonify({
        "status":           "success",
        "plan_id":          plan_id,
        "elapsed_seconds":  round(time.time() - t0, 2),
    })


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
