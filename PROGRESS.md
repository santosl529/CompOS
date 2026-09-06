# PROGRESS

Running log for the pre-Foundry pipeline scripts. Source of truth for scope is `docs/PRD.md`;
conventions are in `docs/CLAUDE.md`. Newest entries first within each section.

## Status at a glance (2026-09-05)

| Step | Script | State | Verified how |
|---|---|---|---|
| — | `scripts/common.py` | done | used by every script |
| S1 | `s01_publog_ingest.py` | done, **untested on real PUB LOG** | synthetic CSV fixture with guessed column names |
| S2 | `s02_qpl_ingest.py` | done, **untested on a real .mdb** (mdbtools not installed here) | code review only; `--inspect` mode exists |
| S3 | `s03_assist_fetch.py` | done | live download of a public test PDF, HTML-as-PDF rejected, 404 handled, manual drop indexed |
| S4 | `s04_spec_extract.py` | done, **no live LLM call made yet** (no key) | pure functions probed; dry-run + no-text PDF path run end to end |
| S5 | `s05_build_requirement_profile.py` | done, **run on synthetic S1/S2/S4 data** | 548 profiles / 41 items; also applies `spec_requirements_overrides.csv` (18 rows) |
| S6 | `s06_digikey_search.py` | done, **live-verified** (all 41 NSNs, 2026-09-05) | 956 candidates; also `part_images.csv` (603 MPN→PhotoUrl); 181 cached responses |
| S7 | `s07_nexar_enrich.py` | done, **live** (Bearer token; 62/100 local) | verify-counting 1N4002 ×2; 60 MPNs / 30 NSNs; `commercial_parts.csv` 60 |
| S8 | `s08_normalize.py` | done | 34 unit tests; 548 profiles + 14 748 candidate attrs; 60 commercial parts |
| S9 | `s09_score.py` | done, **pass 2 after Nexar** | 1 002 candidates; **197 high / 151 medium / 307 low / 347 unscored** |
| S10 | `s10_usaspending_prices.py` | done | **live** for all 41 NSNs; 0 have contract history (synthetic NSNs); endpoint verified with a real keyword |
| S11 | `s11_assembly_gen.py` | done | fixture run, validated + fake-data modes |

Tests: `.venv/bin/python -m unittest discover -s tests` — 88+ pass (`score.py`, `normalize.py`, plus S6 image helpers).

**Contract precedence used by the code:** `docs/SCHEMA.txt` (shared pipeline↔Foundry contract, added
2026-09-05) governs column sets, enums and semantics where it and `docs/PRD.md` disagree. Every such
disagreement is listed under "Docs that need updating" below — PRD.md still describes the older contract in
several places and I have not edited it.

### Commands

```
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/s01_publog_ingest.py [--print-headers] [--publog-dir DIR]
.venv/bin/python scripts/s11_assembly_gen.py                # re-run after S5 for validated interfaces
.venv/bin/python scripts/s06_digikey_search.py [--nsn N --keywords "..." --category ID] [--dry-run] [--list-categories --grep TEXT]
.venv/bin/python scripts/s08_normalize.py
.venv/bin/python scripts/s09_score.py                       # first pass (ranking for S7)
.venv/bin/python scripts/s02_qpl_ingest.py [--inspect]
.venv/bin/python scripts/s05_build_requirement_profile.py [--keep-unaliased]
.venv/bin/python scripts/s03_assist_fetch.py [--spec ID] [--force]
.venv/bin/python scripts/s04_spec_extract.py [--spec ID] [--dry-run] [--max-pages N]
.venv/bin/python scripts/s10_usaspending_prices.py [--nsn N]
.venv/bin/python scripts/s07_nexar_enrich.py --verify-counting MPN   # ONCE, before the first real run
.venv/bin/python scripts/s07_nexar_enrich.py [--top 3] [--nsn N] [--dry-run]
.venv/bin/python scripts/s08_normalize.py && .venv/bin/python scripts/s09_score.py   # second pass (final)
```

Full order per PRD build order: S1 → S11 → S6 → S8 → S9 → S5 (+S2) → S3 → S4 → S5 again → S8 → S9 → S10 → S7 → S8 → S9.

### Foundry upload (SCHEMA §11)

Contract columns match SCHEMA.txt exactly (file prefix). Extras after the contract set are safe to
ignore in Pipeline Builder. PKs unique; FKs resolve except the commercial-parts link.

| File | Rows | PK | Notes |
|---|---|---|---|
| `federal_items.csv` | 41 | nsn | |
| `mcrl.csv` | 145 | — | SCHEMA §7 says pipeline-only; §11 lists it |
| `characteristics.csv` | 317 | — | same |
| `qpl.csv` | 82 | qpl_id | unlinked reference |
| `specifications.csv` | 16 | spec_id | |
| `spec_requirements.csv` | 177 | spec_requirement_id | |
| `requirement_profiles.csv` | 548 | requirement_id | S5; values already in contract form |
| `price_history.csv` | 41 | price_id | all unit prices null (synthetic NSNs) |
| `assemblies.csv` | 5 | assembly_id | synthetic |
| `slots.csv` | 41 | slot_id | |
| `interfaces.csv` | 6 | interface_id | |
| `substitution_candidates.csv` | 1 002 | candidate_id | extras include gap + composite + `gates_failed` + `country_of_origin` |
| `spec_deltas.csv` | 13 676 | delta_id | extras include `scored` + `gate_type` |
| `commercial_parts.csv` | 60 | mpn | lifecycle all `unknown` (`NEXAR_PRO_FIELDS=0`) |

