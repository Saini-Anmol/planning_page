"""Simulation Phase C — re-exports V1's upload_route.

Writes the schedule Excel's contents to:
    jkt_sim_plan_kpis                  (one row of KPIs)
    jkt_sim_plan                       (per-shift production rows)
    jkt_sim_plan_capacityUtilisation   (per-date utilization)

Target tables are resolved via cfg["tbl"][...] which is filled in by
apply_mode("simulation"). The writers' INSERT logic is unchanged.
"""
from __future__ import annotations

from V1.routes.upload_route import run  # noqa: F401  (re-export)
