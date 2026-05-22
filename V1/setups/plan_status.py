"""Append-only enforcement: each plan_id may be scheduled exactly once.

Re-running a plan would create duplicate rows in jkt_plan_kpis / jkt_plan /
jkt_plan_capacityUtilisation. Calling assert_not_already_scheduled() before
starting a run rejects duplicates at the HTTP boundary with a 409 Conflict.

This is by design — the pipeline never deletes or truncates. To re-run a
plan_id, clean its rows out of the 3 target tables manually.
"""
from __future__ import annotations

from V1.utilities.db import connect
from V1.utilities.exceptions import PipelineError


_OUTPUT_TABLES = ("jkt_plan_kpis", "jkt_plan", "jkt_plan_capacityUtilisation")


def assert_not_already_scheduled(db_cfg: dict, plan_id: str) -> None:
    """Raise PipelineError(409) if any output table already has rows for plan_id."""
    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        for table in _OUTPUT_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE plan_id = %s", (plan_id,))
            n = cur.fetchone()[0]
            if n > 0:
                raise PipelineError(
                    f"plan_id={plan_id!r} already has {n} rows in {table}. "
                    f"Each plan_id is append-only — clean those rows manually to re-run.",
                    stage="duplicate_check",
                    status_code=409,
                )
        cur.close()
    finally:
        conn.close()
