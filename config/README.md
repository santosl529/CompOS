# config/ — hand-authored inputs

All files here are read by the scripts; none are generated.

## `target_items.csv` (teammates, from PUB LOG)
Pipeline entry point. `nsn,fsc,item_name,notes`. NSN may be written with or without
dashes (`5905-00-123-4567` or `5905001234567`); scripts normalise to 13 digits.
Until it exists, scripts fall back to `target_items.sample.csv`. **The sample NSNs are
placeholders and will match nothing in PUB LOG** — replace them.

## `fsc_category_map.csv` (mechanical engineer)
`fsc,digikey_category_id,driving_attributes,tolerance_rules`

- `digikey_category_id` — one or more Digi-Key v4 **leaf** category ids, `|`-separated.
  Verify with `python scripts/s06_digikey_search.py --list-categories [--grep text]` (writes
  the full tree to `cache/digikey/categories.json`). Leaves are required: parent categories
  are accepted by the API but return no parametric filters, so nothing narrows the search.
  When several leaves are listed S6 uses the one whose name shares a token with the item
  name ("...Backshell" → 313 Backshells and Cable Clamps) and otherwise searches them all.
  Ids resolved against the live tree 2026-09-05:
  5905 → 53 Through Hole | 54 Chassis Mount | 52 Chip SMD resistors;
  5910 → 60 Ceramic | 62 Film capacitors; 5915 → 838 Power Line Filter Modules | 835 EMI/RFI
  Filters | 845 Feed Through Capacitors; 5930 → 201 Toggle Switches (pressure switches have
  no Digi-Key home — expect poor candidates); 5935 → 436 Assemblies | 320 Housings | 313
  Backshells | 378 Adapters | 329 Accessories (all Circular Connectors); 5945 → 189 Signal |
  188 Power relays; 5950 → 71 Fixed Inductors | 166 Pulse Transformers; 5961 → 280
  Rectifiers > Single Diodes; 5999 → 332 D-Sub Contacts | 219 Heat Sinks | 945 EMI Gaskets |
  869 Shielding Materials | 226 Liquid Cooling (FSC is a catch-all; per-item leaf pick).
  Leave the cell blank to search the whole catalogue by item name with no parametric step.
- `driving_attributes` — `;`- or `|`-separated canonical attribute names (see
  `attribute_aliases.csv`) used to build the parametric search and to rank closeness.
- `tolerance_rules` — `;`- or `|`-separated `attr:rule[:arg]`:
  - `attr:pct:N` (alias `attr:within_pct:N`) — candidate within ±N % of the required value
  - `attr:gte` — candidate value must be ≥ required (ratings: power, voltage, temp max)
  - `attr:lte` — candidate value must be ≤ required (tolerance, temp min)
  - `attr:exact` — string equality after normalisation (package, dielectric)

## `attribute_aliases.csv`
`source,raw_name,canonical_name`. Maps attribute names as they appear in each source
(`publog` requirement statements, `digikey` parameter names, `spec` extracted names) to
one canonical vocabulary. Matching is case-insensitive and ignores punctuation. Anything
not in this file is carried through under a slugified raw name and is logged as
unaliased — it can still be displayed but will never score against anything.

## `spec_requirements_overrides.csv`
Same columns as `data/spec_requirements.csv`. Applied after LLM extraction in S4 **and again by S5**
when it reads `spec_requirements.csv` (same function, so a correction takes effect on the next S5 run
without re-extracting): a row here **replaces** any extracted row with the same
`(spec_id, requirement_name)`, or is appended if none exists. Set `value` to `DELETE` to remove an
extracted row. Rows become `source=manual_override` in the profile and always win the merge.

Current rows correct the rating direction of extracted operators (a tolerance, tempco or dissipation
factor is a ceiling → `lte`; a voltage/current rating or insulation resistance is a floor → `gte`).
S8 warns when a profile operator contradicts `DEFAULT_OPERATORS`; add a row here rather than editing
`data/spec_requirements.csv`.

## `spec_sources.csv`
`spec_id,source_url,notes`. S3 only downloads from explicit direct URLs listed here
(polite: 1 req/s, descriptive User-Agent). It does not search ASSIST — ASSIST has no
public API and QuickSearch would require HTML scraping, which this project does not
do. PDFs can also be dropped manually into `data/specs/{spec_id}.pdf`; S3 indexes them.

## `publog_columns.json`
Column-name mapping for the PUB LOG CSV files. Defaults are best guesses from the
public record layouts; run `python scripts/s01_publog_ingest.py --print-headers` on the
real files and correct this file if S1 reports a missing column.

## `qpl_columns.json`
Heuristics for mapping tables/columns inside the QPL Access databases to the
`qpl.csv` contract. The Access schema was unknown when this was written; S2 dumps every
table to `cache/qpl/` so the mapping can be fixed after inspecting one.
