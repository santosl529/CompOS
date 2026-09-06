#!/usr/bin/env python
"""
S1 — publog_ingest.py

Parse the PUB LOG distribution (downloaded manually from the FLIS Data Electronic
Reading Room; CSV format since 1 Apr 2023) and filter to the NSNs in
config/target_items.csv.

Inputs (manual download, default location raw/publog/, override with PUBLOG_DIR):
  Identification.zip  -> P_FLIS_NSN.CSV          (NIIN, FSC, ITEM_NAME, ...)
  Reference.zip       -> V_FLIS_PART.CSV         (NIIN, PART_NUMBER, CAGE_CODE, RNCC, RNVC, ...)
  Characteristics.zip -> V_CHARACTERISTICS.CSV   (NIIN, MRC, REQUIREMENTS_STATEMENT, CLEAR_TEXT_REPLY)
Column names are configured in config/publog_columns.json (they are best guesses until
verified against a real download — use --print-headers).

Outputs:
  data/federal_items.csv   nsn, fsc, item_name, governing_spec_ref, cage_code
  data/mcrl.csv            nsn, reference_part_number, cage_code (+ rncc, rnvc extras)
  data/characteristics.csv nsn, attribute_name, attribute_value, uom (+ mrc extra)

The files are multi-GB, so they are streamed in chunks and filtered on NIIN before
anything else is touched. Only rows for target NSNs are ever cleaned.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CONFIG_DIR, RAW_DIR, DroppedRows, env, load_target_items, log_rows, niin_from_nsn,
    read_csv_str, setup_logging, write_data_csv,
)

SCRIPT = "s01_publog_ingest"
CHUNK = 250_000

FEDERAL_ITEMS_COLS = ["nsn", "fsc", "item_name", "governing_spec_ref", "cage_code"]
MCRL_COLS = ["nsn", "reference_part_number", "cage_code"]
CHAR_COLS = ["nsn", "attribute_name", "attribute_value", "uom"]


def load_layout() -> dict:
    path = CONFIG_DIR / "publog_columns.json"
    if not path.exists():
        raise SystemExit("config/publog_columns.json is required.")
    return json.loads(path.read_text())


def find_file(publog_dir: Path, filename: str) -> Path | None:
    """Case-insensitive recursive search for filename under publog_dir."""
    target = filename.lower()
    if not publog_dir.exists():
        return None
    for p in publog_dir.rglob("*"):
        if p.is_file() and p.name.lower() == target:
            return p
    # PublogViewer notes some distributions ship split files like V_CHARACTERISTICS-2.CSV
    stem = Path(target).stem
    for p in publog_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".csv" and p.name.lower().startswith(stem):
            return p
    return None


def read_header(path: Path) -> list[str]:
    df = pd.read_csv(path, nrows=0, dtype=str, encoding="utf-8", encoding_errors="replace")
    return [str(c) for c in df.columns]


def require_columns(path: Path, header: list[str], needed: dict[str, str], logger) -> dict[str, str]:
    """Map logical -> actual column name (case-insensitive). Exit loudly on a miss."""
    lookup = {h.strip().lower(): h for h in header}
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical, configured in needed.items():
        actual = lookup.get(str(configured).lower())
        if actual is None:
            missing.append(f"{logical} -> {configured!r}")
        else:
            resolved[logical] = actual
    if missing:
        logger.error("%s: configured column(s) not found: %s", path.name, "; ".join(missing))
        logger.error("%s actual header: %s", path.name, header)
        raise SystemExit(
            f"Fix config/publog_columns.json for {path.name} (see actual header above), then re-run."
        )
    return resolved


def filtered_read(path: Path, cols: dict[str, str], niin_col: str, niins: set[str], logger) -> pd.DataFrame:
    """Stream the CSV in chunks, keep only rows whose NIIN is in `niins`, rename to logical names."""
    keep_actual = list(cols.values())
    kept: list[pd.DataFrame] = []
    total = 0
    reader = pd.read_csv(
        path, dtype=str, keep_default_na=False, na_filter=False, chunksize=CHUNK,
        usecols=keep_actual, encoding="utf-8", encoding_errors="replace", on_bad_lines="warn",
    )
    for i, chunk in enumerate(reader):
        total += len(chunk)
        niin_series = chunk[niin_col].astype(str).str.strip().str.zfill(9)
        hit = chunk[niin_series.isin(niins)]
        if len(hit):
            hit = hit.copy()
            hit[niin_col] = niin_series[hit.index]
            kept.append(hit)
        if (i + 1) % 10 == 0:
            logger.info("  %s: scanned %d rows, %d matched so far", path.name, total, sum(len(k) for k in kept))
    log_rows(logger, f"{path.name} rows scanned", total)
    out = pd.concat(kept, ignore_index=True) if kept else pd.DataFrame(columns=keep_actual)
    inverse = {actual: logical for logical, actual in cols.items()}
    out = out.rename(columns=inverse)
    for c in out.columns:
        out[c] = out[c].astype(str).str.strip()
    log_rows(logger, f"{path.name} rows for target NIINs", len(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publog-dir", default=None, help="override PUBLOG_DIR (default raw/publog)")
    ap.add_argument("--print-headers", action="store_true", help="print the header of each PUB LOG file and exit")
    args = ap.parse_args()

    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    layout = load_layout()
    publog_dir = Path(args.publog_dir or env("PUBLOG_DIR", str(RAW_DIR / "publog")))
    logger.info("PUB LOG directory: %s", publog_dir)

    files: dict[str, Path] = {}
    for section in ("identification", "reference", "characteristics"):
        f = find_file(publog_dir, layout[section]["file"])
        if f is None:
            msg = (f"{layout[section]['file']} not found under {publog_dir}. Download the PUB LOG "
                   f"Identification/Reference/Characteristics zips from the FLIS Data Electronic Reading Room "
                   f"and extract them there (or set PUBLOG_DIR).")
            logger.error(msg)
            raise SystemExit(2)
        files[section] = f
        logger.info("found %-16s %s (%.1f MB)", section, f, f.stat().st_size / 1e6)

    if args.print_headers:
        for section, f in files.items():
            print(f"\n[{section}] {f}")
            for c in read_header(f):
                print(f"  {c}")
        return 0

    targets = load_target_items(logger, dropped)
    targets["niin"] = targets["nsn"].map(niin_from_nsn)
    niins = set(targets["niin"])
    niin_to_nsn = dict(zip(targets["niin"], targets["nsn"]))

    # ---- identification
    sec = layout["identification"]
    cols = require_columns(files["identification"], read_header(files["identification"]),
                           {k: v for k, v in sec.items() if k != "file"}, logger)
    ident = filtered_read(files["identification"], cols, cols["niin"], niins, logger)
    dupes = ident[ident.duplicated("niin", keep="first")]
    dropped.drop("duplicate NIIN in identification (kept first)", dupes, "identification")
    ident = ident.drop_duplicates("niin", keep="first")

    found = set(ident["niin"])
    not_found = targets[~targets["niin"].isin(found)]
    dropped.drop("target NSN not present in PUB LOG identification", not_found, "identification")

    # ---- reference (MCRL)
    sec = layout["reference"]
    cols = require_columns(files["reference"], read_header(files["reference"]),
                           {k: v for k, v in sec.items() if k != "file"}, logger)
    ref = filtered_read(files["reference"], cols, cols["niin"], niins, logger)
    ref["cage_code"] = ref["cage_code"].str.upper()
    ref["part_number"] = ref["part_number"].str.upper()
    empty_pn = ref[ref["part_number"] == ""]
    dropped.drop("reference row with empty part number", empty_pn, "reference")
    ref = ref[ref["part_number"] != ""]
    ref = ref.drop_duplicates(["niin", "part_number", "cage_code"])

    # ---- characteristics
    sec = layout["characteristics"]
    cols = require_columns(files["characteristics"], read_header(files["characteristics"]),
                           {k: v for k, v in sec.items() if k != "file"}, logger)
    chars = filtered_read(files["characteristics"], cols, cols["niin"], niins, logger)
    empty_reply = chars[(chars["clear_text_reply"] == "") | (chars["requirement_statement"] == "")]
    dropped.drop("characteristic with empty statement or reply", empty_reply, "characteristics")
    chars = chars[(chars["clear_text_reply"] != "") & (chars["requirement_statement"] != "")]

    # ---- derive governing spec + primary CAGE per item
    spec_cages = {k for k in layout.get("spec_cage_codes", {}) if not k.startswith("_")}
    rncc_pri = [str(x) for x in layout.get("primary_reference_rncc", ["3", "5", "2"])]
    rnvc_pri = [str(x) for x in layout.get("primary_reference_rnvc", ["2"])]

    def rank(row: pd.Series) -> tuple[int, int]:
        r1 = rncc_pri.index(row["rncc"]) if row["rncc"] in rncc_pri else len(rncc_pri)
        r2 = rnvc_pri.index(row["rnvc"]) if row["rnvc"] in rnvc_pri else len(rnvc_pri)
        return (r1, r2)

    spec_ref: dict[str, str] = {}
    mfr_cage: dict[str, str] = {}
    multi_spec = 0
    for niin, grp in ref.groupby("niin"):
        grp = grp.copy()
        grp["_rank"] = [rank(r) for _, r in grp.iterrows()]
        grp = grp.sort_values(["_rank", "part_number"])
        specs = grp[grp["cage_code"].isin(spec_cages)]
        mfrs = grp[~grp["cage_code"].isin(spec_cages) & (grp["cage_code"] != "")]
        if len(specs):
            spec_ref[niin] = specs.iloc[0]["part_number"]
            if specs["part_number"].nunique() > 1:
                multi_spec += 1
        if len(mfrs):
            mfr_cage[niin] = mfrs.iloc[0]["cage_code"]
    if multi_spec:
        logger.warning("%d item(s) reference more than one government spec; kept the highest-ranked (RNCC/RNVC) one",
                       multi_spec)

    ident["nsn"] = ident["niin"].map(niin_to_nsn)
    ident["governing_spec_ref"] = ident["niin"].map(spec_ref).fillna("")
    ident["cage_code"] = ident["niin"].map(mfr_cage).fillna("")
    no_spec = ident[ident["governing_spec_ref"] == ""]
    if len(no_spec):
        logger.warning("%d item(s) have no government-spec reference number in MCRL (governing_spec_ref left empty)",
                       len(no_spec))
    # PUB LOG FSC should agree with the target file; prefer PUB LOG, log disagreements
    tgt_fsc = dict(zip(targets["niin"], targets["fsc"]))
    disagree = ident[[tgt_fsc.get(n, "") not in ("", f) for n, f in zip(ident["niin"], ident["fsc"])]]
    if len(disagree):
        logger.warning("%d item(s) where target_items fsc disagrees with PUB LOG fsc — PUB LOG wins", len(disagree))

    federal_items = ident[["nsn", "fsc", "item_name", "governing_spec_ref", "cage_code"]].sort_values("nsn")
    write_data_csv(federal_items, "federal_items.csv", FEDERAL_ITEMS_COLS, logger)

    ref["nsn"] = ref["niin"].map(niin_to_nsn)
    mcrl = ref.rename(columns={"part_number": "reference_part_number"})
    mcrl = mcrl[["nsn", "reference_part_number", "cage_code", "rncc", "rnvc"]].sort_values(["nsn", "rncc", "reference_part_number"])
    write_data_csv(mcrl, "mcrl.csv", MCRL_COLS, logger)

    chars["nsn"] = chars["niin"].map(niin_to_nsn)
    characteristics = pd.DataFrame({
        "nsn": chars["nsn"],
        "attribute_name": chars["requirement_statement"].str.upper(),
        "attribute_value": chars["clear_text_reply"],
        "uom": "",  # PUB LOG clear-text replies embed the unit; S8 normalize parses it out
        "mrc": chars["mrc"],
    }).drop_duplicates().sort_values(["nsn", "attribute_name"])
    write_data_csv(characteristics, "characteristics.csv", CHAR_COLS, logger)

    items_without_chars = federal_items[~federal_items["nsn"].isin(set(characteristics["nsn"]))]
    if len(items_without_chars):
        logger.warning("%d item(s) have no PUB LOG characteristics at all — spec extraction must carry their profile",
                       len(items_without_chars))

    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
