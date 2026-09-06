#!/usr/bin/env python
"""
S3 — assist_fetch.py

Fetch the governing specification PDFs for the federal items and index them for S4.

Inputs:  data/federal_items.csv        (S1) distinct governing_spec_ref values -> which specs are needed
         config/spec_sources.csv      spec_id, source_url, notes — explicit direct PDF URLs, hand-listed
         data/specs/*.pdf              PDFs dropped in by hand are indexed too
Outputs: data/specs/{spec_id}.pdf
         data/spec_index.csv  spec_id, spec_key, governing_spec_refs, nsn_count, pdf_path, source_url,
                              status, fetched_at, size_bytes, sha256
         status ∈ downloaded | cached | manual | missing_source | download_failed | not_a_pdf

Why this does not "search ASSIST" (PRD deviation, flagged in PROGRESS.md):
  ASSIST / DLA QuickSearch (assist.dla.mil, quicksearch.dla.mil) publish no API. A document's PDF
  link is generated dynamically behind an HTML search page, so obtaining it programmatically means
  parsing that HTML — a scraper, which CLAUDE.md forbids. The team therefore finds each spec once
  on QuickSearch by document number, and either pastes the direct PDF URL into
  config/spec_sources.csv or saves the PDF as data/specs/{spec_id}.pdf. This script does the
  polite, cached, never-repeated download for anything with a URL and tells you exactly which
  specs still have no source.

Polite fetching: 1 request/second, descriptive User-Agent with PIPELINE_CONTACT_EMAIL, bounded
retry, a file is never re-downloaded if it already exists (use --force to override for one spec).

Join key: spec_key() — the 3–6 digit document number shared by a PUB LOG reference part number
(M55342K06B10E0R -> 55342) and a spec id (MIL-PRF-55342 -> 55342). The same key S5 uses. Reference
numbers with no 3+ digit run (e.g. RWR80S1R00FR) cannot be keyed and are listed as unkeyable.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CONFIG_DIR, CachedSession, DATA_DIR, DroppedRows, ensure_dirs, log_rows, read_csv_str, read_data_csv,
    setup_logging, spec_key, write_data_csv,
)

SCRIPT = "s03_assist_fetch"
SPECS_DIR = DATA_DIR / "specs"
INDEX_COLS = ["spec_id", "spec_key", "governing_spec_refs", "nsn_count", "pdf_path", "source_url", "status",
              "fetched_at", "size_bytes", "sha256"]
QUICKSEARCH_HINT = "https://quicksearch.dla.mil/  (search by document number, then paste the PDF URL into config/spec_sources.csv)"


def safe_spec_filename(spec_id: str) -> str:
    """'MIL-PRF-55342/6' -> 'MIL-PRF-55342_6.pdf'"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", spec_id.strip()).strip("_") + ".pdf"


def is_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def rel(path: Path) -> str:
    """Repo-relative path for the index (falls back to absolute if the PDF lives elsewhere)."""
    try:
        return str(path.relative_to(DATA_DIR.parent))
    except ValueError:
        return str(path)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sources(logger) -> pd.DataFrame:
    path = CONFIG_DIR / "spec_sources.csv"
    if not path.exists():
        logger.warning("config/spec_sources.csv not found — only hand-dropped PDFs in data/specs/ will be indexed")
        return pd.DataFrame(columns=["spec_id", "source_url", "notes"])
    df = read_csv_str(path)
    for c in ("spec_id", "source_url", "notes"):
        if c not in df.columns:
            df[c] = ""
    df["spec_id"] = df["spec_id"].str.strip()
    df["source_url"] = df["source_url"].str.strip()
    df = df[df["spec_id"] != ""]
    log_rows(logger, "spec_sources.csv rows", len(df))
    return df


