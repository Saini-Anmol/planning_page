"""
Phase A — compute ConsolidatedPriorityScore per SKU.

Inputs:  plan row from jkt_plan_params + demand rows from jkt_demand (by plan_id).
Output:  output/requirement_summary_<plan_id>.xlsx (Sheet2-style columns).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import openpyxl

from V1.setups import demand_db, plan_params
from V1.utilities import config_loader
from V1.utilities.exceptions import PipelineError


OUTPUT_COLS = ["SKUCode", "SKU Description", "Order Type", "Market",
               "Requirement", "ConsolidatedPriorityScore"]


def _resolve_weights(plan_row: dict, defaults: dict) -> tuple[float, float, float]:
    def pick(db_val, fallback):
        return float(db_val) if db_val not in (None, 0) else float(fallback)
    w_m = pick(plan_row.get("marketWeightage"),     defaults["market"])
    w_q = pick(plan_row.get("quantityWeightage"),   defaults["quantity"])
    w_d = pick(plan_row.get("targetdateWeightage"), defaults["date"])
    total = w_m + w_q + w_d
    if total == 0:
        raise PipelineError("All three weights are zero — cannot compute CPS",
                            stage="demand", status_code=422)
    return w_m / total, w_q / total, w_d / total


def _market_score(market: Any, plan_row: dict, aliases: dict, smin: int, smax: int,
                  default_scores: dict | None = None) -> int:
    """Resolve a per-SKU market score in [smin, smax]. Higher = higher priority.

    Resolution order:
      1. DB rank in plan_row[<alias>]: convert rank → score (inverts: rank 1 → smax).
      2. YAML default_scores[<market alias key>]: use as score DIRECTLY (no inversion).
      3. smin (lowest priority) as a final safety net.

    Production/API runs go through step 1 since plan_params is populated.
    Local testing with a sparse plan_params row falls through to step 2.
    """
    if not market:
        return smin
    key = str(market).strip()

    # Find the canonical alias key (case-tolerant) so we can look up both
    # the DB column name and the YAML default by the same canonical name.
    canonical = None
    for variant in (key, key.title(), key.upper(), key.lower()):
        if variant in aliases:
            canonical = variant
            break
    if not canonical:
        return smin

    col = aliases[canonical]

    # 1. DB rank (preferred — production path).
    rank = plan_row.get(col)
    if rank is not None:
        rank = max(smin, min(smax, int(rank)))
        return smax + smin - rank

    # 2. YAML default score (local-testing fallback only).
    if default_scores:
        default = default_scores.get(canonical)
        if default is not None:
            return max(smin, min(smax, int(default)))

    # 3. Last-resort fallback.
    return smin


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return None


def _compute_cps(rows: list[dict], plan_row: dict, weights, aliases, smin, smax,
                 default_scores: dict | None = None) -> None:
    plan_start = _as_date(plan_row.get("planStartDate"))
    plan_end   = _as_date(plan_row.get("planEndDate"))
    horizon = (plan_end - plan_start).days if (plan_start and plan_end) else 0

    for r in rows:
        r["_market_score"] = _market_score(r["Market"], plan_row, aliases, smin, smax, default_scores)
        td = _as_date(r["Delivery date"])
        r["_ttt"] = (td - plan_start).days if (td and plan_start) else horizon

    reqs = [float(r["Requirement"] or 0) for r in rows]
    req_min, req_max = (min(reqs), max(reqs)) if reqs else (0.0, 0.0)
    ttts = [r["_ttt"] for r in rows]
    ttt_min, ttt_max = (min(ttts), max(ttts)) if ttts else (0, 0)

    def norm(v, lo, hi):
        return 0.0 if hi == lo else (v - lo) / (hi - lo)

    w_m, w_q, w_d = weights
    for r in rows:
        n_m = norm(r["_market_score"], smin, smax)
        n_q = norm(float(r["Requirement"] or 0), req_min, req_max)
        n_d = 0.0 if ttt_max == ttt_min else (ttt_max - r["_ttt"]) / (ttt_max - ttt_min)
        r["ConsolidatedPriorityScore"] = w_m * n_m + w_q * n_q + w_d * n_d


def _write_output(rows: list[dict], path: Path, sheet_name: str) -> None:
    wb = openpyxl.Workbook()
    try:
        ws = wb.active
        ws.title = sheet_name
        ws.append(OUTPUT_COLS)
        for r in rows:
            ws.append([r.get(c) for c in OUTPUT_COLS])
        wb.save(path)
    finally:
        wb.close()                                       # release file handle


def run(cfg: dict) -> Path:
    plan_id  = cfg["plan"]["plan_id"]
    out_path = config_loader.output_dir(cfg) / cfg["demand"]["output_excel"]

    plan_row = plan_params.fetch(cfg["db"], plan_id)
    print(f"[demand] plan={plan_id}  start={plan_row.get('planStartDate')}  "
          f"end={plan_row.get('planEndDate')}  "
          f"weights(m/q/d)={plan_row.get('marketWeightage')}/"
          f"{plan_row.get('quantityWeightage')}/{plan_row.get('targetdateWeightage')}")

    weights = _resolve_weights(plan_row, cfg["demand"]["default_weights"])
    print(f"[demand] renormalized weights: "
          f"({weights[0]:.4f}, {weights[1]:.4f}, {weights[2]:.4f})")

    rows = demand_db.load(cfg["db"], plan_id)
    if not rows:
        raise PipelineError(
            f"No demand rows in jkt_demand for plan_id={plan_id!r}",
            stage="demand", status_code=404,
        )
    print(f"[demand] loaded {len(rows)} demand rows from jkt_demand")

    smin = int(cfg["demand"]["market_score_scale"]["min"])
    smax = int(cfg["demand"]["market_score_scale"]["max"])
    default_scores = cfg["demand"].get("default_market_scores")
    _compute_cps(rows, plan_row, weights, cfg["demand"]["market_aliases"], smin, smax,
                 default_scores=default_scores)

    _write_output(rows, out_path, cfg["demand"]["output_sheet"])
    print(f"[demand] wrote {out_path}")
    return out_path
