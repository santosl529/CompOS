# PRD — Part Substitution Data Pipeline (pre-Foundry scripts)

## What this is

A set of Python scripts, run **outside Palantir Foundry** on a developer laptop, that
assemble the datasets for a defense part-substitution application. The scripts pull
from government catalogs and commercial distributor APIs, join them, score candidate
substitutions, and emit flat files (CSV/Parquet) that are uploaded into Foundry as
datasets.

Foundry does the ontology modeling, the app UI, and the interactive decision layer.
These scripts do everything before that.

**This document covers the scripts only.** The Foundry build is specified separately
in `FOUNDRY-BUILD.md`.

## Context

Built for a hackathon by a team of four (2 CS, 1 mechanical engineering, 1 business)
over roughly two days. The Foundry tenant already exists. Optimize for:

- **Getting to a working end-to-end path fast**, not for robustness or scale
- **Reproducibility of the precompute run** — the demo runs on materialized output,
  not on live API calls
- **Auditable provenance** — every row must be traceable to a named public source

Intended scale: ~150 federal items, 2–3 Federal Supply Classes, one synthetic assembly.
This is a demo, not a product. Do not build for more.

## Core thesis

Commercial part catalogs (Digi-Key, Octopart) know what you can *buy*. Government
catalogs (PUB LOG, QPL) know what is *qualified*. Nobody joins them. The join is the
product: given a federal stock number, find commercial parts that could replace it,
and quantify exactly which qualification requirements each candidate fails.

The output is never "here's a cheaper part." It is "this candidate meets 9 of 11
requirements, fails operating temperature, and is not QPL-listed against the governing
specification."

## Non-negotiable constraints

1. **No scraping of sites that prohibit it.** McMaster-Carr, Grainger, Fastenal,
   Haystack Gold, and NSN broker sites are off-limits. DLA Land and Maritime's
   `landandmaritimeapps.dla.mil` disallows automated access in robots.txt — its
   qualification databases are downloaded **manually** and committed as local files.
2. **Nexar/Octopart quota is 300 API calls total.** This is a hard project-lifetime
   budget. See "Quota discipline" below.
3. **All data must be public.** No real weapon system bills of material. Assembly
   structure is synthetic; only catalog data is real. This must be stated in the UI.
4. **Every API response is cached to disk on arrival.** A re-run must never re-query.
5. **No secrets in the repo.** API credentials come from environment variables, listed
   in `.env.example`.

## Quota discipline (Nexar)

**Nexar meters matched parts, not API calls. The budget is 300 matched parts for the
entire project lifetime.** Batching MPNs into one query saves nothing — 100 parts in a
single call still costs 100.

This is the tightest constraint in the project and it dictates the division of labour
between the two commercial APIs.

### Division of labour

| Job | Tool | Nexar cost |
|---|---|---|
| Candidate discovery / parametric search | Digi-Key | 0 |
| Enrichment of surviving finalists | Nexar | 1 per MPN |
| Baseline mil-spec part price | USAspending (not Nexar) | 0 |

Nexar is **never** used for discovery. Digi-Key parametric search does that job with
better filters on a quota that isn't scarce. Nexar is used only to enrich a small,
already-filtered set of finalists, because it supplies two things Digi-Key cannot:
cross-distributor lifecycle status (active / NRND / obsolete) and multi-source
availability. Obsolescence and single-sourcing are the substantive findings; price is
secondary.

The cost baseline for the federal item comes from USAspending contract history — what
the government actually paid — never from a commercial lookup of the mil-spec part.
This is both free and more defensible than a list price.

### Budget

Enrich the **top 3 candidates per federal item**, ranked by score from S9.

| Phase | Matched parts |
|---|---|
| Development and testing | 45 |
| Precompute run (~60 items × 3) | 180 |
| Reserve (re-runs, demo-day fixes) | 75 |

If 60 items proves too few, raise items and lower candidates-per-item before touching
the reserve. The reserve exists because someone will re-run the pipeline late at night.

### Verify the counting basis first

Before any real run, query the **same MPN twice** and check whether the usage counter
moves. Most part-metered plans count *distinct* parts over the account lifetime rather
than per-query matches. Confirm which applies:

- **Distinct-part counting**: re-queries of already-seen MPNs are free. The real limit
  is 300 unique MPNs ever touched. Iteration is cheap; discovery is expensive.
- **Per-query counting**: every match costs regardless of history. Caching becomes
  load-bearing rather than merely useful.

