# Part Substitution Data Pipeline

## What this is

Python scripts, run outside Palantir Foundry on a laptop, that assemble datasets for a
defense part-substitution application. They pull from government catalogs (PUB LOG,
QPL, ASSIST, USAspending) and commercial distributor APIs (Digi-Key, Nexar/Octopart),
join them, score candidate substitutions against qualification requirements, and emit
flat files that get uploaded into Foundry. Hackathon project, team of four, ~2 days.
Foundry handles the ontology and UI; this repo handles everything before that.

## Stack

- Python 3.11+, standard scripts run from the command line
- pandas for tabular work, requests for HTTP
- `mdbtools` (system dependency) for converting Access `.mdb` qualification files
- Output: CSV to `data/`
- No database, no web server, no framework

## Commands

[Fill in once scripts exist — expect `python scripts/sNN_name.py` per step]

## Project structure

```
config/     hand-authored inputs (target NSNs, FSC category map, overrides)
scripts/    S1-S11, one file per pipeline step
data/       generated outputs — the Foundry upload contract
cache/      raw API responses, never deleted, never re-fetched
```

## Spec

- The approved PRD is at `PRD.md`. Treat it as the source of truth.
- The Foundry-side build is specified in `FOUNDRY-BUILD.md` — read it for the output
  contract, but do not implement anything from it here.
- Build against the PRD. Flag gaps or ambiguities rather than filling them in
  unilaterally.
- After changes that affect scope or behavior, flag what in the PRD needs updating —
  don't edit it without my go-ahead.

## Conventions

- **Every script is independently runnable and idempotent.** Read from `data/`, write
  to `data/`. No orchestration layer.
- **Cache every API response to `cache/` on arrival, keyed by request hash.** Check the
  cache before every request. A re-run must cost zero quota.
- **Nexar meters matched parts, not calls: 300 for the entire project lifetime.**
  Batching does not help. Enforce a persisted counter in `cache/nexar_usage.json` that
  **raises** when the ceiling is hit. Never write code that could loop over Nexar
  requests without a hard bound.
- **Nexar is for enrichment only, never discovery.** Digi-Key parametric search finds
  candidates; Nexar enriches the top 3 per federal item. Never pipe an unfiltered
  Digi-Key result set into Nexar — that consumes the entire budget in one run.
- **No scrapers.** Every source is an API or a manually-downloaded file. If a source
  seems to require scraping, stop and flag it rather than writing one.
- Polite HTTP for government sources: 1 req/sec, descriptive User-Agent with a contact
  address.
- `unknown` is a first-class verdict in scoring. Never coerce it to `pass`.
- Log dropped rows with a reason. Silent data loss in a join is the failure mode that
  will cost the most debugging time.
- Ask before adding dependencies.

## Secrets

- Never read `.env.local` or any `.env.*` file with real values.
- Refer to `.env.example` for required environment variables.
- If you need an env var's value, ask me.

## Definition of done

- Script runs end to end from a clean `data/` directory without manual intervention.
- Row counts logged at every stage, with dropped-row reasons.
- `score.py` and `normalize.py` have smoke tests. Nothing else needs tests.
