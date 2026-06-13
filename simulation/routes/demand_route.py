"""Simulation Phase A — re-exports V1's demand_route.

The CPS computation logic is identical between planning and simulation. The
ONLY difference is which DB table demand is read from, and that is controlled
by cfg["tbl"]["demand"] (set to "jkt_sim_demand" by apply_mode("simulation")).

Kept as a separate module so the simulation/ folder shows all entry points
for the simulation pipeline at a glance.
"""
from __future__ import annotations

from V1.routes.demand_route import run  # noqa: F401  (re-export)