Two queries, thirty seconds, and it determines how freely the pipeline can be re-run.

### Enforcement

Discipline alone is insufficient — enforce in code:

- A persisted counter at `cache/nexar_usage.json`, incremented per matched part.
- A `MAX_MPNS_PER_RUN` ceiling that **raises an exception** when exceeded. Not a
  warning, not a log line.
- A hard cap on the handoff from S6 to S7. An unfiltered Digi-Key result set piped into
  Nexar will consume the entire 300-part budget in one run. This is the single most
  likely way to lose the project.
- Every response written to `cache/nexar/{mpn}.json` on arrival, checked before any
  request.

## Inputs

The scripts are driven by config, not hardcoded values.

**`config/target_items.csv`** — produced by teammates from PUB LOG. The pipeline's
entry point. Columns:

```
nsn,fsc,item_name,notes
```

Until this file exists, scripts run against `config/target_items.sample.csv` with a
handful of hand-picked NSNs so development isn't blocked.

**`config/fsc_category_map.csv`** — hand-authored by the mechanical engineer. Maps a
Federal Supply Class to a distributor category and the parametric attributes that
drive a match. This is the highest-risk artifact in the project and has no automated
substitute. Columns:

```
fsc,digikey_category_id,driving_attributes,tolerance_rules
```

**`config/spec_requirements_overrides.csv`** — manual escape hatch. Any requirement the
LLM extraction gets wrong can be corrected here without a re-run.

## Scripts

Each script is independently runnable, reads from and writes to `data/`, and is
idempotent. Order matters; later scripts consume earlier outputs.

### S1 — `publog_ingest.py`

Parse the PUB LOG flat-file distribution (downloaded manually from the FLIS Electronic
Reading Room) and filter to the NSNs in `target_items.csv`.

Outputs:
- `data/federal_items.csv` — nsn, fsc, item_name, governing_spec_ref, cage_code
- `data/mcrl.csv` — nsn, reference_part_number, cage_code (the NSN ↔ MPN cross-reference)
- `data/characteristics.csv` — nsn, attribute_name, attribute_value, uom

Notes: PUB LOG files are fixed-width or pipe-delimited depending on segment; inspect
before parsing. Do not attempt to clean the whole distribution — filter first, clean
only what survives.

### S2 — `qpl_ingest.py`

Convert the manually-downloaded Microsoft Access qualification databases into CSV.

Outputs:
- `data/qpl.csv` — governing_spec, manufacturer_name, cage_code, qualified_part_number,
  qualification_date, source_spec_file

Notes: use `mdbtools` (`mdb-export`) for conversion. The internal schema of these files
has not been inspected — open one before designing around it. Schema may vary by
specification; normalize to the columns above and log anything dropped.

### S3 — `assist_fetch.py`

For each distinct `governing_spec_ref` in `federal_items.csv`, download the
specification PDF from ASSIST (`assist.dla.mil`).

Outputs: `data/specs/{spec_id}.pdf`, plus `data/spec_index.csv`.

Notes: polite fetching — 1 request/sec, descriptive User-Agent with contact email,
cache on disk, never re-download.

### S4 — `spec_extract.py`

LLM extraction over the downloaded PDFs. For each spec, produce structured
qualification requirements.

Outputs:
- `data/specifications.csv` — spec_id, title, requirement_count (one row per document)
- `data/spec_requirements.csv` — spec_requirement_id, spec_id, requirement_name,
  operator, value, uom, requirement_class, confidence, source_page (one row per
  extracted requirement)

`requirement_class` is one of: `electrical`, `mechanical`, `environmental`,
`qualification`, `traceability`.

Notes: prompt for JSON-only output and parse defensively. Record `confidence` and
`source_page` for every extracted requirement — an unverifiable requirement is worse
than a missing one. Apply `spec_requirements_overrides.csv` after extraction.

These two files are **provenance only**. The application reads `requirement_profiles`
(S5), not these. Keep them separate — `specifications` is document metadata,
`spec_requirements` is the extracted content.

### S5 — `build_requirement_profile.py`

Merge PUB LOG characteristics (S1) with extracted spec requirements (S4) into one
requirement profile per federal item. Where both sources cover the same attribute, the
specification wins and the conflict is logged.

Outputs: `data/requirement_profiles.csv` — requirement_id, nsn, requirement_name,
operator, value, uom, requirement_class, source, source_spec_id.