def needed_specs(items: pd.DataFrame | None, logger) -> tuple[dict[str, dict], list[str]]:
    """spec_key -> {refs: set, nsns: set} from federal_items; plus the list of unkeyable refs."""
    need: dict[str, dict] = {}
    unkeyable: list[str] = []
    if items is None:
        return need, unkeyable
    for nsn, ref in zip(items["nsn"], items["governing_spec_ref"]):
        if not ref:
            continue
        k = spec_key(ref)
        if not k:
            unkeyable.append(ref)
            continue
        slot = need.setdefault(k, {"refs": set(), "nsns": set()})
        slot["refs"].add(ref)
        slot["nsns"].add(nsn)
    return need, unkeyable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", help="only this spec_id")
    ap.add_argument("--force", action="store_true", help="re-download the selected spec(s) even if the PDF exists")
    args = ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    ensure_dirs(SPECS_DIR)
    session = CachedSession("assist", logger, min_interval_s=1.0)

    items = read_data_csv("federal_items.csv", required=False, logger=logger)
    need, unkeyable = needed_specs(items, logger)
    sources = load_sources(logger)
    if args.spec:
        sources = sources[sources["spec_id"] == args.spec]

    if unkeyable:
        logger.warning("%d governing_spec_ref value(s) have no 3+ digit document number and cannot be joined to a "
                       "spec (S5 has the same limitation): %s", len(unkeyable), ", ".join(sorted(set(unkeyable))[:10]))

    rows: list[dict] = []
    seen_keys: set[str] = set()
    fetched_live = 0

    # ---- 1. specs with an explicit source
    for _, s in sources.iterrows():
        spec_id, url = s["spec_id"], s["source_url"]
        k = spec_key(spec_id)
        seen_keys.add(k)
        dest = SPECS_DIR / safe_spec_filename(spec_id)
        status, fetched_at = "", ""
        if args.force and dest.exists() and (not args.spec or args.spec == spec_id):
            dest.unlink()
        if dest.exists() and dest.stat().st_size > 0:
            status = "cached"
        elif not url:
            status = "missing_source"
        else:
            try:
                live = session.download(url, dest)
                fetched_live += int(live)
                fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                status = "downloaded"
                logger.info("downloaded %s -> %s (%.0f KB)", spec_id, dest.name, dest.stat().st_size / 1e3)
            except RuntimeError as exc:
                status = "download_failed"
                logger.error("%s: %s", spec_id, exc)
        if status in ("downloaded", "cached") and not is_pdf(dest):
            # an HTML login/consent page saved as .pdf would poison S4; remove it and say so
            dest.unlink()
            status = "not_a_pdf"
            logger.error("%s: %s is not a PDF (probably an HTML page) — removed; check the URL", spec_id, dest.name)
        slot = need.get(k, {"refs": set(), "nsns": set()})
        rows.append({
            "spec_id": spec_id, "spec_key": k, "governing_spec_refs": ";".join(sorted(slot["refs"])),
            "nsn_count": str(len(slot["nsns"])), "pdf_path": rel(dest) if dest.exists() else "",
            "source_url": url, "status": status, "fetched_at": fetched_at,
            "size_bytes": str(dest.stat().st_size) if dest.exists() else "",
            "sha256": sha256_of(dest) if dest.exists() else "",
        })
        if k and k not in need and items is not None:
            logger.info("%s is listed in spec_sources.csv but no federal item references document %s", spec_id, k)

    # ---- 2. hand-dropped PDFs not covered above
    listed_files = {safe_spec_filename(s) for s in sources["spec_id"]}
    for pdf in sorted(SPECS_DIR.glob("*.pdf")):
        if pdf.name in listed_files or (args.spec and pdf.stem != re.sub(r"[^A-Za-z0-9._-]+", "_", args.spec)):
            continue
        spec_id = pdf.stem
        k = spec_key(spec_id)
        seen_keys.add(k)
        if not is_pdf(pdf):
            dropped.drop("file in data/specs is not a PDF", pd.DataFrame([{"file": pdf.name}]), "manual")
            continue
        slot = need.get(k, {"refs": set(), "nsns": set()})
        rows.append({
            "spec_id": spec_id, "spec_key": k, "governing_spec_refs": ";".join(sorted(slot["refs"])),
            "nsn_count": str(len(slot["nsns"])), "pdf_path": rel(pdf), "source_url": "",
            "status": "manual", "fetched_at": "", "size_bytes": str(pdf.stat().st_size), "sha256": sha256_of(pdf),
        })

    # ---- 3. specs the items need but nothing supplies
    for k, slot in sorted(need.items()):
        if k in seen_keys:
            continue
        rows.append({
            "spec_id": "", "spec_key": k, "governing_spec_refs": ";".join(sorted(slot["refs"])),
            "nsn_count": str(len(slot["nsns"])), "pdf_path": "", "source_url": "", "status": "missing_source",
            "fetched_at": "", "size_bytes": "", "sha256": "",
        })

    idx = pd.DataFrame(rows, columns=INDEX_COLS).sort_values(["status", "spec_key", "spec_id"])
    write_data_csv(idx, "spec_index.csv", INDEX_COLS, logger)

    have = idx[idx["status"].isin(["downloaded", "cached", "manual"])]
    missing = idx[idx["status"] == "missing_source"]
    logger.info("specs with a PDF: %d (%d fetched live this run); failed: %d; needed but no source: %d",
                len(have), fetched_live, int(idx["status"].isin(["download_failed", "not_a_pdf"]).sum()), len(missing))
    if len(missing):
        logger.warning("TO DO BY HAND — find these on %s", QUICKSEARCH_HINT)
        for _, m in missing.iterrows():
            logger.warning("  document %-8s referenced by %s item(s) as %s%s", m["spec_key"], m["nsn_count"],
                           m["governing_spec_refs"], f"  (spec_id {m['spec_id']} has no URL)" if m["spec_id"] else "")
    covered_nsns = sum(len(need[k]["nsns"]) for k in need if k in set(have["spec_key"]))
    total_nsns = sum(len(v["nsns"]) for v in need.values())
    if total_nsns:
        logger.info("items whose governing spec PDF is available: %d of %d with a keyable spec reference",
                    covered_nsns, total_nsns)
    logger.info(session.summary())
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