Do not upload `candidates.csv`, `candidates_external.csv`, `candidates_normalized.csv`,
`requirement_profiles_normalized.csv`, `commercial_parts_normalized.csv`, or `part_images.csv`
unless the UI needs thumbnails — `part_images.csv` is pipeline output, not in SCHEMA §11.
`configurations` / `substitution_decisions` are created empty in Foundry.

## Session log

### 2026-09-05 (session 11) — federal item images CSV

`--federal-images` wrote `data/federal_item_images.csv` (41 rows: nsn, image_url,
plus source / source_mpn extras). 41/41 have a Digi-Key PhotoUrl: 35
`candidate_standin`, 6 `mcrl_pn`. `federal_items.csv` left unchanged (SCHEMA has
no `image_url`). Not a Foundry §11 file — upload only if the UI needs item
thumbnails.

### 2026-09-05 (session 10) — Phase 3 Nexar (Bearer) + S8/S9 pass 2

S7 now accepts `NEXAR_ACCESS_TOKEN` and sends `Authorization: Bearer` on GraphQL,
skipping `identity.nexar.com` (that path was `invalid_client`).

`--verify-counting 1N4002` once (script hits the API twice): both matched 1 part
(hits=46). Local counter **+2**. **Check the portal meter:** +1 = distinct-part
(re-queries free); +2 = per-query (cache is load-bearing).

Live enrich: `--top 3 --max-nsns 30 --demo-path --skip-zero-survivors`. 88
candidate rows → **60 distinct MPNs**, all matched. 59 live GraphQL + 1 raw-cache
hit (`1N4002` from verify). Local lifetime **62 / 100**. Portal should be ~60
(distinct) or ~61 (per-query, 59 new after verify). Do not raise the ceiling.

`commercial_parts.csv`: 60 rows. `lifecycle_status` all `unknown` (pro fields
off). 16 single-distributor; 12 with `distributor_count=0`. Lead time empty on 7;
median price empty on 3.

S8 → S9 pass 2: **197 high / 151 medium / 307 low / 347 unscored**. Hard-gate
fails still 188. Composite null 347, min 0, median 29.2, max 100.
`weight_coverage_pct` now has lead-time bands (38–78); 78×4. `enriched=true` on
100 candidate rows (60 MPNs, some shared across NSNs). 30 NSNs have at least one
enriched row; after re-rank, 21 still have an enriched top-3.
Enriched risk: 15 high / 57 medium / 22 low / 6 unscored. No obsolete / NRND
visible — lifecycle stays `unknown` until `NEXAR_PRO_FIELDS=1`.

Federal-item images were not fetched.

### 2026-09-05 (session 9) — 5915/5961 headroom, unscored, Phase 3 blocked

5915 category pool is now `insertion_loss` 20 + `current_rating` 20. 5961 is
`voltage_rating` 20 + `current_rating` 20 (Type B headroom). 5999 / 5935 backshells
unchanged. Null composite is `risk_level=unscored`, `risk_rank=4` (SCHEMA still says
low|medium|high — flag for a contract pass).

S8 → S9 pass 1: **197 high / 100 medium / 322 low / 383 unscored**. Null composite
383 (was 433; the 50 FL1+CR1 rows now score). Coverage 0×571, 10×45, 17×155, 20×25,
23×2, 26×11, 27×51, 40×142.

Federal-item Digi-Key images were started then **dropped from this run** — `image_url`
stripped from `federal_items.csv`; `federal_item_images.csv` deleted. Candidate
`part_images.csv` is unchanged.

Phase 3 dry-run: skip S1–S4/K1/L2/L3, 30 demo-path NSNs, top 3, **60 distinct MPNs**
(under the 90 ceiling). Live `--verify-counting 1N4002` failed: Nexar token
`invalid_client`. **0 credits used.** `NEXAR_CLIENT_ID` in `.env.local` is not a
client UUID (it looks like a pasted access token). Put the portal client id there
and re-run verify-counting, then the 60-MPN enrich. `NEXAR_PRO_FIELDS=0` so
lifecycle will stay `unknown` even after a successful run unless you flip it.

### 2026-09-05 (session 8) — hard gates + weighted composite (S9 rewrite)

`docs/CURSOR-composite-risk.md` Phases 0–2. **S7 / Nexar was not run.** Lifetime ceiling lowered
to 100 (`NEXAR_LIFETIME_BUDGET`) / per-run 90 in `.env`, `.env.local`, `.env.example`, and S7
defaults. `cache/nexar/` and `cache/nexar_usage.json` do not exist (0 matched parts used).

**Phase 0** (hard gates alone, missing = unverified): 1 002 in → **814 survivors / 188 eliminated**.
`5935-00-079-5458` (J1): 32 in, **26 survivors**. `coupling_type` is absent on every J1 candidate
so it never fails. The 6 kills are external MPNs with the wrong `contact_count` (1–6 vs 37).
Gates not loosened.

**Phase 1** — S9: gates first (violation → high, score 100, skip composite); survivors get the
60+40 weighted composite with renormalization. New extras: `composite_score`, `weight_coverage_pct`,
`gates_failed`, `country_of_origin` (all `unknown`). `spec_deltas.gate_type` = hard|soft|burden.
**Kept `pass_count` / `fail_count` / `marginal_count` / `unknown_count`** — SCHEMA §3 lists them
and `rank_candidates` still uses them as tie-breakers. CAGE gate not added. Qualification burden
unchanged.

**Phase 2** — S8 → S9 pass 1 (no `commercial_parts`):

