"""Bulk-insert every row of the schedule's Shift Schedule sheet into jkt_plan."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import openpyxl

from V1.utilities.db import connect
from V1.utilities.time_utils import now_ist

_BATCH = 1000

# Index of `qty` and `sku_code` in the tuple appended to `rows`.
_QTY_IDX = 7
_SKU_IDX = 1


def _round_sku_totals_up_to_even(rows: list[tuple]) -> int:
    """Plant produces tyres in even counts only. Walk the rows, group by SKU,
    and for any SKU whose total qty is odd, add +1 to the qty of that SKU's
    first slot. Returns the number of SKUs that got bumped.

    CHANGEOVER rows (qty=0) are excluded from the totals — they don't produce
    tyres. Demand is NOT changed; only the planned qty gets nudged up by 1.
    """
    sku_total: dict = {}
    sku_first: dict = {}
    for i, r in enumerate(rows):
        sku = r[_SKU_IDX]
        qty = r[_QTY_IDX] or 0
        if not sku or qty == 0:        # skip CHANGEOVER / cleaning rows
            continue
        sku_total[sku] = sku_total.get(sku, 0) + int(qty)
        sku_first.setdefault(sku, i)

    bumped = 0
    for sku, total in sku_total.items():
        if total % 2 == 1:
            i = sku_first[sku]
            row = list(rows[i])
            row[_QTY_IDX] = (row[_QTY_IDX] or 0) + 1
            rows[i] = tuple(row)
            bumped += 1
    return bumped


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
    now = now_ist()

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

    # Plant constraint: per-SKU planned qty must be EVEN. Add +1 tyre on the
    # first slot of any SKU whose total is odd.
    bumped = _round_sku_totals_up_to_even(rows)
    if bumped:
        print(f"[upload:plan] bumped +1 tyre on {bumped} SKUs to make totals even")

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
