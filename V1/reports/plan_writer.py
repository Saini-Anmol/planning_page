"""Bulk-insert every row of the schedule's Shift Schedule sheet into jkt_plan."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import openpyxl

from V1.utilities.db import connect

_BATCH = 1000


def _load_sku_descriptions(plan_id: str, db_cfg: dict) -> dict[str, str]:
    """Build a SKUCode → description lookup from jkt_demand.

    The v4 scheduler dropped SKU_Description from the Shift Schedule sheet,
    so we enrich from the demand table (which has it). CHANGEOVER rows and
    any SKU not in jkt_demand simply get None.
    """
    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT skuCode, MAX(skuDescription) FROM jkt_demand "
            "WHERE plan_id = %s GROUP BY skuCode",
            (plan_id,),
        )
        return {sku: desc for sku, desc in cur.fetchall() if sku}
    finally:
        cur.close()
        conn.close()


def upload(schedule_path: Path, plan_id: str, created_by: str, db_cfg: dict) -> None:
    sku_desc_lookup = _load_sku_descriptions(plan_id, db_cfg)

    wb = openpyxl.load_workbook(schedule_path, data_only=True)
    ws = wb["Shift Schedule"]
    now = datetime.now()

    rows = []
    # Shift Schedule columns (v4): Date, Shift, Machine, SKUCode, StartTime,
    # EndTime, Qty, CycleTime_min, GT_Inventory, Remarks. Description is gone.
    for r in range(4, ws.max_row + 1):
        date_v, shift_v, _machine, sku_code, start_t, end_t, qty, cycle, _gt, remarks = (
            ws.cell(row=r, column=c).value for c in range(1, 11)
        )
        if all(v is None for v in (date_v, shift_v, sku_code, start_t, end_t, qty)):
            continue
        rows.append((
            plan_id,
            sku_code,
            sku_desc_lookup.get(sku_code),   # populated from jkt_demand
            date_v.date() if hasattr(date_v, "date") else date_v,
            shift_v,
            start_t,
            end_t,
            int(qty) if qty is not None else None,
            float(cycle) if cycle is not None else None,
            remarks,
            now,
            created_by,
        ))

    conn = connect(db_cfg)
    try:
        cur = conn.cursor()
        total = 0
        for i in range(0, len(rows), _BATCH):
            cur.executemany(
                """INSERT INTO jkt_plan
                       (plan_id, skuCode, skuDescription, date, shift,
                        startTime, endTime, qty, cycleTime, remarks, createdAt, createdBy)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                rows[i:i + _BATCH],
            )
            total += cur.rowcount
        conn.commit()
        print(f"[upload:plan] inserted {total} rows into jkt_plan")
    finally:
        cur.close()
        conn.close()
