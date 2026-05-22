"""
Pipeline entry point.

    python main.py                       # full pipeline: A -> B -> C
    python main.py --phase A             # only demand extract
    python main.py --phase B             # only LP scheduler
    python main.py --phase C             # only upload to DB
    python main.py --plan-id <ID>        # override config.yaml plan_id

Phases:
    A  demand_route    — compute ConsolidatedPriorityScore -> requirement_summary_<plan_id>.xlsx
    B  schedule_route  — LP scheduler                       -> PCR_Schedule_<plan_id>_*.xlsx
    C  upload_route    — push schedule back into DB         -> jkt_plan_kpis / jkt_plan / jkt_plan_capacityUtilisation
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path so `V1.*` imports resolve regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from V1.routes import demand_route, schedule_route, upload_route
from V1.setups import plan_status
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


def main() -> int:
    parser = argparse.ArgumentParser(description="JK Tyre PCR planning pipeline.")
    parser.add_argument("--config",  help="Path to config.yaml (default: config/config.yaml)")
    parser.add_argument("--plan-id", help="Override plan.plan_id from config")
    parser.add_argument("--phase",   choices=["A", "B", "C"],
                        help="Run a single phase (default: all)")
    args = parser.parse_args()

    cfg = config_loader.load(args.config)
    if args.plan_id:
        cfg["plan"]["plan_id"] = args.plan_id
    cfg = config_loader.resolve_paths(cfg)

    print(f"[main] plan_id = {cfg['plan']['plan_id']}")

    try:
        # Append-only guard — same check the API performs. Only meaningful
        # when this run will eventually hit Phase C (DB inserts).
        if args.phase in (None, "C"):
            plan_status.assert_not_already_scheduled(cfg["db"], cfg["plan"]["plan_id"])

        if args.phase in (None, "A"):
            demand_route.run(cfg)
        if args.phase in (None, "B"):
            schedule_route.run(cfg)
        if args.phase in (None, "C"):
            upload_route.run(cfg)
    except PipelineError as e:
        print(f"[main] ABORT (stage={e.stage}, code={e.status_code}): {e}",
              file=sys.stderr)
        return 1

    print("[main] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