| | count |
|---|---|
| profiles / candidate attrs / pairs | 548 / 14 748 / 1 002 |
| spec_deltas | 13 676 (burden 5 733 / hard 5 173 / soft 2 770) |
| hard-gate fails | 188 |
| risk | **197 high / 533 medium / 272 low** (was 36 / 841 / 125) |
| composite | null 433; min 0 / median 48.1 / max 100; score=100 ×197; score=0 ×96 |
| coverage | 0×621, 10×45, 17×155, 23×2, 26×11, 27×51, **40×117** |
| country_of_origin | unknown ×1002; enriched false ×1002 |

433 null composites are all survivors with `available_weight=0` → medium, not 0:
5999×279 (no plating/material), 5935×104 (backshells / unfiltered leaves), 5915×25
(`insertion_loss` unpublished), 5961×25 (no category attrs + unenriched). The 621 coverage-0
rows are those 433 plus the 188 gate fails (coverage forced to 0 when composite is skipped).
The expected unenriched cluster at 40 is 117 rows (full 5935/5999 category pool).

Zero-survivor NSNs (all high / 100): S1–S4 (temp), K1 (temp 200 °C + sealing), L2/L3 (temp).
J1 after scoring: 25 low / 1 medium (null composite) / 6 high.

**Stopped before Phase 3 / Octopart.** Do not run S7 until confirmed.

### 2026-09-05 (session 7) — test protocols → qualification class

`thermal_shock_cycles`, `vibration_grms`, `humidity_resistance` reclassified from environmental to
qualification in `data/spec_requirements.csv` (48 rows). They are MIL-SPEC test protocols, not
distributor-published attributes, so scoring them guaranteed three unknowns per item (~30 pp of the
unknown ratio). Thresholds unchanged. S5 → S8 → S9 pass 1 only.

- Profiles still 548; the three attrs are now `requirement_class=qualification` (39 items each).
- Deltas still 13 676. **Scored 7 943** (was 10 901). **Burden 5 733** (was 2 775). The three
  moved 2 958 rows (986 candidates × 3) into the burden bucket; verdict remains `unknown`.
- Median scored-unknown ratio: **40%** (was 57%). Mean 52%, min 0%, max 100%. 776/1002 still >30%.
- risk: **high 36 / medium 841 / low 125** (was 36 / 966 / 0). High unchanged (still fail>2).
  All 125 low are Digi-Key; external still 36 medium / 10 high.
- `qualification_gap_count`: 0×16, **5×287, 6×595, 7×104** (was 0/2/3/4). Per NSN: 0×2, 5×11, 6×24, 7×4.
  Same two 5950 coils still at 0.

### 2026-09-05 (session 6) — part_images.csv

S6 now writes `data/part_images.csv` (`mpn`, `image_url`) from Digi-Key `PhotoUrl`. One row per MPN;
first non-empty URL wins. Re-ran S6 from cache (0 live requests, 181 hits). 603 of 647 distinct
Digi-Key MPNs have a photo; 44 have none. External candidates are not in this file. `candidates.csv`
still 956 rows (`_photo` is now in `raw_attributes_json` but S8 skips `_` keys, so S8/S9 were not
re-run). Not a SCHEMA contract file — upload only if Foundry wants thumbnails.

### 2026-09-05 (session 5) — external candidates + qualification-as-burden; Foundry package

S5 / S6 / S10 / S11 outputs were not regenerated. S7 / Nexar was not run. `fix_risk_variance.py`
was not in the repo (nothing to discard).

**S8 — ingest `data/candidates_external.csv`**
- Long format (137 rows, 20 MPNs, 8 NSNs, 46 `(nsn, mpn)` pairs). Same MPN on several NSNs is
  legitimate. Zero overlap with Digi-Key `(nsn, mpn)` pairs.
- `gather_normalized_candidates` concatenates Digi-Key + external; `source` is `digikey` | `external`
  (non-contract column on `candidates_normalized.csv`, carried through to `substitution_candidates`).
- Boundaries: 548 profiles (0 unparsed) → 14 748 candidate attrs (4 unparsed, same `-10/ -80ppm/°C`
  as before) → 1 002 pairs (956 Digi-Key + 46 external). External coverage is thin (median 3 attrs).

**S9 — `docs/CHANGE-qualification-burden.md`**
- Risk is computed over `electrical` / `mechanical` / `environmental` only.
- `qualification` and `traceability` are excluded from counts and `risk_level`. `low` no longer
  requires `qpl_listed`.
- New extras on `substitution_candidates`: `qualification_gap_count`, `qualification_gap_summary`
  (identical across candidates of an NSN). New extra on `spec_deltas`: `scored`. Burden deltas keep
  `verdict=unknown` (2 775 / 2 775); never `fail`.
- `qpl_listed` stays as a column (false × 1 002). Pair-join logic unchanged.

**Pass-1 results (no `commercial_parts`)**
- risk: **high 36 / medium 966 / low 0**. Digi-Key 26 high / 930 medium; external 10 high / 36 medium.
- 36 high = `fail_count > 2`. 289 medium have 1–2 technical fails; the rest are the unknown cap.
- median unknown ratio over **scored** requirements: **57%** (mean 65%, min 30%, max 100%).
  996 / 1 002 exceed 30% unknown. The other 6 are high (3 fails each) — so `low` is empty for a
  real reason, not a QPL gate.
- `qualification_gap_count` per candidate: 0×16, 2×287, 3×595, 4×104. Per NSN: 0×2, 2×11, 3×24, 4×4.
  The two gap-0 NSNs are the 5950 coils (`5950-01-199-2306`, `5950-01-224-5861`) — their profiles have
  no qualification/traceability rows. Constant per NSN (0 NSNs with a mixed gap).
