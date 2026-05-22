"""Insert one row into jkt_plan_kpis from the schedule's Demand Fulfillment summary."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import openpyxl

from V1.utilities.db import connect


def _find(pattern: str, text: str, default=None):
    m = re.search(pattern, text)
    if not m:
        return default
    return m.group(1)


def _require(pattern: str, text: str):
    v = _find(pattern, text)
    if v is None:
        raise ValueError(f"Pattern not found in summary: {pattern}")
    return v


def _count_sku_rows(ws) -> int:
    """Detail rows in the Demand Fulfillment sheet start at row 4; row 1 is the
    title, row 2 the summary, row 3 the column headers."""
    return sum(1 for r in range(4, ws.max_row + 1) if ws.cell(row=r, column=1).value)


def _count_demand_skus(plan_id: str, db_cfg: dict) -> int:
    """Distinct SKUs requested in jkt_demand for this plan."""
    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(DISTINCT skuCode) FROM jkt_demand WHERE plan_id = %s",
            (plan_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        cur.close()
        conn.close()


def upload(schedule_path: Path, plan_id: str, created_by: str, db_cfg: dict) -> None:
    wb = openpyxl.load_workbook(schedule_path, data_only=True)
    ws = wb["Demand Fulfillment"]
    summary = ws.cell(row=2, column=1).value or ""

    # demandSKU comes from jkt_demand (the input);
    # planSKU comes from the schedule output (what actually got scheduled).
    # These can differ when the LP couldn't fit all demanded SKUs.
    demand_sku = _count_demand_skus(plan_id, db_cfg)
    plan_sku   = _count_sku_rows(ws)

    row = {
        "plan_id":             plan_id,
        "demandFulfillment":   float(_require(r"Fulfillment:\s*([\d.]+)\s*%", summary)),
        "demandSKU":           demand_sku,
        "planSKU":             plan_sku,
        "capacityUtilisation": float(_require(r"Avg Util:\s*([\d.]+)\s*%", summary)),
        "curingChangeovers":   int(_require(r"Changeovers:\s*([\d,]+)", summary).replace(",", "")),
        "createdAt":           datetime.now(),
        "createdBy":           created_by,
    }

    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO jkt_plan_kpis
                   (plan_id, demandFulfillment, demandSKU, planSKU,
                    capacityUtilisation, curingChangeovers, createdAt, createdBy)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (row["plan_id"], row["demandFulfillment"], row["demandSKU"], row["planSKU"],
             row["capacityUtilisation"], row["curingChangeovers"], row["createdAt"], row["createdBy"]),
        )
        conn.commit()
        print(f"[upload:kpi] inserted 1 row into jkt_plan_kpis")
    finally:
        cur.close()
        conn.close()
