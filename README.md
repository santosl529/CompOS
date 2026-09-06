# Substitution Analysis Tool — full description

What it is, how it works, and why it's built the way it is.

---

## The problem

Defense procurement buys parts by National Stock Number. Many of those parts
cost 50x their commercial equivalent, and the obvious explanation — vendor
markup — is wrong. They cost more because of qualification: −55°C to +125°C
operating range, vibration and thermal shock testing, lot traceability under
counterfeit-avoidance rules, listing on a Qualified Products List, guaranteed
long-term availability.

So the naive tool — "find a cheaper part with the same specs" — makes a claim
any procurement officer knows is false. That's why nobody uses them.

The useful question isn't *is there a cheaper part*. It's *what would it
actually take to substitute this one*.

---

## What the application does

A user picks a position in an assembly. The tool shows commercial parts that
could fill it, and for each one reports three separate things:

**Can it replace?** Hard constraints checked as pass/fail, soft attributes
scored into a weighted composite.

**What would it take?** The qualification clauses the governing MIL-SPEC imposes
that no commercial catalog can answer — lot traceability, DPA sampling,
solderability, thermal shock. These need testing, not lookup.

**Can you get it?** Distributor count, stock, lead time. A part that meets every
spec but has one supplier and no stock is a bad substitution.

The user approves, rejects, or flags a candidate with a written justification.
That decision layers over an immutable baseline assembly, and every other part
that has to agree with the one just changed is re-checked immediately.

---

## The core insight

Commercial catalogs know what you can **buy**. Government catalogs know what's
**qualified**. Nobody joins them.

That join is the product. Everything else is scaffolding around it.

---

## Data architecture

```
GOVERNMENT SIDE                  COMMERCIAL SIDE
federal catalog (what it is)     distributor catalogs
MIL-SPEC docs (what it must meet) parametric attributes
qualification lists (who's approved) pricing, stock, lead time
        |                                    |
        +----------------+-------------------+
                         |
                  requirement profile
                  (548 rows, per NSN)
                         |
                    candidates
                  (1,002 scored)
                         |
        +----------------+-------------------+
        |                |                   |
   risk level    qualification gap     supply chain
```

**Eleven Python scripts** run outside Foundry, doing acquisition and scoring.
**Foundry** holds the ontology, the decision layer, and the application.

That split is deliberate, not a compromise. Distributor APIs need outbound
network egress policies; acquisition living outside the platform is what real
deployments look like. Everything downstream — the object model, the decision
state, the conflict logic, the app — is native.

### Object model

```
Assembly ──< Slot ──> FederalItem ──< Requirement
                │                 ──< SubstitutionCandidate ──< SpecDelta
                │                                           ──> CommercialPart
                ├──< SlotConstraint
                │
Configuration ──< SubstitutionDecision ──> Slot
                                       ──> SubstitutionCandidate
```

Three functional assemblies — interconnect, power distribution, signal
conditioning — holding 41 slots, with six interface constraints between them.

---

## The scoring model

### Stage 1 — hard gates

Binary disqualifiers, evaluated first. Any violation ends the analysis: risk is
high, composite locks at 100.

Universal: the candidate's temperature range must fully contain the required
range; voltage rating must meet or exceed.

Per supply class: resistance within tolerance band, contact count exact, shell
size exact, coupling type exact, contact configuration exact, sealing meets or
exceeds — depending on what the part is.

**Missing data is not a failure.** If a candidate has no value for a gated
attribute, the gate is unverified and passes through. Only an actual violation
fails. Treating absence as failure would eliminate nearly everything and tell
you nothing about why.

### Stage 2 — weighted composite

Survivors get scored:

```
Universal pool   60 pts    lead time 38, cost vs historical 22
Category pool    40 pts    per supply class
```

Four formula shapes: lower-is-better (contact resistance, dissipation factor),
higher-is-better (mechanical life, Q factor), closer-to-original (tolerances),
and categorical lookup (plating: gold 0, silver 20, tin 45, unknown 80).

**Missing attributes are excluded and remaining weights renormalized.** The
resulting coverage percentage is reported alongside the score, because a
composite of 22 computed on 40% of the model is a different claim than one on
90%.

### Stage 3 — bands

```
0–33 low  ·  34–66 medium  ·  67–100 high  ·  null = unscored
```

### Current distribution

| Band | Count |
|---|---|
| High | 197 — 188 failed a hard gate, 9 scored 100 on the composite |
| Medium | 151 |
| Low | 307 |
| Unscored | 347 |

---

## The qualification burden

The part that makes this different from a distributor search.

Some requirements can't be checked against a part at all. Lot traceability isn't
an attribute of a connector — it's a property of how the manufacturer runs their
production line. No catalog publishes it, because it isn't a fact about the
object.

Six such clauses appear in the data: QPL listing, lot traceability, DPA
sampling, solderability per J-STD-002, thermal shock cycles, vibration, humidity
resistance.

