#!/usr/bin/env python
"""
S2 — qpl_ingest.py

Convert the manually-downloaded Microsoft Access qualification databases (QPL/QML from DLA
Land and Maritime — downloaded by hand; that site forbids automated access) into
data/qpl.csv.

Inputs:  raw/qpl/*.mdb|*.accdb   (override with QPL_DIR)
         config/qpl_columns.json  column-name heuristics (the Access schema was unknown when written)
System:  mdbtools  (`brew install mdbtools`) — provides mdb-tables / mdb-export
Cache:   cache/qpl/<file>/<table>.csv — every table exported once, never re-exported
Output:  data/qpl.csv  qpl_id, governing_spec, manufacturer_name, cage_code, qualified_part_number,
                       qualification_date  (+ source_spec_file, source_table extras)

Use --inspect to print every table and its columns (no output written) — do this first on a
real file, then fix config/qpl_columns.json if S2 reports tables it could not map.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CACHE_DIR, CONFIG_DIR, RAW_DIR, DroppedRows, ensure_dirs, env, log_rows, read_csv_str, setup_logging,
    write_data_csv,
)

SCRIPT = "s02_qpl_ingest"
# Contract (SCHEMA §6): qpl_id, governing_spec, manufacturer_name, cage_code, qualified_part_number, qualification_date.
# source_spec_file (PRD) and source_table are kept as extras after the contract columns.
QPL_COLS = ["qpl_id", "governing_spec", "manufacturer_name", "cage_code", "qualified_part_number", "qualification_date"]
MAPPED_COLS = ["governing_spec", "manufacturer_name", "cage_code", "qualified_part_number", "qualification_date"]


def qpl_id(governing_spec: str, cage_code: str, qualified_part_number: str) -> str:
    """Surrogate PK from the natural key (spec, CAGE, part number), whitespace-free."""
    return "__".join(re.sub(r"\s+", "", str(x or "")).upper() or "NONE"
                     for x in (governing_spec, cage_code, qualified_part_number))
_SPEC_IN_NAME = re.compile(r"(MIL|DOD|FED|AN|MS|SAE|QQ|WW|ZZ|A-A|JAN)[-_ ]?[A-Z]*[-_ ]?\d+(?:/\d+)?[A-Z]?\b", re.IGNORECASE)


def need_mdbtools() -> None:
    if shutil.which("mdb-tables") is None or shutil.which("mdb-export") is None:
        raise SystemExit("mdbtools not found on PATH. Install with `brew install mdbtools` (macOS) or "
                         "`apt install mdbtools`, then re-run.")


def list_tables(mdb: Path) -> list[str]:
    out = subprocess.run(["mdb-tables", "-1", str(mdb)], capture_output=True, text=True, check=True).stdout
    return [t for t in out.splitlines() if t.strip()]


def export_table(mdb: Path, table: str, dest: Path, logger) -> Path:
    if dest.exists():
        return dest
    ensure_dirs(dest.parent)
    with open(dest.with_suffix(".tmp"), "w") as fh:
        proc = subprocess.run(["mdb-export", "-D", "%Y-%m-%d", str(mdb), table], stdout=fh, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        dest.with_suffix(".tmp").unlink(missing_ok=True)
        raise RuntimeError(f"mdb-export failed for {mdb.name}:{table}: {proc.stderr.strip()[:300]}")
    dest.with_suffix(".tmp").replace(dest)
    logger.info("exported %s:%s -> %s", mdb.name, table, dest.relative_to(CACHE_DIR.parent))
    return dest


def norm_col(c: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(c).upper())


def map_columns(columns: list[str], heuristics: dict) -> dict[str, str]:
    """contract column -> actual column, using ordered candidate lists (first match wins)."""
    lookup = {norm_col(c): c for c in columns}
    out: dict[str, str] = {}
    for contract_col, candidates in heuristics["columns"].items():
        for cand in candidates:
            if norm_col(cand) in lookup:
                out[contract_col] = lookup[norm_col(cand)]
                break
        if contract_col not in out:
            # loose: any column containing the candidate token
            for cand in candidates:
                hit = next((c for k, c in lookup.items() if norm_col(cand) in k and len(norm_col(cand)) >= 4), None)
                if hit:
                    out[contract_col] = hit
                    break
    return out


def spec_from_filename(name: str) -> str:
    m = _SPEC_IN_NAME.search(Path(name).stem.replace("_", "-"))
    return m.group(0).upper().replace("_", "-") if m else Path(name).stem


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qpl-dir", default=None, help="override QPL_DIR (default raw/qpl)")
    ap.add_argument("--inspect", action="store_true", help="list tables and columns of every file; write nothing")
    args = ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    need_mdbtools()
    heuristics = json.loads((CONFIG_DIR / "qpl_columns.json").read_text())

    qpl_dir = Path(args.qpl_dir or env("QPL_DIR", str(RAW_DIR / "qpl")))
    files = sorted([p for p in qpl_dir.rglob("*") if p.suffix.lower() in (".mdb", ".accdb")]) if qpl_dir.exists() else []
    if not files:
        raise SystemExit(f"No .mdb/.accdb files under {qpl_dir}. Download the QPL/QML databases manually from "
                         f"DLA Land and Maritime and place them there (or set QPL_DIR).")
    logger.info("found %d Access file(s) under %s", len(files), qpl_dir)

    all_rows: list[pd.DataFrame] = []
    for mdb in files:
        tables = list_tables(mdb)
        logger.info("%s: %d table(s): %s", mdb.name, len(tables), ", ".join(tables))
        for table in tables:
            dest = CACHE_DIR / "qpl" / mdb.stem / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', table)}.csv"
            try:
                export_table(mdb, table, dest, logger)
            except RuntimeError as exc:
                logger.error(str(exc))
                dropped.drop(f"table export failed: {mdb.name}:{table}", 1, "export")
                continue
            try:
                df = read_csv_str(dest)
            except pd.errors.EmptyDataError:
                logger.info("%s:%s is empty — skipped", mdb.name, table)
                continue
            if args.inspect:
                print(f"\n[{mdb.name}] {table}  ({len(df)} rows)")
                for c in df.columns:
                    sample = next((v for v in df[c].head(20) if v), "")
                    print(f"  {c:<40} e.g. {sample[:60]!r}")
                continue
            colmap = map_columns(list(df.columns), heuristics)
            required = heuristics.get("required_for_row", ["qualified_part_number", "manufacturer_name"])
            missing = [c for c in required if c not in colmap]
            if missing:
                logger.warning("%s:%s skipped — could not map %s (columns: %s)", mdb.name, table, missing, list(df.columns))
                dropped.drop(f"table skipped, unmapped {missing}: {mdb.name}:{table}", len(df), "map")
                continue
            out = pd.DataFrame({c: (df[colmap[c]] if c in colmap else "") for c in MAPPED_COLS})
            if "governing_spec" not in colmap or (out["governing_spec"] == "").all():
                out["governing_spec"] = spec_from_filename(mdb.name)
                logger.info("%s:%s has no spec column — governing_spec taken from the filename: %s",
                            mdb.name, table, out["governing_spec"].iloc[0] if len(out) else "")
            out["source_spec_file"] = mdb.name
            out["source_table"] = table
            out["cage_code"] = out["cage_code"].astype(str).str.strip().str.upper()
            out["qualified_part_number"] = out["qualified_part_number"].astype(str).str.strip().str.upper()
            out["manufacturer_name"] = out["manufacturer_name"].astype(str).str.strip()
            blank = out[(out["qualified_part_number"] == "") & (out["manufacturer_name"] == "") & (out["cage_code"] == "")]
            dropped.drop(f"row with no part number, manufacturer or cage: {mdb.name}:{table}", blank, "rows")
            out = out.drop(blank.index)
            unmapped_cols = [c for c in df.columns if c not in colmap.values()]
            logger.info("%s:%s -> %d rows; mapped %s; unmapped source columns: %s", mdb.name, table, len(out),
                        {k: v for k, v in colmap.items()}, unmapped_cols)
            all_rows.append(out)

    if args.inspect:
        return 0
    if not all_rows:
        raise SystemExit("No QPL tables could be mapped. Run with --inspect and update config/qpl_columns.json.")
    qpl = pd.concat(all_rows, ignore_index=True)
    before = len(qpl)
    qpl = qpl.drop_duplicates(["governing_spec", "cage_code", "qualified_part_number", "manufacturer_name"])
    dropped.drop("duplicate qpl row", before - len(qpl), "dedupe")
    qpl["qpl_id"] = [qpl_id(s, c, p) for s, c, p in zip(qpl["governing_spec"], qpl["cage_code"], qpl["qualified_part_number"])]
    dup = qpl[qpl["qpl_id"].duplicated(keep=False)]
    if len(dup):
        # same (spec, CAGE, part number) listed under two manufacturer spellings — one qualification, keep one row
        dropped.drop("same (spec, cage, part) under differing manufacturer_name — kept first", dup.iloc[1:], "dedupe")
        qpl = qpl.drop_duplicates("qpl_id", keep="first")
    write_data_csv(qpl.sort_values(["governing_spec", "manufacturer_name", "qualified_part_number"]), "qpl.csv",
                   QPL_COLS, logger)
    log_rows(logger, "distinct governing specs", qpl["governing_spec"].nunique())
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
