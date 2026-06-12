"""
Central version for the JK Tyre PCR planning suite (curing + building + coupling).

History
  v1-v4  : curing LP scheduler (internal LP versions, see curing_LP.py header)
  v5.0   : + units-per-cycle correction (C1), CSV data pipeline, heuristic curing
           scheduler, Daily Curing tab.
  v6.0   : + green-tyre BUILDING scheduler (JIT) and the COUPLED plan that makes
           curing and building mutually feasible (curing capped to building's
           deliverable supply).
  v6.1   : + TIME-PHASED coupling — curing is paced to building's daily output so
           cumulative cured never exceeds cumulative built (no curing a green tyre
           before it is made). Building efficiency factor made explicit (= 1.0,
           theoretical, per spec).
  v6.2   : + BUILDING CHANGEOVER time (Master_Building_ChangeoverTime) charged
           when a machine switches SKU; machines 6001-6004 appended to the DB
           cycle-time table (no longer a manual CSV addition).
  v6.3   : + SIZE-AWARE changeover — same-rim-size SKU switches use the cheaper
           SameSize_Min, different sizes use DifferentSize_Min. Rim size = chars
           9-10 of the SKU code. This is the current release.
"""

from datetime import datetime

SUITE_VERSION = "6.3"
VERSION_LABEL = f"v{SUITE_VERSION}"


def run_stamp() -> str:
    """Human-readable run timestamp (local time)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def run_tag() -> str:
    """Filesystem-safe run timestamp for filenames."""
    return datetime.now().strftime("%Y%m%d_%H%M")