- Sample rationale: `Meets 5 of 10 measurable requirements; fails temperature_coefficient; 4 unknown.
  3 qualification clauses under MIL-PRF-39007 require testing to verify. lifecycle/availability
  unknown (not enriched)` (`5905-01-173-4875__AC0805DR-071RL`).
- Degradation without S7: `lifecycle_status=unknown` ×1002, `enriched=false` ×1002, commercial /
  gov prices and `price_delta_indicative` null ×1002. Exit 0.

**5905 resistance gap:** left as-is. Did **not** add a `spec_requirements_overrides.csv` row.
`5905-00-493-1204` / `-1210` still have no `resistance` requirement (25 candidates each).

**Foundry package** — SCHEMA.txt contract columns match exactly (prefix); extras after. All PKs
unique/non-empty; all FKs resolve except `candidate_mpn → commercial_parts.mpn` (file not emitted).
Upload list is under "Foundry upload" below. `commercial_parts.csv` is the one SCHEMA §11 file that
does not exist.

**Docs that now also disagree with the code** (still not edited): SCHEMA §10 and PRD S9 still say
qualification-class fail → high and `low` requires QPL. CHANGE-qualification-burden.md is the
approved override; SCHEMA/PRD need a pass when you want them.

### 2026-09-05 (session 4) — first live S6 → S8 → S10 → S9 pass 1; stopped before S7

Inputs are still the **synthetic** S1/S2/S4 files (41 items, 317 characteristics, 177 spec requirements,
82 QPL rows). Not regenerated. Everything below `S5` was rebuilt from scratch this session.

**Bugs found and fixed (each was returning confidently wrong results, none had a test)**
- `common.load_env` only read `.env`; the keys live in `.env.local`. Now `.env` then `.env.local` (override).
- **Separate `uom` column ignored.** `characteristics.csv` holds `10000` with `uom=pF`; the normaliser read
  `10000 F`. Fixed with `s08.value_with_uom` (S5 `_losing_value` and S8 `normalize_profiles`). Covered by
  `TestSeparateUomColumn` (3 tests: pF, Mohm, degC, ppm_per_C, unitless count; contextual tolerance).
  Spot-checked on real rows: `10000.0 pF → 1e-08 F`, `5000 Mohm → 5e9 ohm`, `-55 C → -55 degC`.
- `fsc_category_map.csv`: 8 of 9 category ids were wrong or non-leaf (a parent returns no `ParametricFilters`).
  Verified live; now one-or-more **leaf** ids per FSC (`|`-separated). S6 picks leaves by item-name token
  overlap, falls back to the remaining leaves only when a real parametric search returned zero products
  (not when a leaf simply has no usable filters — a backshell must not borrow connector assemblies).
- S6 read `FilterOptions.ParametricFilters[].Values[].ValueText`; the v4 API returns `FilterValues[].ValueName`.
  Parametric filtering had therefore never fired. Both shapes accepted now.
- S6 `Children` vs `ChildCategories` in the category tree; `--nsn` matched 13-digit against dashed NSNs
  (S10 had the same bug); `categories()` fetched a token before checking the cache (broke `--dry-run`);
  `value_satisfies` compared `"5VDC"` to `"5 V"` as text for `exact` numeric attributes.
- S6 keywords: the PUB LOG item name matches nothing inside the right leaf. Keywords are now value-only
  ("150 Ohms"), empty keyword + leaf + parametric filters otherwise.
- S6 pooled unfiltered step-1 rows from one leaf with parametric rows from another. Now: if any leaf returned
  parametric results those are the candidate set; filler is used only when nothing parametric exists.
  Six-figure post-filter hit sets are labelled `search_mode=parametric_loose` and warned.
- `Tolerance` on Digi-Key is generic; `contextual_attr` resolves it to `resistance_/capacitance_/inductance_tolerance`
  from the part's other attributes; for S6 targets it uses whichever `*_tolerance` the profile has.
- `Shell Size, MIL` on Digi-Key is a MIL-DTL-38999 **letter** (A–J); profiles hold numbers. `SHELL_SIZE_LETTERS`
  maps A=9 … J=25 in S8. Connector hit sets went from 35 000–58 000 to 52–2 400.
- Ceramic dielectric class (X7R, C0G/NP0) is filed under "Temperature Coefficient" by Digi-Key; the value
  now routes it to `dielectric_type`, and `normalize_dielectric` folds NP0 → C0G, drops "ceramic".
- Digi-Key "value @ condition" strings ("30 @ 7.9MHz") now parse to the value.
- S9 text compare: `SPDT` vs `SPDT (1 Form C)`, `ALUMINUM` vs `ALUMINUM ALLOY`, `GOLD` vs `GOLD OVER NICKEL`
  were `fail`; containment is now `marginal` (a human decides), exact still `pass`.
- S9 pass 1 without `commercial_parts`: rationale now states "lifecycle/availability unknown (not enriched)";
  `lifecycle_status` (`unknown`) and `distributor_count` (null) are carried as extra columns on
  `substitution_candidates.csv`. Missing enrichment never reads as pass; `low` still requires QPL + all pass.
- **Operator direction.** The synthetic S4 extraction used `eq`/`gte` as a lazy default on ceiling attributes
  (`resistance_tolerance >= 0.5 %` failed 0.1 % parts; `temperature_coefficient == 100 ppm` failed 25 ppm parts).
  Source data untouched; 18 corrections live in `config/spec_requirements_overrides.csv`, and **S5 now applies
  the overrides too** (same `s04.apply_overrides`), so a correction does not require re-running S4. S8 warns
  whenever a stated operator contradicts the attribute's rating direction (`DEFAULT_OPERATORS`).
