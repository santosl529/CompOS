# Foundry Build Checklist

Everything to do **inside** Foundry once the scripts in `PRD.md` have produced their
output files. Ordered so that something demoable exists as early as possible.

Owner note: this is one person's job for the whole event (CS #2 in the team split).
Do not split Foundry work across two people — the coordination cost exceeds the benefit
at this scale.

---

## Phase 0 — Before any real data (first 60–90 minutes)

- [ ] **Run a Workshop tutorial end to end.** Not the project — a throwaway. Workshop
      has its own idioms and discovering them at 11pm is the standard way teams lose.
- [ ] Create the project folder and set permissions for all four teammates.
- [ ] Upload **fake CSVs** matching the output contract in `PRD.md` — 5 rows each,
      correct column names, plausible values. Everything downstream gets built against
      these, so the app exists before the pipeline does.
- [ ] Confirm you can create an Object Type from an uploaded dataset and see instances
      in Object Explorer. This is the "is Foundry working" smoke test.

---

## Phase 1 — Datasets and pipeline

- [ ] Upload all output-contract files as datasets.
- [ ] In **Pipeline Builder**, one transform per dataset: type coercion, null handling,
      primary key enforcement. Nothing clever.
- [ ] Verify primary keys are unique — `nsn` on federal items, `slot_id` on slots,
      composite `(nsn, candidate_mpn)` on substitution candidates. Foundry will let you
      create an object type on a non-unique key and then behave strangely.
- [ ] Do **not** build transformation logic here that the scripts already do. If
      something is wrong, fix it upstream and re-upload. Two places doing normalization
      is how you lose an afternoon.

---

## Phase 2 — Ontology: object types

Create these object types, each backed by its dataset.

| Object type | Backing dataset | Primary key | Key properties |
|---|---|---|---|
| `FederalItem` | federal_items.csv | nsn | fsc, item_name, governing_spec_ref, cage_code |
| `Requirement` | requirement_profiles.csv | synthetic id | nsn, requirement_name, operator, value, uom, requirement_class, source |
| `Specification` | spec_requirements.csv | spec_id | title, requirement_count |
| `CommercialPart` | commercial_parts.csv | mpn | manufacturer, lifecycle_status, median_price, stock_qty, lead_time_days, datasheet_url |
| `SubstitutionCandidate` | substitution_candidates.csv | (nsn, candidate_mpn) | risk_level, pass/fail/marginal/unknown counts, qpl_listed, rationale |
| `SpecDelta` | spec_deltas.csv | synthetic id | requirement_name, required_value, candidate_value, verdict |
| `Assembly` | assemblies.csv | assembly_id | name, parent_assembly_id |
| `Slot` | slots.csv | slot_id | assembly_id, slot_name, baseline_nsn |
| `Interface` | interfaces.csv | interface_id | slot_a, slot_b, constrained_attribute, match_rule |
| `PriceHistory` | price_history.csv | synthetic id | nsn, fiscal_year, unit_price_avg, vendor_cage |
| `Configuration` | *created empty* | config_id | name, created_by, created_at |
| `SubstitutionDecision` | *created empty* | decision_id | config_id, slot_id, chosen_mpn, verdict, justification, decided_by, decided_at |

The last two have **no backing file from the scripts** — they are written by user
Actions at runtime. Create them as writeback datasets.

Set display names, title properties, and icons. This takes ten minutes and makes the
demo look twice as finished.

---

## Phase 3 — Ontology: link types

- [ ] `FederalItem` → `Requirement` (one-to-many)
- [ ] `FederalItem` → `Specification` (many-to-one, via governing_spec_ref)
- [ ] `FederalItem` → `SubstitutionCandidate` (one-to-many)
- [ ] `SubstitutionCandidate` → `CommercialPart` (many-to-one, via mpn)
- [ ] `SubstitutionCandidate` → `SpecDelta` (one-to-many)
- [ ] `FederalItem` → `PriceHistory` (one-to-many)
- [ ] `Assembly` → `Slot` (one-to-many)
- [ ] `Assembly` → `Assembly` (self-link, parent/child)
- [ ] `Slot` → `FederalItem` (many-to-one, baseline part)
- [ ] `Slot` ↔ `Interface` (two links: interface_a, interface_b)
- [ ] `Configuration` → `SubstitutionDecision` (one-to-many)
- [ ] `SubstitutionDecision` → `Slot` (many-to-one)

