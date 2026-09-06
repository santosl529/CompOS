# FIXED

Issues previously logged in `PROGRESS.md` that are confirmed resolved and stable. Newest first.

## 2026-09-05 (session 7)

- **Pass 1 had 0 `low` because three test protocols scored unknown on every candidate** →
  `thermal_shock_cycles`, `vibration_grms`, `humidity_resistance` reclassified to `qualification`
  in `spec_requirements.csv`. They are now burden, not scored. Median scored-unknown 57% → 40%;
  risk 36/966/0 → **36 high / 841 medium / 125 low**. Thresholds not changed.

## 2026-09-05 (session 5)

- **Pass-1 scoring dominated by `qualification` (940/956 high)** → `docs/CHANGE-qualification-burden.md`.
  Risk now rolls up electrical/mechanical/environmental only. Qualification/traceability are a burden
  (`qualification_gap_count` / `_summary`, `spec_deltas.scored=false`, verdict `unknown` never `fail`).
  `low` no longer requires `qpl_listed`. Result: 36 high / 966 medium / 0 low — the remaining empty
  `low` band is the 57% median unknown ratio, not a QPL column of falses.

## 2026-09-05 (session 4)

- **`fsc_category_map.csv` category ids unverified** → verified live against the Digi-Key v4 category tree.
  8 of 9 guesses were wrong or non-leaf. Now one-or-more leaf ids per FSC; documented in `config/README.md`.
  Stable across three full 41-NSN runs.
- **S6 parametric filtering never exercised live** → it had never fired at all: the code read
  `Values[].ValueText`, the API returns `FilterValues[].ValueName`. Fixed; 873 of 956 candidates now come
  from parametric (filtered) searches, hit sets typically 8–2 400 instead of 35 000+.
- **`.env` only** → `common.load_env` reads `.env` then `.env.local` (override; empty values do not shadow).
- **Unit in a separate `uom` column dropped** (10000 pF read as 10000 F) → `value_with_uom` in S5 and S8,
  covered by `tests/test_normalize.py::TestSeparateUomColumn`.
- **`--nsn` filter never matched** (13-digit vs dashed) in S6 and S10 → both normalise for matching and keep
  the source `nsn` as the FK.
- **S6 `--dry-run` needed network** (token fetched before the category cache was checked) → cache first.
- **Stale `requirement_profiles_normalized.csv` read by S6** → S6 re-normalises when it is older than
  `requirement_profiles.csv`.
- **Overrides only applied by S4** (so a correction needed a re-extraction) → S5 applies
  `config/spec_requirements_overrides.csv` with the same function; 18 rows in effect.
