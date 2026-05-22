"""
Phase C — upload the LP scheduler's Excel output into 3 DB tables:
  - jkt_plan_kpis                  (kpi_writer)
  - jkt_plan                       (plan_writer)
  - jkt_plan_capacityUtilisation   (capacity_writer)
"""
from __future__ import annotations

from pathlib import Path

from V1.reports import kpi_writer, plan_writer, capacity_writer
from V1.setups import plan_params
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


def _resolve_schedule_path(cfg: dict) -> Path:
    """The scheduler writes PCR_Schedule_<plan_id>_<plan_start>_<n>days.xlsx into output/."""
    plan_id = cfg["plan"]["plan_id"]
    plan_row = plan_params.fetch(cfg["db"], plan_id)
    import datetime as _dt
    ps = plan_row["planStartDate"]
    pe = plan_row["planEndDate"]
    if isinstance(ps, _dt.datetime): ps = ps.date()
    if isinstance(pe, _dt.datetime): pe = pe.date()
    days = (pe - ps).days + 1
    name = cfg["schedule"]["output_excel"].format(
        plan_id=plan_id, plan_start=ps, planning_days=days,
    )
    return config_loader.output_dir(cfg) / name


def run(cfg: dict, schedule_path: Path | None = None) -> None:
    plan_id    = cfg["plan"]["plan_id"]
    created_by = cfg["upload"]["created_by"]
    db_cfg     = cfg["db"]
    path       = schedule_path or _resolve_schedule_path(cfg)

    if not path.exists():
        raise PipelineError(
            f"Schedule output not found: {path}. Run schedule_route first.",
            stage="upload", status_code=412,
        )

    print(f"[upload] reading {path.name}")
    kpi_writer.upload(path, plan_id, created_by, db_cfg)
    plan_writer.upload(path, plan_id, created_by, db_cfg)
    capacity_writer.upload(path, plan_id, created_by, db_cfg)