- S5 crashed on `NaN` in `origin` after the override concat; the failure was masked in a chained shell command
  and one S8/S9 run happened on stale S5 output before it was caught. Re-run cleanly afterwards.
- 30 Digi-Key aliases added (`Number of Positions`, `Shell Size, MIL`, `Current Rating (Amps)`, `Coil Voltage`,
  `Contact Form`, `Circuit`, `Voltage Rating - DC`, `Contact Rating (Current)`, `DC Resistance (DCR)`,
  `Q @ Freq`, `Operating Temperature - Junction` → `junction_temp` (new range attr), …).

**Live S6 (41 NSNs, 956 candidates, 67 live + 110 cached requests first run)**
- 40 NSNs at 25 candidates (the cap), `5910-01-204-1981` at 1, `5930-01-110-7093` at 14, two 5950 coils at 8.
- Zero NSNs with no candidates (5915 filter found 1 + 50 via the fallback leaves 835/845).
- **Temperature-only filtering** (aliases cannot help — the leaf has no other matching parameter):
  6 × 5999 contacts/gaskets (leaves 945/869).
- **No filters at all** (unfiltered leaf, `search_mode=no_filter_values_matched`): 3 backshells + 1 adapter
  (5935; leaves 313/378 have no positions/temperature parameters) and 2 heat sinks (5999, leaf 219).
- **Six-figure after filtering** (`parametric_loose`): `5905-00-493-1204` and `-1210` — their profiles have
  **no `resistance` requirement** (synthetic data gap), so 131 k–570 k resistors match. Not a candidate set.

**S8 → S10 → S9 pass 1 boundaries**
- S5 548 profiles → S8 548 normalised (0 unparsed). S6 956 candidates → S8 14 611 attribute rows
  (4 unparsed: `-10/ -80ppm/°C`). S10 41 price rows, 0 with history. S9 956 candidates, 12 996 deltas.
- risk: **high 940 / medium 16 / low 0**. Verdicts: unknown 8 494, pass 2 905, fail 1 390, marginal 207.
  956/956 candidates have > 30 % unknown verdicts (median 57 %).
