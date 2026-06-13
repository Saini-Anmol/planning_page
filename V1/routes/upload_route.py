"""
Phase C — upload the LP scheduler's Excel output into 4 DB tables:
  - jkt_plan_kpis                  (kpi_writer)
  - jkt_plan                       (plan_writer)
  - jkt_plan_capacityUtilisation   (capacity_writer)
  - jkt_plan_Infeasibility         (infeasibility_writer — unmet + default-CT SKUs)

(In simulation mode each resolves to its jkt_sim_* counterpart via cfg["tbl"].)
"""
from __future__ import annotations

from pathlib import Path

from V1.reports import kpi_writer, plan_writer, capacity_writer, infeasibility_writer
from V1.setups import plan_params
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


def _resolve_schedule_path(cfg: dict) -> Path:
    """The scheduler writes PCR_Schedule_<plan_id>_<plan_start>_<n>days.xlsx into output/."""
    plan_id = cfg["plan"]["plan_id"]
    tbl = cfg.get("tbl", {})
    plan_row = plan_params.fetch(cfg["db"], plan_id, tbl.get("plan_params", "jkt_plan_params"))
    import datetime as _dt
    ps = plan_row["planStartDate"]
    pe = plan_row["planEndDate"]
    if isinstance(ps, _dt.datetime): ps = ps.date()
    if isinstance(pe, _dt.datetime): pe = pe.date()
    days = (pe - ps).days + 1
    name = cfg["schedule"]["output_excel"].format(
        mode_tag=config_loader.mode_file_tag(cfg),
        plan_id=plan_id, plan_start=ps, planning_days=days,
    )
    return config_loader.output_dir(cfg) / name


def run(cfg: dict, schedule_path: Path | None = None) -> None:
    plan_id    = cfg["plan"]["plan_id"]
    created_by = cfg["upload"]["created_by"]
    db_cfg     = cfg["db"]
    tbl        = cfg.get("tbl", {})
    path       = schedule_path or _resolve_schedule_path(cfg)

    if not path.exists():
        raise PipelineError(
            f"Schedule output not found: {path}. Run schedule_route first.",
            stage="upload", status_code=412,
        )

    print(f"[upload] reading {path.name}")
    kpi_writer.upload(path, plan_id, created_by, db_cfg, tbl)
    plan_writer.upload(path, plan_id, created_by, db_cfg, tbl)
    capacity_writer.upload(path, plan_id, created_by, db_cfg, tbl)
    infeasibility_writer.upload(path, plan_id, created_by, db_cfg, tbl)
