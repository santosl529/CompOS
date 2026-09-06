# S9 rewrite — hard gates + weighted composite

Replaces the count-based risk rollup with the model in the two product design
PDFs. Do the phases in order and stop where indicated.

---

## Phase 0 — Test the hard gates first. Do not skip.

Before rewriting anything, run the hard gates alone against existing
`candidates_normalized.csv` and report survivor counts per NSN.

Hard gates by category:

**Universal (all FSCs)**
- `operating_temp_min` ≤ required AND `operating_temp_max` ≥ required —
  candidate range must fully contain the required range
- `voltage_rating` ≥ required, for every FSC where the field exists except
  5945 (relays use `coil_voltage` instead)

**5905** `resistance` within the required tolerance band; `power_rating` ≥ required
**5910** `capacitance` within tolerance band; `voltage_rating` ≥ required
**5915** `frequency_range_max` ≥ required; `current_rating` ≥ required; `voltage_rating` ≥ required
**5930** `current_rating` ≥ required; `voltage_rating` ≥ required; `contact_configuration` exact
**5935** `contact_count` exact; `shell_size` exact; `dielectric_withstanding_voltage` ≥ required; `coupling_type` exact
**5945** `contact_rating` ≥ required; `coil_voltage` exact; `contact_configuration` exact; `sealing` meets or exceeds
**5950** `inductance` within tolerance band; `current_rating` ≥ required
**5961** `voltage_rating` ≥ required; `current_rating` ≥ required
**5999** `current_rating` ≥ required; `contact_count` exact where present; `insulation_resistance` ≥ required

**Report per NSN: candidates in, survivors out, and which gate eliminated the most.**

I specifically need the number for `5935-00-079-5458` (slot `J1`). Its baseline
has 37 contacts, and 5935 requires exact match on `contact_count`, `shell_size`
AND `coupling_type` simultaneously. If that leaves zero survivors, stop and tell
me — the gates need loosening before the composite is worth building.

**A missing value is NOT a gate failure.** If the candidate has no value for a
gated attribute, the gate is `unverified` — it passes to the composite stage and
the attribute is excluded from scoring. Only an actual violation fails the gate.
Treating missing as failure would eliminate nearly everything.

---

## Phase 1 — Rewrite scoring

### Gates run first

Any hard gate violation → `risk_level = high`, `composite_score = 100`, skip the
composite entirely. Record which gate(s) failed in `rationale`.

### Composite for survivors

Weight pools:

```
Universal soft      60    lead_time 38, unit_cost_vs_historical 22
Category soft       40    per table below
```

| FSC | Attribute | Weight |
|---|---|---|
| 5905 | resistance_tolerance | 23 |
| 5905 | temperature_coefficient | 17 |
| 5910 | capacitance_tolerance | 13 |
| 5910 | dissipation_factor | 13 |
| 5910 | dielectric_type | 14 |
| 5915 | insertion_loss | 40 |
| 5930 | mechanical_life | 23 |
| 5930 | actuation_pressure | 17 |
| 5935 | contact_resistance | 13 |
| 5935 | insulation_resistance | 10 |
| 5935 | contact_plating | 10 |
| 5935 | shell_material | 7 |
| 5945 | coil_resistance | 40 |
| 5950 | inductance_tolerance | 13 |
| 5950 | dc_resistance | 13 |
| 5950 | q_factor | 14 |
| 5961 | *(none)* | — |
| 5999 | contact_plating | 20 |
| 5999 | shell_material | 20 |

### Formulas

**Type A — lower is better** (`dissipation_factor`, `insertion_loss`,
`contact_resistance`, `dc_resistance`, `temperature_coefficient`,
`coil_resistance`):
```
worst_case = original × 1.20
risk = clamp(((value - original) / (worst_case - original)) × 100, 0, 100)
```

**Type B — higher is better** (`mechanical_life`, `q_factor`,
`insulation_resistance`):
```
worst_case = original × 0.80
risk = clamp(((original - value) / (original - worst_case)) × 100, 0, 100)
```

**Type C — closer to original is better** (`actuation_pressure`,
`capacitance_tolerance`, `resistance_tolerance`, `inductance_tolerance`):
```
pct_deviation = |value - original| / original
risk = clamp(pct_deviation × 500, 0, 100)
```

