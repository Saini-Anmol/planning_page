"""Simulation Phase B — re-exports V1's schedule_route.

The LP solver, mould tracker, ScheduleBuilder, ExcelExporter, and the
post-process pass (Machine Schedule rebuild, Machine Utilization fix,
default CT=15 NA handling) are all shared with the planning pipeline.

Plan parameters are read from jkt_sim_plan_params via cfg["tbl"]["plan_params"]
(set by apply_mode("simulation")). The shared _RUN_LOCK in
V1.routes.schedule_route serializes both planning and simulation runs, so a
concurrent /plan + /simulation request cannot corrupt the global Config state.
"""
from __future__ import annotations

from V1.routes.schedule_route import run  # noqa: F401  (re-export)
