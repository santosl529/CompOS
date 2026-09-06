#!/usr/bin/env python
"""
S5 — build_requirement_profile.py

Merge PUB LOG characteristics (S1) with extracted specification requirements (S4) into one
requirement profile per federal item. Where both sources cover the same canonical attribute
(SCHEMA.txt §1, "merge precedence with a confidence floor"):
    spec extraction confidence >= 0.70  -> specification wins
    spec extraction confidence <  0.70  -> PUB LOG wins (the extraction stays in spec_requirements)
    manual_override                     -> always wins
The losing value is STORED on the winning row as conflicting_value / conflicting_source (both nullable),
not merely logged: source disagreement is a queryable finding.

Inputs:  data/characteristics.csv      (S1) required
         data/federal_items.csv        (S1) required — governing_spec_ref joins items to specs
         data/spec_requirements.csv    (S4) optional — without it, profiles are PUB LOG only
         config/attribute_aliases.csv
Output:  data/requirement_profiles.csv  requirement_id, nsn, requirement_name, operator, value, uom,
                                        requirement_class, source, source_spec_id, conflicting_value,
                                        conflicting_source
         (+ value_raw, parse_status, raw_requirement_name, confidence, source_page extras)

requirement_id = "{nsn}__{requirement_name}" (unique: S5 keeps one row per item and attribute).
source ∈ {publog_characteristics, spec_extraction, manual_override} — nothing else. Rows that S4 marked
origin=override (from config/spec_requirements_overrides.csv) become manual_override.
source_spec_id is the spec_id for spec_extraction / manual_override rows and null for PUB LOG rows.
THIS IS THE TABLE THE APPLICATION READS (PRD); specifications.csv / spec_requirements.csv are provenance.

`value` is written PRE-NORMALISED (PRD: one unit per attribute, Foundry does no conversion): a bare
number in the attribute's base unit, 'min|max' for a range, or normalised text; `uom` is the canonical
unit. Range attributes such as operating_temp are split into *_min / *_max rows here. The original
text is kept in `value_raw`. Values that could not be parsed keep their raw text with
parse_status=unparsed and are logged — they will score `unknown` in S9.

Attribute names are mapped to the canonical vocabulary here so that S8/S9 can compare like with
like. PUB LOG characteristics with no canonical alias are NOT scoreable (Digi-Key has no
counterpart) and are dropped by default with a logged reason, so they do not inflate the
unknown ratio in S9. Use --keep-unaliased to carry them through anyway (they will score unknown).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CONFIG_DIR, Aliases, DroppedRows, infer_class, log_rows, read_csv_str, read_data_csv, setup_logging, spec_key,
    write_data_csv,
)
from s04_spec_extract import apply_overrides  # noqa: E402
from s08_normalize import bare_value, normalize_profiles, normalize_value, value_with_uom  # noqa: E402

SCRIPT = "s05_build_requirement_profile"
PROFILE_COLS = ["requirement_id", "nsn", "requirement_name", "operator", "value", "uom", "requirement_class", "source",
                "source_spec_id", "conflicting_value", "conflicting_source"]
SOURCES = {"publog_characteristics", "spec_extraction", "manual_override"}
CONFIDENCE_FLOOR = 0.70   # SCHEMA §1: below this an LLM extraction does not beat structured PUB LOG data
EXTRA_COLS = ["raw_requirement_name", "confidence", "source_page", "conflicting_value", "conflicting_source"]
OUT_EXTRAS = ["value_raw", "parse_status", "raw_requirement_name", "confidence", "source_page"]
# columns carried from the raw merge into to_contract_values (before value normalisation)
MERGE_COLS = ["nsn", "requirement_name", "operator", "value", "uom", "requirement_class", "source", "source_spec_id"] + EXTRA_COLS


def requirement_id(nsn: str, requirement_name: str) -> str:
    return f"{nsn}__{requirement_name}"


def _losing_value(value: str, attr: str, uom: str = "") -> str:
    """Contract form of the losing value when it parses, else the raw text — so conflicting_value is
    comparable with value in Foundry without unit conversion."""
    n = normalize_value(value_with_uom(value, uom), attr)
    return bare_value(n.as_row()) if n.kind in ("number", "range", "text") else str(value)


def merge_sources(spec_df: pd.DataFrame, pub: pd.DataFrame, aliases: Aliases) -> tuple[pd.DataFrame, dict]:
    """One row per (nsn, requirement_name). Precedence (SCHEMA §1):
         manual_override           -> wins
         spec_extraction conf>=0.70 -> wins
         otherwise                  -> publog_characteristics wins
    The loser's value/source go into the winner's conflicting_value / conflicting_source."""
    stats = {"both": 0, "spec_won": 0, "publog_won": 0, "override_won": 0}
    pub_idx = {(r["nsn"], r["requirement_name"]): i for i, r in pub.iterrows()}
    keep_pub = set(pub_idx)
    spec_rows: list[dict] = []
    for _, s in spec_df.iterrows():
        s = dict(s)
        key = (s["nsn"], s["requirement_name"])
        p = pub.loc[pub_idx[key]] if key in pub_idx else None
        if p is None:
            spec_rows.append(s)
            continue
        stats["both"] += 1
        conf = pd.to_numeric(s.get("confidence", ""), errors="coerce")
        spec_wins = s["source"] == "manual_override" or (pd.notna(conf) and float(conf) >= CONFIDENCE_FLOOR)
        if spec_wins:
            stats["override_won" if s["source"] == "manual_override" else "spec_won"] += 1
            s["conflicting_value"] = _losing_value(p["value"], s["requirement_name"], p.get("uom", ""))
            s["conflicting_source"] = p["source"]
            spec_rows.append(s)
            keep_pub.discard(key)
        else:
            stats["publog_won"] += 1
            pub.at[pub_idx[key], "conflicting_value"] = _losing_value(s["value"], s["requirement_name"], s.get("uom", ""))
            pub.at[pub_idx[key], "conflicting_source"] = s["source"]
    pub_kept = pub[[(n, a) in keep_pub for n, a in zip(pub["nsn"], pub["requirement_name"])]]
    merged = pd.concat([pd.DataFrame(spec_rows, columns=MERGE_COLS), pub_kept], ignore_index=True)
    return merged, stats


def to_contract_values(profiles: pd.DataFrame, aliases: Aliases, dropped: DroppedRows, logger) -> pd.DataFrame:
    """Normalise values (S8 logic), split range attributes, write bare contract values."""
    # conflicting_* already ride through normalize_profiles row by row; the rest are re-attached by pre-split name
    carried = [c for c in EXTRA_COLS if c not in ("conflicting_value", "conflicting_source")]
    extras = profiles[["nsn", "requirement_name", "value"] + carried].drop_duplicates(["nsn", "requirement_name"])
    norm = normalize_profiles(profiles, aliases, dropped)
    # normalize_profiles keeps the raw value in `value` and the pre-split name in raw_requirement_name
    norm = norm.rename(columns={"value": "value_raw", "raw_requirement_name": "_pre_split_name"})
    norm["value"] = [bare_value(r) if r["parse_status"] in ("number", "range", "text") else r["value_raw"]
                     for _, r in norm.iterrows()]
    norm = norm.merge(extras.rename(columns={"requirement_name": "_pre_split_name", "value": "_v"})
                      .drop(columns=["_v"]), on=["nsn", "_pre_split_name"], how="left")
    unparsed = norm[norm["parse_status"] == "unparsed"]
    if len(unparsed):
        logger.warning("%d requirement value(s) could not be normalised (kept raw, will score unknown); e.g. %s",
                       len(unparsed), "; ".join((unparsed["requirement_name"] + "=" + unparsed["value_raw"]).head(5)))
    return norm.drop(columns=["_pre_split_name", "value_num", "value_min", "value_max", "value_text"], errors="ignore")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-unaliased", action="store_true",
                    help="keep PUB LOG characteristics that have no canonical alias (they will score unknown)")
    args = ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    aliases = Aliases()

    chars = read_data_csv("characteristics.csv", logger=logger)
    items = read_data_csv("federal_items.csv", logger=logger)
    specs = read_data_csv("spec_requirements.csv", required=False, logger=logger)

    item_nsns = set(items["nsn"])
    orphan = chars[~chars["nsn"].isin(item_nsns)]
    dropped.drop("characteristic for nsn not in federal_items", orphan, "characteristics")
    chars = chars[chars["nsn"].isin(item_nsns)]

    # ---- PUB LOG rows
    pub_rows: list[dict] = []
    unaliased_rows: list[pd.Series] = []
    for _, r in chars.iterrows():
        canon, known = aliases.canonical("publog", r["attribute_name"])
        if not known and not args.keep_unaliased:
            unaliased_rows.append(r)
            continue
        pub_rows.append({
            "nsn": r["nsn"], "requirement_name": canon, "operator": "", "value": r["attribute_value"],
            "uom": r.get("uom", ""), "requirement_class": infer_class(canon), "source": "publog_characteristics",
            "source_spec_id": "", "raw_requirement_name": r["attribute_name"], "confidence": "", "source_page": "",
            "conflicting_value": "", "conflicting_source": "",
        })
    if unaliased_rows:
        dropped.drop("publog characteristic has no canonical alias (not scoreable)", pd.DataFrame(unaliased_rows),
                     "characteristics")
    pub = pd.DataFrame(pub_rows, columns=MERGE_COLS)
    log_rows(logger, "publog requirements (aliased)", len(pub))

    # ---- spec rows, joined to items through governing_spec_ref
    spec_df = pd.DataFrame(columns=MERGE_COLS)
    if specs is not None and len(specs):
        specs = specs.copy()
        # config/spec_requirements_overrides.csv is applied by S4 at extraction time; applying it here as well
        # means a correction takes effect on the next S5 run without re-extracting (and re-spending) S4.
        # Same function, same semantics: replace / append / DELETE by (spec_id, requirement_name).
        overrides_path = CONFIG_DIR / "spec_requirements_overrides.csv"
        if overrides_path.exists():
            specs = apply_overrides(specs, read_csv_str(overrides_path), dropped, logger)
        specs["_key"] = specs["spec_id"].map(spec_key)
        items_k = items.copy()
        items_k["_key"] = items_k["governing_spec_ref"].map(spec_key)
        items_k = items_k[items_k["_key"] != ""]
        joined = items_k[["nsn", "governing_spec_ref", "_key"]].merge(specs, on="_key", how="inner")
        log_rows(logger, "spec requirement rows joined to items", len(joined))
        unmatched_specs = set(specs["_key"]) - set(items_k["_key"])
        if unmatched_specs:
            logger.warning("%d extracted spec(s) match no federal item's governing_spec_ref: %s",
                           len(unmatched_specs), ", ".join(sorted(unmatched_specs)[:10]))
        items_no_spec_rows = set(items["nsn"]) - set(joined["nsn"])
        if items_no_spec_rows:
            logger.warning("%d item(s) have no spec requirements (no governing spec, or spec not extracted)",
                           len(items_no_spec_rows))
        rows = []
        for _, r in joined.iterrows():
            canon, _known = aliases.canonical("spec", r["requirement_name"])
            origin = r.get("origin", "")   # S4 extra column; absent in hand-made files, NaN on rows concat'd by overrides
            origin = "" if origin is None or (isinstance(origin, float) and pd.isna(origin)) else str(origin).strip().lower()
            rows.append({
                "nsn": r["nsn"], "requirement_name": canon, "operator": r.get("operator", ""), "value": r["value"],
                "uom": r.get("uom", ""), "requirement_class": infer_class(canon, r.get("requirement_class", "")),
                "source": "manual_override" if origin == "override" else "spec_extraction",
                "source_spec_id": r["spec_id"], "raw_requirement_name": r["requirement_name"],
                "confidence": r.get("confidence", ""), "source_page": r.get("source_page", ""),
                "conflicting_value": "", "conflicting_source": "",
            })
        spec_df = pd.DataFrame(rows, columns=MERGE_COLS)
        # a spec may state the same requirement twice (e.g. per page); keep the highest-confidence one
        spec_df["_conf"] = pd.to_numeric(spec_df["confidence"], errors="coerce").fillna(0)
        before = len(spec_df)
        spec_df = spec_df.sort_values("_conf", ascending=False).drop_duplicates(["nsn", "requirement_name"], keep="first")
        dropped.drop("duplicate spec requirement for item (kept highest confidence)", before - len(spec_df), "spec")
        spec_df = spec_df.drop(columns=["_conf"])
    else:
        logger.warning("no data/spec_requirements.csv — profiles are PUB LOG characteristics only")

    # duplicate PUB LOG statements for the same canonical attribute (e.g. two tolerance MRCs): keep first, log
    before = len(pub)
    pub = pub.drop_duplicates(["nsn", "requirement_name"], keep="first")
    dropped.drop("duplicate publog requirement for item (kept first)", before - len(pub), "merge")

    # ---- conflict resolution with a confidence floor; the loser is stored on the winner, not just logged
    profiles, stats = merge_sources(spec_df, pub, aliases)
    logger.info("merge: %d attribute(s) covered by both sources — spec won %d, publog won %d (extraction "
                "confidence < %.2f), override won %d", stats["both"], stats["spec_won"], stats["publog_won"],
                CONFIDENCE_FLOOR, stats["override_won"])
    if stats["publog_won"]:
        logger.warning("%d low-confidence extraction(s) lost to PUB LOG — they remain in spec_requirements.csv; "
                       "confirm or override them in config/spec_requirements_overrides.csv", stats["publog_won"])
    profiles = to_contract_values(profiles, aliases, dropped, logger)
    profiles = profiles.sort_values(["nsn", "requirement_class", "requirement_name"]).reset_index(drop=True)
    profiles["requirement_id"] = [requirement_id(n, a) for n, a in zip(profiles["nsn"], profiles["requirement_name"])]
    bad_source = profiles[~profiles["source"].isin(SOURCES)]
    if len(bad_source):
        raise SystemExit(f"internal error: source values outside {sorted(SOURCES)}: {sorted(set(bad_source['source']))}")
    if profiles["requirement_id"].duplicated().any():
        dup = profiles[profiles["requirement_id"].duplicated(keep=False)]
        raise SystemExit(f"requirement_id is not unique after merge — inspect: {sorted(set(dup['requirement_id']))[:5]}")
    write_data_csv(profiles, "requirement_profiles.csv", PROFILE_COLS + OUT_EXTRAS, logger)
    items_without = item_nsns - set(profiles["nsn"])
    if items_without:
        logger.warning("%d item(s) ended with an EMPTY profile — every candidate for them will be all-unknown: %s",
                       len(items_without), ", ".join(sorted(items_without)[:10]))
    if len(profiles):
        logger.info("requirements per item: min %d / median %.0f / max %d",
                    profiles.groupby("nsn").size().min(), profiles.groupby("nsn").size().median(),
                    profiles.groupby("nsn").size().max())
        logger.info("source mix: %s", profiles["source"].value_counts().to_dict())
    aliases.report(logger)
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