- **Why high:** every item except the three 5950 coils carries a `qualification` requirement ("QPL listing
  required"); no Digi-Key candidate MPN appears in the synthetic QPL (0 overlap), so 940 fail a
  qualification-class requirement → high (SCHEMA §10). That is the rule working, not aliasing.
  With `qualification` set aside the picture is: 669 medium (> 30 % unknown), 261 medium (1–2 fails),
  26 high (> 2 fails). `low` is impossible until a candidate is QPL-listed.
- **Unknowns are source coverage, not aliasing:** the 100 %-unknown names are spec clauses Digi-Key does not
  publish — `humidity_resistance`, `thermal_shock_cycles`, `vibration_grms`, `lot_traceability`,
  `solderability_per_j_std_002`, `dpa_sample_required`, `insulation_resistance`, `contact_resistance`,
  `dielectric_withstanding_voltage`, `coupling_type`, `actuation_pressure`, `mechanical_life`,
  `dissipation_factor`, `coil_resistance`, `insertion_loss`, `frequency_range_max`. Partial unknowns
  (`shell_material`, `contact_count`, `contact_plating`, `current_rating`, `operating_temp_*`) come from the
  unfiltered leaves above (backshells, heat sinks, gaskets) whose parts carry none of those attributes.
- Real fails: `operating_temp_min/max` (138/114 — −40 °C parts against −55 °C), `shell_material` 81,
  `dielectric_type` 26 (ceramic candidates for a metallized-paper item — the 5910 map has no paper/film leaf
  match), `inductance_tolerance` 25, `temperature_coefficient` 21.
- Degradation confirmed: `lifecycle_status=unknown` ×956, `commercial_unit_price`/`distributor_count`/
  `gov_unit_price` null ×956, `enriched=false`, S9 exit 0.

**Not run:** S7 / Nexar (per instruction). `--verify-counting` still pending.

### 2026-09-05 (session 3) — docs/SCHEMA.txt adopted as the contract

`docs/SCHEMA.txt` arrived "for reference" but is a shared contract that changes column sets and semantics.
Applied in full; verified with unit tests, the fixture chain, and a synthetic S6→S8→S9→S7(dry)→S8→S9 run
(synthetic inputs deleted afterwards — `data/` holds only fixture-derived outputs).

**Contract changes applied**
- `operator` is the closed enum `eq | gte | lte | range | in_set | boolean` everywhere (S4 prompt +
  validation, S5, S8, S9, tests). Symbol spellings (`>=`, `==`, …) are still accepted on INPUT (overrides,
  hand-made files, model output) and mapped; anything unrecognised is dropped with a reason, never coerced.
  `range` values are `min|max` (was `lo..hi`); S8 also parses `min|max` back, so the contract form is
  idempotent. `in_set` = pipe-delimited normalised members; `boolean` = `true`/`false`.
- `requirement_profiles`: + `conflicting_value`, `conflicting_source` (nullable). Merge precedence now has
  the 0.70 confidence floor: spec wins only if extraction confidence ≥ 0.70, otherwise PUB LOG wins and the
  extraction stays in `spec_requirements`; `manual_override` always wins. The loser is STORED on the winner
  (contract form when parseable), not just logged.
- `substitution_candidates`: `estimated_savings` REMOVED. Now `gov_unit_price, gov_quantity,
  commercial_unit_price, commercial_basis_qty, price_delta_indicative` (SCHEMA §3). `rank_within_nsn`
  mirrors the UI sort: `risk_rank` first, `price_delta_indicative` descending only within a band, nulls last.
- QPL check is the PAIR join (SCHEMA §3/§6): `qualified_part_number == mpn AND (cage_code OR
  manufacturer_name)`, filtered to the governing spec. The previous manufacturer-only match is gone — it made
  every Vishay part "listed" against a Vishay QPL entry. A `qualification` boolean requirement is judged by
  this check (pass iff listed), so a "QPL listing required" spec line makes every unlisted candidate `high`.
- Risk rollup adds the marginal cap: >30% marginal → `medium`; `low` requires marginal ≤ 30%.
- `lifecycle_status` is the closed enum `active | nrnd | obsolete | unknown` in `commercial_parts.csv` itself
  (S7 writes it; S8 re-normalises idempotently). EOL / last-time-buy → `obsolete`. Nexar's wording is kept in
  the `lifecycle_raw` extra; `price_basis_qty` extra = 1000 (`medianPrice1000`).
- Surrogate PKs: `price_history.price_id` = `{nsn}__{fiscal_year}` (`__none` for the null row);
  `qpl.qpl_id` = `{spec}__{cage}__{part}`. `qpl.source_spec_file` (PRD) is now an extra, not contract.
- `spec_requirements` values are pre-normalised too (SCHEMA §8 says both tables are); verbatim text is kept
  in `value_raw` / `uom_raw` extras with `parse_status`. `PROMPT_VERSION` bumped to v2.
- S11 encodes the directional interface semantics (`rule_holds`, slot_a = left operand) and now picks rules
  the BASELINE satisfies; validation fails if a baseline violates its own interface. (The fixture previously
  generated `resistance numeric_lte` between a 10 kΩ and a 1 Ω slot — a violation before any decision.)

**Bugs found and fixed while doing this**
- `bare_value()` read `parse_status` but `Norm.as_row()` emits `kind`, so the new S4/S5 contract-value calls
  returned "". Now accepts either.
- `s04.apply_overrides` crashed on an EMPTY extracted frame (tuple `==` against an empty Series) — exactly the
  scanned-PDF-described-by-overrides case.
- `-55|150` (the SCHEMA range form) was not recognised as a range by S8, so a spec range stayed one
  `operating_temp range` row instead of splitting into `_min`/`_max`.

**SCHEMA.txt issues to raise with its author (not changed by me)**
- §7 says `mcrl` and `characteristics` "never reach Foundry"; §11's file list includes them. The scripts
  emit both either way.
- §1 `requirement_profiles` and §8 say `range` values are `min|max`; the pipeline still SPLITS
  `operating_temp` into `operating_temp_min` (lte) / `operating_temp_max` (gte) so a distributor's
  `-55°C ~ 155°C` can be compared per bound. `range` therefore appears only for other range-valued
  attributes. `conflicting_value` on the split rows carries the pre-split `min|max`.
- §3 "gov_quantity — quantity on that contract": USAspending rarely states one (see S10 note below), so it
  is null in most rows even when `gov_unit_price` exists.
- §6 says CAGE is preferred; Digi-Key/Nexar return none, so the pair join runs on manufacturer name in
  practice. `qpl_listed(..., cage=)` is wired for when a source supplies one.

### 2026-09-05 (session 2) — PRD review, remaining scripts, PRD changes

**Built**
- `s10_usaspending_prices.py`, `s07_nexar_enrich.py`, `s03_assist_fetch.py`, `s04_spec_extract.py`.
- This file.

**PRD changes applied (three revisions this session)**
- S9 emits `risk_rank` (1/2/3 = low/medium/high) after `risk_level`; `risk_rank()` raises on any other value.
- S4 emits `data/specifications.csv` (spec_id, title, requirement_count; + pages, text_chars, chunks,
  pdf_path, sha256, status extras) and `spec_requirement_id` (= `{spec_id}__{requirement_name}`) on
  `spec_requirements.csv`.
- S5 `requirement_profiles.csv` now: `requirement_id` (= `{nsn}__{requirement_name}`, recomputed after
  the operating_temp min/max split), `source` ∈ {publog_characteristics, spec_extraction, manual_override},
  `source_spec_id` (null for PUB LOG rows). Rows S4 marked `origin=override` become `manual_override`.
  S8 carries `requirement_id` / `source_spec_id` through to `requirement_profiles_normalized.csv`; S9 carries
  `source_spec_id` into `spec_deltas.csv` as an extra.
- Earlier (session 1, same day): `candidate_id`, `delta_id`, `estimated_savings`, pre-normalised contract
  values, S11 closed `match_rule` grammar + `constrained_attribute` validation.

**Other changes**
- S9: a requirement named `qualification` (e.g. "QPL listing required", which only spec extraction or an
  override produces) is now judged by the QPL check itself (pass iff QPL-listed) instead of being compared
  against a candidate attribute no distributor carries. Without this the PRD's
  "high — any qualification-class requirement fails" could never fire. Test added.
- S9 rationale wording for zero stocked distributors.

**PRD compliance review of everything built (findings; all deviations are deliberate and listed here)**

Conformant: output file set and column names/order for all 13 contract files (extras are appended after the
contract columns and logged at write time); cache-on-arrival for every API; polite 1 req/s + contact UA on
government sources; `unknown` never coerced to pass; risk rollup rules; `estimated_savings` null when either
side missing; S6 cap 25 ranked by closeness; S7 top-3 handoff, per-MPN cache, counter that raises; S11
generic names, ~6 interface slots, closed grammar, loud validation failure; no scrapers; no DB; no secrets
(env only, `.env.example`); tests only on score/normalize.

Deviations / interpretations (need your OK or awareness):
1. **S3 does not search ASSIST.** No API exists; QuickSearch PDF links are generated behind an HTML search
   page (checked live: neither host publishes a robots.txt, but obtaining a link still means parsing HTML =
   a scraper, which CLAUDE.md forbids). S3 downloads from explicit URLs in `config/spec_sources.csv`, indexes
   PDFs dropped into `data/specs/`, and prints exactly which document numbers still need a source.
2. ~~S9 QPL check matches on manufacturer name or part number~~ — superseded in session 3 by SCHEMA §3's pair
   join (part number AND manufacturer/CAGE). Digi-Key and Nexar still return no CAGE, so manufacturer name
   is the second half of the pair in practice.
3. **S10 `unit_price_avg` will be mostly null.** Verified live: USAspending has no quantity/unit-price fields;
   DLA's own buys are described as `<PR number>!<ITEM NAME>` with no NSN, so they are invisible to an NSN
   search. Hits come from other services (e.g. Coast Guard) whose descriptions contain the NSN and
   occasionally "QTY: 4". S10 derives a unit price only from transactions that state a quantity, records
   the basis in `price_basis`, and emits the PRD's null row otherwise. `vendor_cage` is always empty (UEI
   only); `vendor_name`/`vendor_uei` extras carry what exists. Consequence: `gov_unit_price` and therefore
   `price_delta_indicative` will be null for most candidates — SCHEMA/PRD say the UI handles this.
