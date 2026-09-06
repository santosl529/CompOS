# Change: qualification becomes a burden, not a verdict

## Why

`qpl_listed` is `false` for 956 of 956 candidates, and it will stay that way
with real QPL data too. Commercial parts are not on military qualified
products lists — that is not a data gap, it is the structural fact the tool
exists to quantify.

Scoring candidates against a QPL clause therefore produces a column that reads
`false` every time and a risk distribution of 940 high / 16 medium / 0 low.
Technically correct, informationally empty.

The bar belongs on the **baseline** side of the comparison, not the candidate
side. The baseline part is QPL-listed under its governing spec; that tells you
what testing the replacement would have to pass. The useful output is not
"this candidate fails qualification" — it is "here is the qualification work
this substitution would require."

## What changes

### 1. Risk scoring excludes qualification and traceability classes

`S9` currently rolls up all requirement classes into pass/fail/marginal/unknown
and treats a `qualification`-class failure as automatic `high`.

Change: risk is computed over **`electrical`, `mechanical`, and `environmental`
requirements only.** Requirements of class `qualification` and `traceability`
are excluded from `pass_count`, `fail_count`, `marginal_count`,
`unknown_count`, and from the `risk_level` rollup entirely.

Revised rollup (unchanged except for the scope it operates on, and the removal
of the QPL condition on `low`):

```
high    -- more than 2 scored requirements fail
medium  -- 1-2 scored requirements fail
           OR more than 30% unknown
           OR more than 30% marginal
low     -- all scored requirements pass or marginal,
           AND unknown <= 30%, AND marginal <= 30%
```

`low` no longer requires `qpl_listed`. A candidate that meets every measurable
requirement is low **technical** risk; the qualification burden is reported
separately and is never zero.

### 2. New columns on `substitution_candidates.csv`

```
qualification_gap_count   int   number of qualification/traceability
                                clauses the baseline's governing spec
                                imposes that cannot be verified from
                                commercial data
qualification_gap_summary text  pipe-delimited clause names, e.g.
                                lot_traceability|thermal_shock_cycles|
                                solderability_per_j_std_002
```

Both are derived from the federal item's `requirement_profiles` rows where
`requirement_class` is `qualification` or `traceability`. They describe the
**baseline's** spec, so they are identical across all candidates for a given
NSN. That is correct and expected — the gap is a property of the substitution,
not of the individual part.

`qpl_listed` stays as a column. It is still meaningful on the baseline side
and costs nothing to keep. It no longer gates `risk_level`.

### 3. `spec_deltas.csv` keeps qualification rows, verdict `unknown`

Do not drop them. They are what populates the gap panel in the UI.

Their `verdict` must be `unknown`, never `fail`. "Not verifiable from
commercial data" is not the same claim as "does not meet." Writing `fail`
asserts something we have not established.

Add a column so the UI can separate the two groups cleanly:

```
scored   bool   true for electrical/mechanical/environmental,
                false for qualification/traceability
```

### 4. Rationale text

`rationale` on `substitution_candidates` should now read along the lines of:

> Meets 9 of 11 measurable requirements; fails operating temperature.
> 4 qualification clauses under MIL-DTL-38999 require testing to verify.

Not "fails qualification."

## What this does NOT change

- `SCHEMA.txt` §10 risk thresholds, other than the scope they apply over and
  the removal of the QPL condition on `low`
- The `unknown` never becomes `pass` rule — still absolute
- `risk_rank` (1/2/3) and the sort order
- Any other file's contract columns
- The QPL pair-join logic itself (part number AND cage/manufacturer). It stays
  for the baseline side.

## Do NOT run the fix script

`fix_risk_variance.py` seeded real Digi-Key MPNs into `qpl.csv` to manufacture
variance. That was the wrong fix — it fakes qualification status that does not
exist in reality. Discard it. This change achieves the same variance honestly.

## Re-run scope

Only S9 depends on this. Nothing upstream changes.

```
S9 pass 1     (rewrites substitution_candidates.csv and spec_deltas.csv)
```

S5, S6, S8, S10, S11 outputs are unaffected and must not be regenerated.

## Expected result

Report after the re-run:

- `risk_level` distribution — expect a genuine spread now. If it is still
  overwhelmingly high, the cause is technical requirement failures or the
  unknown ratio, which is a real finding worth reporting rather than a bug.
- median unknown ratio over **scored** requirements only
- distribution of `qualification_gap_count` (expect 2-6 per NSN)
- a sample rationale string

## One thing to sanity-check

The two 5905 NSNs whose profiles contain no `resistance` requirement
(`5905-00-493-1204`, `5905-00-493-1210`) will still produce loose candidate
sets. That is a gap in the synthetic source data, not a scoring problem.
Either add a resistance requirement for them via
`config/spec_requirements_overrides.csv`, or leave them out of the demo path.
Flag which you did.
