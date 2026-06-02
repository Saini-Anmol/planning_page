# KPI & Metrics Reference

Every metric the pipeline computes, with the **exact formula** and a **worked
numerical example** for each. All values land in the `jkplanningV1.*` tables —
the formulas below are what `V1/reports/*_writer.py` actually implements.

**Contents**

1. [ConsolidatedPriorityScore (CPS)](#1-consolidatedpriorityscore-cps)
2. [demandFulfillment](#2-demandfulfillment)
3. [demandSKU vs planSKU](#3-demandsku-vs-plansku)
4. [capacityUtilisation (daily + overall)](#4-capacityutilisation)
5. [curingChangeovers](#5-curingchangeovers)
6. [Press efficiency](#6-press-efficiency)
7. [Even-tyre rule](#7-even-tyre-rule)
8. [How everything fits together](#8-how-it-all-fits-together)

---

## 1. ConsolidatedPriorityScore (CPS)

A single score in `[0, 1]` per SKU that decides scheduling priority. Higher CPS → scheduled earlier / more completely. Computed in [V1/routes/demand_route.py](../V1/routes/demand_route.py).

**Formula:**
```
CPS(s) = w_market · norm_market(s)
       + w_qty    · norm_req(s)
       + w_date   · norm_date(s)
```

Weights `(w_market, w_qty, w_date)` come from `jkt_plan_params` (or YAML defaults `0.50 / 0.20 / 0.30`), renormalized to sum to 1.

### 1a. Market score (`norm_market`)

DB stores **rank** per market (`oe`, `re`, `st`, etc.) where rank 1 = highest priority. The code inverts rank → score, then normalizes:

```
score       = (smax + smin) − rank          # smin=1, smax=7  →  8 − rank
norm_market = (score − smin) / (smax − smin) = (score − 1) / 6
```

**Example** — OE SKU, DB `oe = 1`:
```
score       = 8 − 1 = 7
norm_market = (7 − 1) / 6 = 1.00          ← highest possible
```

Replacement SKU with DB `re = 7`:
```
score       = 8 − 7 = 1
norm_market = (1 − 1) / 6 = 0.00          ← lowest
```

### 1b. Quantity score (`norm_req`)

Min-max normalize each SKU's `requirement` across the plan:
```
norm_req(s) = (req(s) − req_min) / (req_max − req_min)
```

**Example** — req = 11,846, plan min = 1,802, max = 55,000:
```
norm_req = (11846 − 1802) / (55000 − 1802) = 10044 / 53198 = 0.189
```

### 1c. Date-urgency score (`norm_date`)

Sooner target date = more urgent = higher score:
```
ttt(s)       = (delivery_date − plan_start).days
norm_date(s) = (ttt_max − ttt(s)) / (ttt_max − ttt_min)
```

Missing delivery date → falls back to plan end (least urgent). If **all** SKUs lack dates, `norm_date = 0` for every SKU and the date factor drops out.

### 1d. Putting it together

**Example** — OE SKU, weights `(0.50, 0.20, 0.30)`, no delivery dates:
```
norm_market = 1.00   (OE, rank 1)
norm_req    = 0.189
norm_date   = 0.00   (no dates → factor inactive)

CPS = 0.50·1.00 + 0.20·0.189 + 0.30·0 = 0.538
```

### Aggregation in Phase B

If one SKU appears in multiple market rows in `jkt_demand`, Phase A keeps both
rows (each gets its own CPS). The LP scheduler (Phase B) then aggregates:
**`Quantity = SUM`, `Priority = MAX`**.

---

## 2. demandFulfillment

**What:** demand-weighted average of per-SKU fulfillment, with each SKU capped at 100%. Written to `jkt_plan_kpis.demandFulfillment`.

**Formula:**
```
demandFulfillment = Σ_i [ min(planned_i / demand_i, 1.0) · (demand_i / Σ demand) ] × 100
```

Two rules baked in:
- **Per-SKU cap at 100%** — over-production on one SKU can't mask shortfalls on another.
- **`planned` rounded UP to next even** — matches the even-tyre rule in `plan_writer` (see §7).

The `'TOTAL'` summary row at the bottom of the Demand Fulfillment sheet is **excluded** from this calc (otherwise it double-counts).

**Example** — two SKUs, equal demand:

| SKU | demand | planned | raw fulfilled | capped | weight |
|---|---|---|---|---|---|
| A | 100 | 120 | 120% | **100%** | 0.5 |
| B | 100 | 80 | 80% | 80% | 0.5 |

```
demandFulfillment = (1.00·0.5 + 0.80·0.5) × 100 = 90%
```
Without the cap it would read 100% — hiding the fact that B is short.

---

## 3. demandSKU vs planSKU

| KPI | What it counts | Source |
|---|---|---|
| `demandSKU` | distinct SKUs **requested** | `SELECT COUNT(DISTINCT skuCode) FROM jkt_demand WHERE plan_id = ?` |
| `planSKU` | distinct SKUs that **actually got production** | Demand Fulfillment sheet rows where `Planned_Units > 0` |

`planSKU` **excludes**:
- the `'TOTAL'` summary row
- SKUs with status `UNMET` or `UNSCHEDULABLE` (their Planned_Units = 0)

So `planSKU ≤ demandSKU` always.

**Example** — `jkt_demand` has 15 rows, 11 distinct SKUs. LP fully meets 7, partially meets 3, marks 1 as UNMET. → `demandSKU = 11`, `planSKU = 10` (7 fully + 3 partial).

---

## 4. capacityUtilisation

**What:** how busy the press fleet is, measured **per machine** against the full 1440-minute calendar day. Changeover and mould-clean time are **excluded** — only productive minutes count.

Written to:
- `jkt_plan_capacityUtilisation` — one row per planning day
- `jkt_plan_kpis.capacityUtilisation` — overall (mean of daily values)

Both come from the same `compute_daily_utilisation()` function in [V1/reports/capacity_writer.py](../V1/reports/capacity_writer.py), so the overall KPI is provably the mean of the daily numbers.

### Formula

```
1. per-machine, per-day :  u(m,d) = min( productive_min(m,d) / 1440, 1.0 )    ← capped at 100%
2. DAILY (per-date)     :  daily(d) = mean over 170 machines of u(m,d)         → jkt_plan_capacityUtilisation
3. OVERALL              :  mean over all days of daily(d)                      → jkt_plan_kpis.capacityUtilisation
```

`productive_min(m,d)` = sum of `(EndTime - StartTime)` across this machine's slots on date `d`, excluding rows where `SKUCode == 'CHANGEOVER'` or `Remarks` contains `CLEAN`. Slots crossing midnight are split so each date gets only its own minutes.

### Why "productive only"

| Type of slot | Counted as busy? |
|---|---|
| Producing tyres | ✅ Yes |
| CHANGEOVER (SKU switch, ~300 min) | ❌ No |
| Mould cleaning (~120 min) | ❌ No |
| Idle | ❌ No |

This matches the **Productive Utilization** definition (not OEE Availability). The press IS physically occupied during a changeover, but it's not producing — so it doesn't count.

### Day-1 example (plan starts 07:00)

Plan begins at 07:00 June 1. June 1's calendar day is 00:00–23:59.

```
00:00 ────────── 07:00 ────────────────────── 23:59
└── no production ──┘└──── ~17 hrs production ─┘
   (plan not yet started)                       + 1 hr from shift C
```

Total productive minutes per machine ≈ 1020. Utilization = 1020/1440 ≈ **70.83%**. Day 2 (full 00:00 → 23:59 inside the plan window) can reach ~100%.

### Overall example

```
overall = (1/n_days) × Σ_d daily(d)
```

For BTP_June_Plan_V1_124766 on June 2026: overall = **87.75%** = mean of 30 daily values (range 70.7% – 99.0%).

> **Productive util ≠ OEE Availability.** If you ever need "press-occupancy %" (including CO + clean), that's a different metric — would require a new column.

---

## 5. curingChangeovers

**What:** total number of mould/SKU changeovers in the schedule. Each one costs ~300 min of press downtime. Written to `jkt_plan_kpis.curingChangeovers`.

**Formula:**
```
curingChangeovers = the "Changeovers: N" value from the schedule's
                    Demand Fulfillment summary line
```

**Example** — summary line reads:
```
Demand: 72,712 | Planned: 72,793 | … | Changeovers: 170 | …
```
→ `curingChangeovers = 170`.

### Does the per-shift cap reduce this number?

**No — only `CHANGEOVER_PENALTY_WEIGHT` reduces the count.** `noOfChangeOver` (the per-shift cap) only spreads changeovers across shifts. The LP picks the *count* based on the demand structure + penalty weight; the cap just decides *when* each changeover lands.

For a typical PCR month: ~169 changeovers across 93 shifts = ~1.82 avg/shift. So caps ≥ 2 never bind for this load. Only `noOfChangeOver = 1` would force fewer total changeovers (by pushing some out of the plan window → fulfillment drops).

---

## 6. Press efficiency

**The single efficiency factor in the pipeline.** Default `0.94`. Configurable per plan via `jkt_plan_params.efficiency` (stored as percentage; converted to fraction at runtime).

**Formula** (in `ETL.load_cycle_times`):
```
CycleTime_min = (raw_cure_time + load_unload_buffer) / press_efficiency
```

**Example** — raw cure 15 min, buffer 2.3 min:
```
at 100% efficiency: (15 + 2.3) / 1.00 = 17.3 min/unit
at  94% efficiency: (15 + 2.3) / 0.94 = 18.4 min/unit
```

So at 94% efficiency each tyre occupies the press ~1.1 min longer. That extra time is counted as **productive busy** time (the press is occupied producing, just slower).

### Lower efficiency → HIGHER utilization

Because efficiency lives in the **numerator** (it inflates busy minutes via cycle time), a *lower* efficiency makes the same production output consume *more* press-time → *higher* utilization.

**Example** — a press makes **50 tyres** in one day:

| Efficiency | Cycle time/tyre | Busy minutes | Utilization (÷ 1440) |
|---|---|---|---|
| 94% | 18.4 | 920 | **63.9%** |
| 90% | 19.2 | 961 | **66.7%** |

Same 50 tyres, but at 90% efficiency the press is "busier" → higher util. This does NOT mean more output — same output, more press-time consumed.

### Where it does NOT go

Press efficiency only affects `CycleTime_min`. It is **NOT** applied as a denominator factor to utilization (that would double-count). The utilization denominator is always the full 1440 min/day.

---

## 7. Even-tyre rule

The plant produces tyres in **even counts only**. Both writers enforce this:

- `plan_writer` rounds each SKU's total qty UP to the next even number by adding +1 tyre to the SKU's first scheduled slot if total is odd.
- `kpi_writer.demandFulfillment` reads each SKU's `Planned_Units` from the sheet and rounds UP to the next even before the weighted-average math.

Both writers use the **same** helper: `_round_up_to_even(n) = n + (n % 2)`. Defined in [V1/reports/kpi_writer.py](../V1/reports/kpi_writer.py); imported by `plan_writer`. By construction, they cannot drift.

**Example** — SKU "ABC" with 3 slots:

| Slot | qty (pre-rule) | qty (post-rule) |
|---|---|---|
| 1 | 50 | **51** ← +1 because total was odd |
| 2 | 50 | 50 |
| 3 | 1101 | 1101 |
| **Total** | **1201** (odd ❌) | **1202** (even ✅) |

---

## 8. How it all fits together

```
              ┌─────────── Phase A (demand_route) ───────────┐
jkt_demand    │ per-SKU CPS = w_m·norm_market                 │
jkt_plan_params│              + w_q·norm_req                  │
              │              + w_d·norm_date                  │
              └────────────────────┬─────────────────────────┘
                                   ▼  output/requirement_summary_<plan>.xlsx
              ┌─────────── Phase B (schedule_route) ──────────┐
6 master tables│ DB overrides applied: PRESS_EFFICIENCY,       │
              │   MAX_CHANGEOVERS_PER_SHIFT, PLAN_DATE, etc.  │
              │ LP solve (scipy.linprog)                       │
              │ aggregate dup SKUs (sum qty, max priority)    │
              └────────────────────┬─────────────────────────┘
                                   ▼  output/PCR_Schedule_<plan>_*.xlsx  (5 sheets)
              ┌─────────── Phase C (upload_route) ────────────┐
              │ plan_status.assert_not_already_scheduled()    │ → 409 if duplicate
              │ kpi_writer       → jkt_plan_kpis              │
              │   demandFulfillment (demand-wt, cap @100%, even rounded) │
              │   demandSKU / planSKU                         │
              │   capacityUtilisation (productive only, /1440)│
              │   curingChangeovers                           │
              │ plan_writer      → jkt_plan                   │
              │   (per-SKU totals rounded UP to even tyres)   │
              │ capacity_writer  → jkt_plan_capacityUtilisation│
              │   (per-date, productive only, /1440)          │
              └─────────────────────────────────────────────┘
```

### Quick reference

| KPI | Source | Formula | Example value |
|---|---|---|---|
| CPS | computed | `w_m·nm + w_q·nq + w_d·nd` | 0.538 (an OE SKU) |
| `demandFulfillment` | sheet rows (excl TOTAL) | `Σ min(p/d,1)·(d/Σd) × 100`, even-rounded | 96.79% |
| `demandSKU` | `jkt_demand` table | `COUNT(DISTINCT skuCode)` | 120 |
| `planSKU` | sheet rows | rows with Planned_Units > 0, excl TOTAL + UNMET + UNSCHEDULABLE | 104 |
| `capacityUtilisation` | sheet, productive only | mean of (busy_prod / 1440), capped, fleet-mean per date | 87.75% |
| `curingChangeovers` | sheet summary | parsed count | 169 |

### Configurable knobs (`config/config.yaml`)

| Knob | Default | Controls |
|---|---|---|
| `demand.default_weights` | 0.50 / 0.20 / 0.30 | CPS weights when DB row is NULL |
| `demand.default_market_scores` | OE=7, Rep=1, … | Per-market scores when DB ranks NULL |
| `demand.market_score_scale` | min 1, max 7 | Score range used in normalization |
| `schedule.press_efficiency` | 0.94 | Cycle-time inflation factor |
| `schedule.max_changeovers_per_shift` | 5 | Per-shift cap when DB `noOfChangeOver` is NULL |
| `upload.created_by` | "Algo8 AI" | String for `createdBy` columns |

### Per-plan DB overrides (`jkt_plan_params`)

| DB column | Maps to | Notes |
|---|---|---|
| `efficiency` | `Config.PRESS_EFFICIENCY` | DB stores as % (94); divided by 100 at runtime |
| `noOfChangeOver` | `Config.MAX_CHANGEOVERS_PER_SHIFT` | Direct mapping — value is per-SHIFT |
| `marketWeightage` / `quantityWeightage` / `targetdateWeightage` | CPS weights | Renormalized to sum 1 |
| `oe / re / st / defence / export / otr / government` | CPS market ranks | NULL → YAML default scores apply |
