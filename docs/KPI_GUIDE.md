# JK Tyre PCR Planning — KPIs Guide

This document explains every metric (KPIs) the pipeline computes: how it's defined, the
exact formula, and one worked example. 

**Contents** </br> 
1. [demandFulfillment](#2-demandfulfillment)</br> 
2. [demandSKU vs planSKU](#3-demandsku-vs-plansku)</br>
3. [capacityUtilisation](#4-capacityutilisation)</br>
4. [curingChangeovers](#5-curingchangeovers)</br>
5. [Press efficiency](#6-press-efficiency)</br>

## 1. demandFulfillment

**What it is:** how much of total demand was met, as a **demand-weighted average
of per-SKU fulfillment, with each SKU capped at 100%.** 

**Formula:**
```
demandFulfillment = Σ_i [ min(planned_i / demand_i,) · (demand_i / total_demand) ] × 100
```

- Each SKU's fulfillment is capped at 100% so over-production on one SKU will not counted in overall demand fulfilment. 
- Each SKU is weighted by its **share of total demand**.

**Example** — two SKUs, equal demand:

| SKU | demand | planned | raw fulfilled | Fulfilled (max 100, (for SKU>100%)) |  |
|-----|--------|---------|---------------|--------|-
| A | 100 | 120 | 120% | **100%** |  
| B | 100 | 80 | 80% | 80% |  

```
demandFulfillment = (1.00·(100/200) + 0.80·(100/200)) × 100 = 90%
```
(Without the cap it would read more than 100%)

**On real data** (BTP_May plan): **99.42%**.

---

## 2. demandSKU vs planSKU

**What they are:** two counts in `jkt_plan_kpis`.
- `demandSKU` = number of **distinct SKUs requested** through input demand file. 
- `planSKU` = number of **distinct SKUs actually scheduled** by our engine. 

They differ when the LP can't fit every demanded SKU.


---
## 3. capacityUtilisation

**What it is:** how busy the press fleet is, measured against the **full
1440-minute calendar day.

**Formula (exact, as implemented):**
```
1. per-machine, per-day :  = min( busy_min(m, d) / 1440, 1.0 ) ← capped at 100%
2. DAILY (per-date)     :  daily(d) = mean over all 170 machines 
3. OVERALL              :  mean over all days of daily(d)                
```

Key points:
- Denominator is the full calendar day (**1440 min**) — a machine busy the entire
  day reads as 100%.
- The 100% **cap is applied per machine, per day** (step 1), BEFORE averaging.
- Idle machines count as 0% 

**Daily example** — one machine, busy 1,200 min on a given day:
```
u = min(1200 / 1440) = 0.833 → 83.3%
```

**Overall** — total busy minutes ÷ total available minutes (all presses × all
in-window days):
```
overall = Σ over (machine, day-in-window) of min(busy(m,d), 1440)
          ───────────────────────────────────────────────────────
                       1440 × n_days × n_machines
```

---

## 4. curingChangeovers

**What it is:** total number of mould/SKU changeovers in the schedule — each one
costs press downtime (default 300 min). Stored in
`jkt_plan_kpis.curingChangeovers`.

**Example** — summary line reads:
```
Demand: 72,712 | Planned: 72,793 | … | Changeovers: 170 | …
```
→ `curingChangeovers = 170`. Fewer changeovers = less lost time = more capacity
for production.

---

## 5. Press efficiency

**What it is:** a derating factor (default **0.94**) that accounts for a press not
running at theoretical peak — it makes each unit take **longer** to produce. It is
applied to **cycle time**, NOT to the utilization denominator.

**Formula** :
```
CycleTime_min = (raw_cure_time + load_unload_buffer) / press_efficiency
```

**Example** — raw cure 15 min, buffer 2.3 min:
```
at 100% efficiency: (15 + 2.3) / 1.00 = 17.3 min/unit
at  94% efficiency: (15 + 2.3) / 0.94 = 18.4 min/unit
```
So at 94% efficiency each unit occupies the press ~1.1 min longer. That extra time
is counted as **busy** time (the press is occupied but not at peak throughput).

**Important:** efficiency is in the **numerator** (busy time), and capacity
utilization is measured against available time (Section 7). We do **not** multiply
utilization by efficiency again — that would double-count. A press at 90%
utilization running 90% efficiency delivers `0.94 × 0.94 = 88%` of its theoretical
maximum unit output.

---


### Quick reference table

| Metric | Formula (short) | Denominator | Example result |
|---|---|---|---
|
| Market score | `8 − rank`, normalized `/6` | rank 1–7 | OE→1.00, Rep→0.00 |
| demandFulfillment | `Σ min(p/d,1)·(d/Σd)` | total demand | 99.42% |
| demandSKU / planSKU | distinct counts | — | 11 / 11 (or 11 / 9 if dropped) |
| capacityUtilisation | `busy / (1440·0.94)`, capped, fleet-mean | available time | 87.98% |
| curingChangeovers | count from summary | — | 170 |
| Press efficiency | `(cure+buffer)/0.90` | — | 17.3 → 19.2 min/unit |

### Configurable params-
| Knob | Default | Controls |
|---|---|---|
| `Priority weights` | 0.50 / 0.20 / 0.30 | Market, Requirement, Target Date.|
| `Market Priority` | OE=1 … Rep=7 | Default Market Priority |
| `Press Efficiency` | 0.94 (PCR) | Cycle-time efficiency |
