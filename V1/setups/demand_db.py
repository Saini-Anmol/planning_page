"""Load per-SKU demand rows from jkt_demand for a given plan_id."""
from __future__ import annotations

from V1.utilities.db import connect, safe_table


def load(db_cfg: dict, plan_id: str, table: str = "jkt_demand") -> list[dict]:
    """Returns rows in the same shape demand_route expects internally:
        SKUCode | SKU Description | Requirement | Order Type | Market | Delivery date
    The demand table has no Order Type column → that field is left as None.
    `table` lets the simulation pipeline read jkt_sim_demand instead.
    """
    table = safe_table(table)
    conn = connect(db_cfg)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            f"""SELECT skuCode, skuDescription, requirement, market, deliveryDate
               FROM {table}
               WHERE plan_id = %s""",
            (plan_id,),
        )
        db_rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return [
        {
            "SKUCode":         r["skuCode"],
            "SKU Description": r["skuDescription"],
            "Requirement":     r["requirement"] or 0,
            "Order Type":      None,  # not present in jkt_demand
            "Market":          r["market"],
            "Delivery date":   r["deliveryDate"],
        }
        for r in db_rows if r.get("skuCode")
    ]