**They're excluded from risk scoring entirely.** 5,733 delta rows, all
permanently unknown, none of them failures. They surface as a gap count and a
clause list.

### Why this was the pivotal decision

The first version treated a qualification failure as automatic high risk. Since
essentially no commercial part is on a military QPL, all 956 candidates failed —
940 high, zero low. Technically correct and completely useless.

Separating burden from risk changed the output from a verdict you can't support
into a work estimate you can:

> Meets 5 of 10 measurable requirements; fails temperature coefficient.
> Five qualification clauses under MIL-PRF-39007 require testing to verify.

"Fails qualification" is a claim distributor data cannot establish. "Requires
testing to verify" is exactly what the data supports.

The gap count is constant across candidates for a given part, because it's a
property of the governing specification. That's correct: it prices the
*substitution*, while risk ranks the *candidates*.

---

## Slots, not parts

A slot is a **position** in the assembly. The connector currently in it is a
part.

```
current part in a slot =
    the approved substitution in the active configuration,
    else the slot's baseline part
```

That single indirection means the baseline assembly is never mutated. Revert is
free, baseline-vs-modified comparison is free, and audit history is free.

Slots use reference designators — J1, R5, C2, FL1, K1 — the way an engineer
would name positions on a drawing.

---

## The interface cascade

Six constraints define which slots must agree with each other and on what:

```
J1  contact_count      exact        J10
J1  shell_material     exact        J2
J1  contact_plating    exact        J3
E1  operating_temp_max ≥            J1
C1  voltage_rating     ≥            FL1
R1  operating_temp_max ≥            L1
```

Comparison is directional — the left slot is the left operand — because
greater-than and within-percent constraints are asymmetric and evaluating them
backwards produces plausible-looking wrong answers.

**Every constraint is re-evaluated across the whole configuration after every
decision**, not just the ones touching the changed slot. Compatibility is
order-dependent — substituting A then B can succeed where the reverse fails —
and full re-evaluation avoids phantom blocking when a user backtracks.

**Conflicts are reported, never resolved.** A conflicting choice isn't blocked;
it's flagged, and the human decides. Automatic resolution would be a genuine
constraint-satisfaction problem and would produce recommendations nobody could
audit.

Four of the six constraints touch J1. Substituting there re-evaluates all six
and surfaces findings on three — J2, J3 and E1. J10 passes, because the
replacement happens to have the same contact count. That's the check working
rather than a blanket alarm.

---

## Decision model

```
SubstitutionDecision
  decision_id = {config}__{slot}__{candidate}   deterministic
  verdict     approved | rejected | flagged_for_test
  is_current  true only for the active approval
  justification, decided_by, decided_at, superseded_at
```

Approving supersedes any prior approval for that slot atomically —
`is_current` false, `superseded_at` stamped. Rejections and flags are never
current.

**Nothing is ever deleted.** Reverting sets `is_current` false rather than
removing the record.

**Justification is required with no default.** An approval without a stated
reason isn't a procurement decision, it's a click.

The action validates that the candidate's NSN matches the slot's baseline before
writing, so a valid part number belonging to a different federal item can't be
recorded against the wrong slot.

---

## Interface design decisions

**Three claims, never one number.** Risk, burden, and sourcing are displayed
separately. Collapsing them into a single score is exactly what makes existing
tools useless.

**Absence is displayed as itself.** Four distinct states, none of them blank and
none of them zero:

| State | Meaning |
|---|---|
| unknown | requirement exists, candidate has no value |
| unscored | no scoreable soft attributes for this item type |
| not enriched | no sourcing data for this part |
| unverified | a hard gate exists but couldn't be checked |

A blank cell reads as breakage. A zero reads as a measurement. Both are lies
when the truth is "we don't know."

**Score is always paired with coverage.** A risk badge on its own overstates
confidence. The header reads "Risk low (composite 0.0, scored on 27% of model)."

**Suppressed rather than shown-as-unknown:** lifecycle status (uniformly unknown
— our Octopart tier lacks the field), country of origin (not yet populated), QPL
listing (uniformly false on candidates). A column of identical unknowns invites
the inference that something was assessed when it wasn't.

**Unknown uses warning semantics, never neutral or success.** An unverifiable
requirement is a risk, not a pass.

---

## What we deliberately don't do

Named boundaries, not oversights.

**Export control classification.** A low-risk substitution here has not been
assessed for ITAR or EAR. The synthetic assembly handles the larger exposure —
no real bill of materials exists — but part-level classification is out of scope.

**Country of origin and Buy American / TAA compliance.** Not modelled. In a real
deployment this is a hard gate that runs *before* technical comparison.

**Automatic conflict resolution.** Surfaced, never solved.

**Certifying substitutions.** The tool produces a ranked shortlist and a
qualification work estimate. A human decides what to test. A tool claiming to
certify would be wrong.

---

## What's real and what's synthetic

