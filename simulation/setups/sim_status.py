"""Append-only enforcement for the simulation pipeline.

Each plan_id may be SIMULATED at most once — re-running a simulation would
duplicate rows in:
    jkt_sim_plan_kpis
    jkt_sim_plan
    jkt_sim_plan_capacityUtilisation
    jkt_sim_plan_Infeasibility            (has auto-increment PK — no DB-level
                                           protection against duplicate rows
                                           for the same plan_id, so the
                                           duplicate check MUST include it)

This wraps V1's plan_status.assert_not_already_scheduled() with the SIM output
table names so the simulation API layer can call it without knowing about
table resolution details.

To re-run a simulation, manually clean ALL FOUR sim output tables for that
plan_id (same operational discipline as the planning pipeline).
"""
from __future__ import annotations

from V1.setups import plan_status


def assert_not_already_simulated(db_cfg: dict, plan_id: str, tbl: dict) -> None:
    """Raise PipelineError(409) if any sim output table already has rows for plan_id.

    `tbl` is cfg["tbl"] (filled by apply_mode("simulation")). Reads:
        tbl["plan_kpis"]     -> jkt_sim_plan_kpis
        tbl["plan"]          -> jkt_sim_plan
        tbl["capacity"]      -> jkt_sim_plan_capacityUtilisation
        tbl["infeasibility"] -> jkt_sim_plan_Infeasibility
    """
    output_tables = (
        tbl["plan_kpis"],
        tbl["plan"],
        tbl["capacity"],
        tbl["infeasibility"],
    )
    plan_status.assert_not_already_scheduled(db_cfg, plan_id, output_tables)