`source` ∈ {`publog_characteristics`, `spec_extraction`, `manual_override`}.
`source_spec_id` is populated only when `source` = `spec_extraction`, and is null
otherwise. It carries provenance forward so the application can trace a requirement back
to its governing document.

**This is the table the application reads.** If S3/S4 are cut, this file still works —
every row simply has `source` = `publog_characteristics` and a null `source_spec_id`.

### S6 — `digikey_search.py`

Candidate generation. For each federal item, use `fsc_category_map.csv` to translate
its profile into a Digi-Key parametric search, apply tolerance bands, and retrieve
candidate MPNs.

Outputs: `data/candidates.csv` — nsn, candidate_mpn, manufacturer, digikey_pn,
raw_attributes_json.

Cap at 25 candidates per federal item. Rank by attribute closeness before truncating,
so the cap doesn't discard good matches arbitrarily.

### S7 — `nexar_enrich.py`

Enrichment only — **never discovery**. Takes the top 3 candidates per federal item as
ranked by S9, and retrieves what Digi-Key cannot provide.

**Quota-governed.** Reads the persisted counter, refuses to exceed the ceiling, raises
rather than warns. Costs 1 matched part per MPN; batching does not help.

Outputs: `data/commercial_parts.csv` — mpn, manufacturer, lifecycle_status,
median_price, stock_qty, lead_time_days, datasheet_url, distributor_count.

`lifecycle_status` and `distributor_count` are the reason this script exists. An
obsolete or single-distributor candidate is a bad substitution regardless of price, and
these two fields are what surface that.

Notes: check `cache/nexar/{mpn}.json` before every request. Because S7 depends on S9's
ranking, the pipeline runs S9 twice — once on unenriched candidates to rank them, then
again after enrichment to produce final scores. This is intentional; the first pass
scores on spec fit alone, the second incorporates lifecycle and availability.

### S8 — `normalize.py`

Unit conversion, range parsing (e.g. `-55°C to +125°C` → two numeric bounds), package
code alignment, and tolerance normalization across all attribute sources.

Outputs: rewritten `data/*_normalized.csv` for profiles, candidates, and commercial parts.

This script is boring and will take longer than expected. It is also the difference
between a working scorer and a broken one.

### S9 — `score.py`

The heart of the product. For each (federal item, candidate) pair, compare every
requirement in the profile against the candidate's normalized attributes.

Per requirement, emit one of: `pass`, `fail`, `marginal`, `unknown`.

Then check QPL status: is the candidate's CAGE listed in `qpl.csv` against the item's
governing specification?

Roll up into a risk classification:

```
high    — any qualification-class requirement fails, OR >2 requirements fail
medium  — 1-2 non-qualification requirements fail, OR >30% unknown
low     — all requirements pass or marginal, and QPL-listed
```

`unknown` must never be silently treated as `pass`. A high proportion of unknowns is
itself a risk signal and must surface in the UI.

Outputs:
- `data/substitution_candidates.csv` — candidate_id, nsn, candidate_mpn, risk_level,
  risk_rank, pass_count, fail_count, marginal_count, unknown_count, qpl_listed,
  estimated_savings, rationale
- `data/spec_deltas.csv` — delta_id, candidate_id, nsn, candidate_mpn, requirement_name,
  required_value, candidate_value, verdict

**`risk_rank`** is an integer 1/2/3 for low/medium/high, emitted alongside `risk_level`.
Foundry sorts on it because alphabetical ordering of the string values produces
`high → low → medium`, which would present the worst candidates first.

**`candidate_id` is a required surrogate primary key**, formatted `{nsn}__{candidate_mpn}`.
Foundry object types accept only a single primary key, so a composite will not work.
`spec_deltas.candidate_id` is the foreign key linking back.

**`estimated_savings`** = `price_history.unit_price_avg` (most recent fiscal year) minus
`commercial_parts.median_price`. Null when either input is missing — the UI distinguishes
null from zero, so do not default it. This is computed here rather than in Foundry so the
candidate table can sort on it server-side.

All attribute values written to `spec_deltas.candidate_value` and
`requirement_profiles.value` must be **pre-normalized to a single unit per attribute**.
Foundry performs no unit conversion; interface comparison depends on this holding.

### S10 — `usaspending_prices.py`

Historical contract pricing per NSN from the USAspending API, for the cost baseline.

