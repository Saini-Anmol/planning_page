"""Read a single plan row from jkt_plan_params (shared by demand + schedule routes)."""
from __future__ import annotations

from V1.utilities.db import connect
from V1.utilities.exceptions import PipelineError


def fetch(db_cfg: dict, plan_id: str) -> dict:
    conn = connect(db_cfg)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM jkt_plan_params WHERE plan_id = %s", (plan_id,))
        row = cur.fetchone()
        if not row:
            raise PipelineError(
                f"plan_id={plan_id!r} not found in jkt_plan_params",
                stage="plan_params", status_code=404,
            )
        return row
    finally:
        cur.close()
        conn.close()