**Real:** 41 NSNs across 9 federal supply classes with real item names, part
numbers and CAGE codes. MIL-SPEC document numbers and their correct
part-type pairings. 1,002 candidates from live Digi-Key parametric search.
Sourcing data for 60 parts from live Octopart queries. The qualification
practices themselves — J-STD-002, DPA, lot traceability, MIL-STD-202 test
methods are all real requirements.

**Synthetic:** the specific requirement values, the assembly structure
(deliberately — a real weapon system BOM is an ITAR problem we chose not to
have), and the QPL entries.

**Why:** PUB LOG ships as a proprietary binary format with no macOS reader, and
ASSIST has no search API — retrieving spec documents programmatically would have
meant scraping a site that publishes them for human download, which we wouldn't
do. We built the analysis layer correctly on synthetic requirements rather than
lose a day to extraction. Substitute real extracted requirements and nothing
downstream changes.

**We scraped nothing that prohibits it.** McMaster-Carr, Grainger and DLA's
qualification portal all forbid automated access. We used APIs where they exist,
downloaded manually where they don't, and did without where neither was
possible.

---

## Known limitations

**Coverage is thin, and that's the finding.** Roughly 40% of what a MIL-SPEC
demands isn't published in any commercial catalog — insulation resistance,
contact resistance, dielectric withstanding voltage, coupling type. Not a gap in
the tool; the actual state of commercial parts data.

**Sourcing covers 60 of 667 distinct parts.** Octopart's free tier meters 300
matched parts for the account lifetime. We enriched the demo path deliberately
and left the long tail unenriched, displayed as "not enriched."

**No obsolescence signal.** Lifecycle status needs a paid tier. Distributor
count substitutes — 16 single-distributor parts, 10 with zero stock, which is
arguably the stronger supply risk finding anyway.

**No government price baseline.** USAspending has no quantity or unit-price
fields, and DLA's own purchase records don't carry NSNs searchably. Note that a
contract unit price and a 1,000-unit distributor break aren't comparable
regardless — calling that difference a "saving" would mostly measure quantity.

**Thresholds are placeholders.** The gate rules and band boundaries are a
designed framework, not a derived one. A real deployment would calibrate against
historical substitution outcomes, or let each program office set its own
tolerance. The *structure* — hard gates before soft scoring, unknown never
counting as pass, qualification separated from technical fit — is what we'd
defend.

**Seven slots have no surviving candidates.** All four switches, the relay, two
coils. Every commercial option fails on temperature or sealing. That's a real
result: for those parts, no commercial substitution exists at this requirement
level.

---

## Engineering decisions worth naming

**Unknown never becomes pass.** The single rule everything else was built
around. Absence of evidence is not evidence of compliance.

**Normalization happens in exactly one place.** Two places doing unit conversion
means two places that can disagree with no way to tell which produced the wrong
number — and unit mismatch doesn't raise an error, it returns a confident wrong
answer. We hit this: 10000 pF was briefly read as 10000 farads.

**Sort on rank, never on label.** Alphabetical ordering of "high / low / medium"
puts the worst candidates first while looking entirely correct. An integer rank
column exists for this reason alone.

**Surrogate keys over composite.** Ontology object types take a single primary
key; the natural key here is (NSN, part number). The composite would have made
the candidate-to-delta link unreliable.

**Confidence floor on requirement precedence.** Where the specification and the
catalog disagree about a requirement, the specification wins — but only above
0.70 extraction confidence. Below that the catalog wins, and the losing value is
stored rather than discarded, so source disagreement stays queryable.

**The FSC-to-category map is hand-authored.** No published crosswalk exists
between federal supply classes and distributor categories. Choosing which 3–5
attributes actually determine whether a substitution works is engineering
judgment, and it's the input with no automated substitute.

---

## Numbers

| | |
|---|---|
| Federal items | 41 across 9 supply classes |
| Requirement profiles | 548 |
| Candidates | 1,002 |
| Spec deltas | 13,676 — 7,943 scored, 5,733 burden |
| Hard gate failures | 188 |
| Top eliminating gate | operating temperature |
| Qualification gap | 5–7 clauses per part |
| Sourced | 60 parts, 100 candidate rows |
| Single-distributor | 16 |
| Zero stocked | 10 |
| Assemblies / slots / constraints | 3 / 41 / 6 |

---

## If it were carried further

**Real requirement extraction.** The pipeline for it is built — ASSIST download
and LLM extraction with confidence scoring and page-level provenance. It needs
spec PDFs, which need a source that permits automated retrieval.

**Country of origin as a hard gate.** CAGE to NCAGE lookup, or SAM.gov entity
records. This runs before technical comparison in the real workflow.

**Real QPL data** from DLA's qualification databases, which would let the
baseline side of the comparison carry genuine qualification provenance.

**Calibrated thresholds.** Every number in the scoring model is a placeholder.
Historical substitution outcomes would turn the framework into a model.

**Obsolescence.** A paid distributor tier gives lifecycle status, which turns
"this part is end-of-life and single-sourced" into a finding the tool can make
on its own.