**Type D — categorical lookup**
```
contact_plating   gold 0 | silver 20 | tin 45 | unknown 80
shell_material    composite 10 | stainless steel 20 | aluminum 30 | unknown 75
dielectric_type   C0G/NP0 0 | X7R 30 | Z5U/Y5V 70 | unknown 85
```
Match case-insensitively and by containment — `gold over nickel` scores as gold,
`aluminum alloy` as aluminum.

**Universal**
```
lead_time   risk = clamp(lead_time_days / 180 × 100, 0, 100)
unit_cost   pct_change = (candidate_cost - historical_cost) / historical_cost
            risk = clamp(50 + pct_change × 100, 0, 100)
```

**Guard against division by zero.** Types A, B and C all divide by a term derived
from `original_value`. If `original_value` is 0 or missing, the attribute is
unavailable — exclude it and renormalize. Never emit inf or NaN.

### Renormalization — the important part

An attribute is unavailable when the candidate has no value, the original has no
value, or the formula would divide by zero.

```
available_weight = sum of weights for attributes with data
composite = Σ(weight_i × risk_i) / available_weight
weight_coverage_pct = available_weight / 100 × 100
```

Concretely: an unenriched candidate has no `lead_time_days` and no
`gov_unit_price`, so the entire 60-point universal pool drops out. A 5935
candidate then scores over its 40-point category pool alone, renormalized to
100, with `weight_coverage_pct = 40`.

**5961 has no category attributes.** Its composite is the universal pool alone,
renormalized. If a 5961 candidate is also unenriched, `available_weight` is 0 —
emit `composite_score` null, `weight_coverage_pct` 0, `risk_level = medium`, and
say so in `rationale`. Do not emit 0, which would read as low risk.

### Bands

```
0-33    low
34-66   medium
67-100  high
```

`risk_rank` stays 1/2/3. Sort remains `risk_rank` then composite ascending.

---

## Decisions already made — implement as stated

- **Drop the CAGE-code hard gate.** Digi-Key and Nexar return no CAGE, so it
  would fail 100% of candidates.
- **Country of origin gate:** stub only. Add a `country_of_origin` column, all
  `unknown` for now, and do not gate on it. AIP will populate it in Foundry
  later. Missing must never fail the gate.
- **Qualification burden is unchanged.** It stays out of the composite entirely.
  `qualification_gap_count` and `qualification_gap_summary` keep their current
  behaviour, and the three reclassified environmental clauses stay in burden.
- **`spec_deltas` keeps the `scored` boolean.** Add a `gate_type` column:
  `hard`, `soft`, or `burden`.

---

## New columns on `substitution_candidates`

```
composite_score        float 0-100, null when available_weight is 0
weight_coverage_pct    float 0-100
gates_failed           text, pipe-delimited names of failed hard gates, empty if none
country_of_origin      text, 'unknown' for now
```

Keep `risk_level`, `risk_rank`, `qualification_gap_count`,
`qualification_gap_summary`, `source`, `rank_within_nsn`, `enriched`.

Drop `pass_count` / `fail_count` / `marginal_count` / `unknown_count` from the
contract only if nothing else reads them — check first and tell me.

---

## Phase 2 — Run and report

Re-run S8 → S9 pass 1. Report:

- survivors per hard gate, and per NSN
- `risk_level` distribution (was 36 high / 841 medium / 125 low)
- `composite_score` distribution — min, median, max
- `weight_coverage_pct` distribution — I expect a cluster at 40 for unenriched
- any candidate with null composite, and why
- row counts at every boundary

**Stop here.** Do not run Octopart yet.

---

## Phase 3 — Octopart enrichment (only after I confirm Phase 2)

Budget: **90 matched parts, hard ceiling, enforced by a counter that raises.**

First run `--verify-counting <MPN>` twice on the same MPN and report whether the
meter moves once or twice.

Selection: top 3 by `rank_within_nsn` for 30 NSNs, demo-path NSNs first.
**Dedupe the MPN list before querying** — the same MPN appears against multiple
NSNs and a distinct MPN costs one credit regardless. Report how many NSNs the 90
credits actually covered after dedup.

Emit `commercial_parts.csv` per the SCHEMA contract. Set `enriched = true` on
those candidates.

Then re-run S8 → S9. Enriched candidates now have `lead_time_days`, so their
`weight_coverage_pct` rises from 40 to 78 (universal lead-time component plus
category pool). `unit_cost` stays unavailable — `gov_unit_price` is null for all
41 NSNs from USAspending.

Report the same distributions again, plus `lifecycle_status` breakdown — how
many obsolete or NRND.