Outputs: `data/price_history.csv` — nsn, fiscal_year, unit_price_avg, quantity,
vendor_cage, contract_count.

Notes: no API key required. If an NSN has no history, emit a null row rather than
dropping it — the UI must distinguish "no savings" from "unknown savings."

### S11 — `assembly_gen.py`

Generate the **synthetic** assembly structure. Randomly but deterministically (fixed
seed) assign federal items to slots within a fictional assembly hierarchy, and define
interfaces between slot pairs.

Outputs:
- `data/assemblies.csv` — assembly_id, name, parent_assembly_id
- `data/slots.csv` — slot_id, assembly_id, slot_name, baseline_nsn
- `data/interfaces.csv` — interface_id, slot_a, slot_b, constrained_attribute,
  match_rule

Notes: assembly names must be generic (`power_distribution`, `signal_conditioning`).
Do not model any real weapon system. Define interfaces on only ~6 slots — the ones in
the demo click path.

`constrained_attribute` must exactly match a `requirement_name` that exists in
`requirement_profiles` for the baseline NSN of both slots. Validate this and fail loudly
if it doesn't hold — a dangling attribute reference produces silent non-checking
downstream.

`match_rule` ∈ {`exact`, `numeric_equal`, `numeric_gte`, `numeric_lte`,
`numeric_within_pct:N`}. No other values are legal. Foundry implements exactly this
grammar and nothing more.

## Output contract

The scripts' only deliverable to Foundry is this set of files. Column names are the
contract; do not rename them without updating `FOUNDRY-BUILD.md`.

```
federal_items.csv          assemblies.csv
mcrl.csv                   slots.csv
characteristics.csv        interfaces.csv
qpl.csv                    requirement_profiles.csv
specifications.csv         substitution_candidates.csv
spec_requirements.csv      spec_deltas.csv
commercial_parts.csv       price_history.csv
```

`requirement_profiles.csv` is the table the application reads for part requirements.
`specifications.csv` and `spec_requirements.csv` are provenance only.

## Out of scope — explicitly

The coding agent must not build any of the following:

- **Any web scraper.** All sources are APIs or manual downloads.
- **A constraint solver or auto-optimizer** for multi-part substitution. Compatibility
  conflicts are surfaced to the user, never resolved automatically.
- **A web frontend.** The UI is Foundry Workshop.
- **Authentication, user accounts, or multi-tenancy.**
- **A database.** Flat files only.
- **Incremental/streaming updates.** The pipeline is a batch run.
- **Retry/backoff sophistication beyond a simple bounded retry.**
- **Test coverage beyond smoke tests on the scoring logic.** `score.py` and
  `normalize.py` get tests; nothing else does.
- **Mechanical FSC support** unless electronics is complete and working first.

## Open questions

- Nexar counts *distinct* MPNs over account lifetime vs. per-query matches — verify
  with a duplicate query before the first real run (see Quota discipline).
- QPL Access file schema — unknown until one is opened.
- Digi-Key parametric coverage within the chosen FSCs — may be thin; test early with
  five real NSNs before committing to a category mapping.
- Whether PUB LOG characteristics are populated densely enough to drive matching, or
  whether spec extraction has to carry most of the requirement profile.

## Build order

Each step must run end-to-end before the next begins.

1. **S1** with a hand-written 5-NSN sample. Confirm PUB LOG parses.
2. **S11** with fake data. Assembly structure exists.
3. **S6** with hardcoded search params for one NSN. Confirm Digi-Key returns candidates.
4. **S9** with a hand-written requirement profile. Confirm scoring produces sensible
   verdicts. *At this point a thin slice exists — do not proceed until it does.*
5. **S8** normalization, applied to real attribute data.
6. **S5** + **S2** — real profiles and QPL status.
7. **S3** + **S4** — spec extraction. Highest complexity, lowest certainty; it goes here
   because everything above must work without it.
8. **S10** — price history. Must run before S9's final pass, because `estimated_savings`
   is computed during scoring. Costs no quota.
9. **S7** — Nexar enrichment, then re-run **S9** for final scores and savings. Late
   deliberately: quota is unrecoverable, so it is spent only once candidate ranking has
   stopped changing. Verify the counting basis before the first real run.

If time runs short, cut in reverse order: S3/S4, then S2, then S10. Note that cutting
S10 leaves `estimated_savings` null throughout, which the UI handles — it falls back to
sorting candidates by commercial price. Steps 1–6 plus S9 are the minimum viable
pipeline.
