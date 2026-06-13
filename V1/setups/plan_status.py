"""Append-only enforcement: each plan_id may be scheduled exactly once.

Re-running a plan would create duplicate rows in jkt_plan_kpis / jkt_plan /
jkt_plan_capacityUtilisation. Calling assert_not_already_scheduled() before
starting a run rejects duplicates at the HTTP boundary with a 409 Conflict.

The duplicate check guards the 3 core output tables below. (jkt_plan_Infeasibility
is written last and auto-created, and is implicitly protected — a re-run is already
blocked by the kpis row written first.)

This is by design — the pipeline never deletes or truncates. To re-run a plan_id,
clean its rows out of ALL 4 output tables manually (the 3 below + jkt_plan_Infeasibility).
"""
from __future__ import annotations

from V1.utilities.db import connect, safe_table
from V1.utilities.exceptions import PipelineError


_OUTPUT_TABLES = ("jkt_plan_kpis", "jkt_plan", "jkt_plan_capacityUtilisation")


def assert_not_already_scheduled(
    db_cfg: dict, plan_id: str, tables: tuple[str, ...] | None = None
) -> None:
    """Raise PipelineError(409) if any output table already has rows for plan_id.

    `tables` lets the simulation pipeline check jkt_sim_* outputs instead of
    the default jkt_* ones.
    """
    output_tables = tables or _OUTPUT_TABLES
    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        for table in output_tables:
            table = safe_table(table)
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