4. **Risk rollup gap:** 0 fails, within the unknown and marginal caps, all pass/marginal but NOT QPL-listed is
   undefined in both PRD and SCHEMA §10; classified `medium` (never `low` without QPL). 0 requirements → `medium`.
5. **S11 in fake-data mode** (build-order step 2, before S5 exists) writes UNVALIDATED interfaces with a loud
   banner instead of failing, because there is nothing to validate against yet. With `requirement_profiles.csv`
   present it validates and exits non-zero on any violation.
6. **S5 drops PUB LOG characteristics with no canonical alias by default** (`--keep-unaliased` to keep). They
   cannot be scored against any distributor attribute and would inflate the unknown ratio to `medium` for
   everything. Dropped rows are logged with the reason.
7. **Spec join key** (S3/S5): the 3–6 digit document number shared by a PUB LOG reference part number and a
   spec id (`M55342K06B10E0R` ↔ `MIL-PRF-55342` → 55342). References with no such run (e.g. `RWR80S1R00FR`,
   whose spec is MIL-PRF-39007) cannot be joined; both scripts log them. Fix would be an explicit
   `governing_spec_ref → spec_id` mapping column in `config/spec_sources.csv` — not added without approval.
8. **Nexar plan default** is 1,000 parts/month per Nexar's docs; the PRD's 300-lifetime figure is what the code
   enforces (`NEXAR_LIFETIME_BUDGET`, `NEXAR_MAX_MPNS_PER_RUN`). Lifecycle/datasheet need the Tech Specs
   add-on (`NEXAR_PRO_FIELDS=0` if the plan lacks it; S8 then maps empty lifecycle to `unknown`).
9. `pypdf` was added to `requirements.txt` (S4) without prior approval — CLAUDE.md says ask first. It is the
   only non-stdlib dependency beyond pandas/requests. Say so if you want it swapped or removed.
10. Contract files carry extra non-contract columns after the contract ones (e.g. `mcrl.csv` rncc/rnvc,
    `characteristics.csv` mrc, `substitution_candidates.csv` rank_within_nsn/manufacturer/…). Foundry's
    Pipeline Builder can ignore them; say if you want them stripped.

**Docs that need updating (I do not edit docs without your go-ahead — CLAUDE.md)**
- `docs/PRD.md` vs `docs/SCHEMA.txt` — the code follows SCHEMA; PRD still says:
  - S9 emits `estimated_savings` (§S9 outputs, the bold paragraph, build-order step 8, the "cut S10" note).
    SCHEMA §3 removes it in favour of `gov_unit_price, gov_quantity, commercial_unit_price,
    commercial_basis_qty, price_delta_indicative` and explains why.
  - S5 output lacks `conflicting_value, conflicting_source`; "the specification wins and the conflict is
    logged" — SCHEMA §1 adds the 0.70 confidence floor and stores the conflict.
  - S2 `qpl.csv` has no `qpl_id` and lists `source_spec_file` (SCHEMA §6 has `qpl_id`, no `source_spec_file`).
  - S10 `price_history.csv` has no `price_id` (SCHEMA §1).
  - Risk rollup lacks the marginal >30% → medium rule and the marginal ≤30% condition on low (SCHEMA §10).
    CHANGE-qualification-burden.md further changes the *scope* (scored classes only; `low` without QPL)
    and adds `qualification_gap_*` / `scored`. CURSOR-composite-risk.md then replaces the count rollup
    with hard gates + weighted composite — SCHEMA §3/§10 and the PRD still describe the old rule.
  - S9 "is the candidate's CAGE listed in qpl.csv" — SCHEMA §3 defines the (part number AND cage/manufacturer)
    pair join.
  - No operator enum is stated; SCHEMA §8 closes it to `eq|gte|lte|range|in_set|boolean`.
  - `lifecycle_status` enum `active|nrnd|obsolete|unknown` (SCHEMA §8) is not in the PRD.
- `docs/FOUNDRY-BUILD.md`: still says `SubstitutionCandidate` PK is composite `(nsn, candidate_mpn)` (must be
  `candidate_id`); `Specification` object should be backed by `specifications.csv` not `spec_requirements.csv`;
  `SpecDelta` now has `delta_id`; `Requirement` has `requirement_id`, `conflicting_value/_source`;
  `PriceHistory` needs `price_id`, `Qpl` needs `qpl_id`; candidate table sorts on `risk_rank` then
  `price_delta_indicative` within band and must show all four price fields, never the delta alone (SCHEMA §3);
  `SubstitutionCandidate` key properties: `risk_rank` and the five price fields, not `estimated_savings`.