The graph is the demo. Being able to click from an assembly down to a slot, to its
baseline item, to its candidates, to the spec deltas — with no code — is the thing
Foundry does that a React app doesn't. Make sure that traversal works before adding
anything else.

---

## Phase 4 — Functions

Write these as Foundry functions (TypeScript or Python, whichever the tenant supports
more readily).

- [ ] **`resolveCurrentPart(slot, configuration)`** — returns the substituted part if a
      decision exists in this configuration, else the slot's baseline item. Every other
      piece of logic depends on this; write it first and get it right.
- [ ] **`checkInterfaceConflicts(configuration)`** — for every interface, resolve both
      sides via `resolveCurrentPart`, compare the constrained attribute under the match
      rule, return a list of conflicts. Re-run after every decision.
- [ ] **`configurationSummary(configuration)`** — counts of decisions by risk level,
      total estimated savings, open conflict count. Drives the header widget.

Deliberately **not** building: any function that resolves conflicts automatically.
Conflicts are displayed, not fixed.

---

## Phase 5 — Actions

- [ ] **`createConfiguration(name)`** — creates an empty `Configuration`.
- [ ] **`recordSubstitution(config, slot, mpn, verdict, justification)`** — writes a
      `SubstitutionDecision`. `verdict` is one of `approved`, `rejected`,
      `flagged_for_test`. **Justification is a required field** — this is what makes the
      output an audit trail rather than a toy, and it's the detail that lands with
      judges who care about governance.
- [ ] **`revertSubstitution(config, slot)`** — deletes the decision, returning the slot
      to baseline.

Actions are what make this an application instead of a dashboard. If you build nothing
else in this phase, build `recordSubstitution`.

---

## Phase 6 — Workshop app

One page, four regions. Resist adding pages.

**Left — assembly tree.** Object tree widget on `Assembly` → `Slot`. Each slot shows
its current part and a risk badge. Selecting a slot drives everything else.

**Center — candidate table.** Filtered `SubstitutionCandidate` list for the selected
slot's item. Columns: MPN, manufacturer, risk level, fail count, QPL listed, price,
lead time. Sorted by risk then savings. Selecting a row drives the detail panel.

**Right — spec delta panel.** `SpecDelta` list for the selected candidate. One row per
requirement, colour-coded by verdict. This panel is the product; give it the most room
and make sure `unknown` is visually distinct from `pass`.

**Bottom — decision bar.** Buttons wired to `recordSubstitution`, a justification text
input, and a live conflict list from `checkInterfaceConflicts`.

**Header** — configuration selector, `configurationSummary` metrics, and a visible
banner reading *"Synthetic assembly structure. Catalog data from PUB LOG, ASSIST,
Digi-Key, Octopart."* Do not omit the banner; provenance is half the pitch.

---

## Phase 7 — AIP (only if Phases 0–6 are done)

- [ ] **AIP Logic function: substitution rationale.** Given a `SubstitutionCandidate`
      and its deltas, generate a short plain-language justification a procurement
      officer could paste into a memo. Pre-fills the justification field.
- [ ] **AIP agent over the ontology.** "Which slots have low-risk substitutions
      available?" answered by tool-calling against object types. High demo value, but
      genuinely optional.

Do not start Phase 7 before the click path works without it.

---

## Demo-day checklist

- [ ] Full click path rehearsed out loud, timed, at least three times.
- [ ] All data materialized — no live API calls anywhere in the path.
- [ ] A known-good starting configuration saved, and a way to reset to it.
- [ ] One candidate in the demo path that is deliberately **high risk** — showing the
      tool correctly rejecting something is more convincing than showing it approve.
- [ ] The interface-conflict cascade rehearsed specifically. It is the single best
      thirty seconds you have: substitute one part, watch a warning appear on a
      different slot.
- [ ] Answer prepared for "where did this data come from?" — name the sources, note
      what is synthetic, note what you deliberately did not scrape.

---

## Cut order if behind

1. Phase 7 (AIP) — entirely optional
2. `Interface` objects and `checkInterfaceConflicts` — drops the cascade, keeps
   single-part substitution working
3. `PriceHistory` and savings math
4. `Specification` / `Requirement` from spec extraction — fall back to PUB LOG
   characteristics alone

Never cut: `FederalItem`, `SubstitutionCandidate`, `SpecDelta`, `recordSubstitution`,
and the Workshop page. That set is a complete demo on its own.