- `docs/CLAUDE.md` says PRD.md is the source of truth; with SCHEMA.txt now the shared contract, say which wins.
- `docs/CLAUDE.md`: "Commands" section is still the placeholder — see Commands above. Project structure should
  add `raw/` (manual downloads, git-ignored), `tests/`, `docs/`. `CLAUDE.md` lives in `docs/` while
  `docs/always.mdc` says "at the project root".
- `docs/PRD.md` S1 note says PUB LOG is "fixed-width or pipe-delimited"; the distribution has been CSV since
  1 Apr 2023 (S1 is written for the CSV files).
- Not created: `GUIDELINES.md` (your standing rule asks me to read it; it does not exist).

## Known issues / open items

- **Blocked on you:** read the Nexar portal usage meter after `--verify-counting 1N4002`
  (local +2; we need +1 vs +2). Anthropic key untested; real PUB LOG CSVs → `raw/publog/`;
  QPL `.mdb` files → `raw/qpl/` (+ `brew install mdbtools`); spec PDFs or URLs →
  `config/spec_sources.csv` / `data/specs/` (`data/specs/` is empty; `spec_requirements.csv`
  is synthetic).
- **Nexar counting basis: LOCAL +2 on two live queries of 1N4002.** Portal meter not read
  from here. Local lifetime counter is **62 / 100** (includes one raw-cache hit counted as
  matched). Do not run S7 again without checking the portal.
- **7 NSNs have zero hard-gate survivors** (all high/100): S1–S4 (operating temp), K1 (200 °C +
  sealing), L2/L3 (temp). Not loosened — J1 was the only stop condition and it has 26 survivors.
- **Coverage still clusters at 0 (535) and 40 (138);** lead-time enrichment added 38–78
  bands (78×4). Null composite ×347, all `unscored` (not medium).
- **Two 5950 coils have `qualification_gap_count=0`** (`5950-01-199-2306`, `5950-01-224-5861`). Their
  synthetic profiles have no qualification/traceability rows. Every other NSN is 2–4.
- `5905-00-493-1204` / `-1210` have no `resistance` requirement → `search_mode=parametric_loose`, 25 arbitrary
  resistors each. Synthetic data gap; do not trust those 50 rows.
- 5935 backshells/adapter and 5999 heat sinks: their Digi-Key leaves expose no parameter the profiles use →
  `no_filter_values_matched`, 25 unfiltered parts each. Honest label, weak candidates.
- 5910 `Paper Metallized Fixed Capacitor` searched Ceramic (leaf 60) — the map's second 5910 leaf (62, Film)
  was not picked by name overlap. 26 `dielectric_type` fails are that mismatch.
- `MIL-PRF-83446 dc_resistance eq` is the one remaining direction-contradicting spec row; PUB LOG wins it
  (confidence 0.43) so it is inert. Not overridden.
- 101 Digi-Key attribute names unaliased (Features, Color, Width, Shell Size - Insert, …). None maps to a
  profile requirement; carried under their slug, never scored.
- USAspending: the synthetic NSNs have no contract history; every `gov_unit_price` is null. The endpoint
  itself was verified live with a real keyword. Expect real NSNs to populate.
- `config/publog_columns.json` and `config/qpl_columns.json` are best guesses; S1 `--print-headers` and S2
  `--inspect` exist to correct them on first contact with real files.
- `data/` holds outputs from the **synthetic** S1/S2/S4 files (41 items), not real data. Everything from
  S5 down regenerates from a clean `data/`; `cache/digikey/` (181) and `cache/usaspending/` (132) make the
  re-run free.
- S4 has no OCR: scanned-image PDFs are logged (`status=no_text`) and skipped. Many older MIL specs are
  scanned. Would need a new dependency; not added.
- S4's system prompt is `PROMPT_VERSION=v1`, untested against a real spec; expect to iterate once a key exists
  (bump the version to invalidate the cache).
- `raw/` (manual downloads; git-ignored) does not exist yet — S1/S2 create nothing there and exit with the
  expected path when it is missing.
- Interfaces on the fixture: only 1 (two items with profiles). Real data yields ~6 slots as the PRD wants.
- S8 does not resolve a bare SI prefix without a unit (`22k` for a resistance) — it is `unparsed` and scores
  `unknown`. Deliberately conservative; say if you want the attribute's unit family assumed.
- `in_set`/`boolean` requirements are scoreable in S9, but S6 ignores them when building Digi-Key search
  parameters and S11 never offers them as interface attributes (they are not single comparable values).
- `common.CachedSession` writes a `.error` cache entry for non-retryable failures (inspectable) but the
  caller re-requests next run — intended for transient auth problems, but means a permanently-bad request
  is retried every run.

## Flags from session 1 (2026-09-05, earlier) — kept for the record

- `docs/CLAUDE.md` location vs `always.mdc` (see above).
- PUB LOG distribution is CSV, not fixed-width (see above).
- `raw/` directory introduced for manual downloads (git-ignored); `PUBLOG_DIR` / `QPL_DIR` env overrides.
- `config/attribute_aliases.csv`, `config/publog_columns.json`, `config/qpl_columns.json`,
  `config/spec_sources.csv`, `config/README.md` were added as hand-authored inputs beyond the three the PRD
  names; all documented in `config/README.md`.
- Tests use stdlib `unittest` (no pytest dependency).
