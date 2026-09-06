#!/usr/bin/env python
"""
S11 — assembly_gen.py

Generate the SYNTHETIC assembly structure. Deterministic (fixed seed). Assigns federal
items to slots in a fictional, generically-named hierarchy and defines interfaces
between a handful of slot pairs (the demo click path). No real weapon system is modelled;
only the catalog data attached to each slot is real.

Inputs:  data/federal_items.csv (from S1) if present, else config/target_items(.sample).csv
         data/requirement_profiles.csv (S5) if present — REQUIRED for validated interfaces
         config/fsc_category_map.csv (driving attributes used to pick interface constraints)
Outputs: data/assemblies.csv  assembly_id, name, parent_assembly_id
         data/slots.csv       slot_id, assembly_id, slot_name, baseline_nsn
         data/interfaces.csv  interface_id, slot_a, slot_b, constrained_attribute, match_rule

match_rule grammar (closed; Foundry implements exactly this):
    exact | numeric_equal | numeric_gte | numeric_lte | numeric_within_pct:N
Comparison is DIRECTIONAL (SCHEMA.txt §4): slot_a is the left operand, slot_b the right —
numeric_gte means value(a) >= value(b). Rules are chosen so the BASELINE configuration satisfies
every interface; `rule_holds` is the single encoding of the semantics.
constrained_attribute must be a requirement_name present in requirement_profiles for the baseline
NSN of BOTH slots. When requirement_profiles.csv exists this (and baseline satisfaction) is
enforced and the script exits non-zero on any violation. When it does not exist (build-order step 2, fake-data mode) interfaces
are generated from the FSC driving attributes UNVALIDATED and a loud warning says to re-run S11
after S5.
"""
from __future__ import annotations

import argparse
import math
import random
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DATA_DIR, DroppedRows, load_fsc_map, load_target_items, log_rows, read_csv_str, setup_logging,
    slugify, split_list, write_data_csv,
)

SCRIPT = "s11_assembly_gen"
SEED = 20260905
ROOT_NAME = "demo_electronics_unit"
SUBASSEMBLIES = ["power_distribution", "signal_conditioning", "control_logic", "sensor_interface"]
N_INTERFACE_SLOTS = 6
NUMERIC_RULES = ["numeric_equal", "numeric_gte", "numeric_lte", "numeric_within_pct:10"]
TEXT_RULE = "exact"
_RULE_RE = re.compile(r"^(exact|numeric_equal|numeric_gte|numeric_lte|numeric_within_pct:\d+)$")
# interface constraint chosen per (fsc of slot_a) from its driving attributes; fallback below
FALLBACK_CONSTRAINT = "operating_temp_max"


def valid_rule(rule: str) -> bool:
    return bool(_RULE_RE.match(rule or ""))


def load_profile_attrs(logger) -> dict[str, dict[str, tuple[str, str]]] | None:
    """nsn -> {requirement_name: ('numeric'|'text', contract value)} from data/requirement_profiles.csv,
    or None if absent. Ranges ('min|max'), sets and booleans are not single comparable values and are
    never offered as interface constraints."""
    path = DATA_DIR / "requirement_profiles.csv"
    if not path.exists():
        return None
    df = read_csv_str(path)
    log_rows(logger, "read data/requirement_profiles.csv", len(df))
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for _, r in df.iterrows():
        st = r.get("parse_status", "")
        kind = "text" if st == "text" else "numeric" if st == "number" else "unparsed"
        if kind == "unparsed":
            continue  # cannot be checked downstream; never constrain on it
        out.setdefault(r["nsn"], {})[r["requirement_name"]] = (kind, r.get("value", ""))
    return out


def rule_holds(rule: str, a: str, b: str, kind: str) -> bool | None:
    """SCHEMA §4 semantics, slot_a = LEFT operand, slot_b = RIGHT operand:
         numeric_gte  a >= b      numeric_lte  a <= b      numeric_equal  a == b      exact  string equality
         numeric_within_pct:N  abs(a-b)/abs(b)*100 <= N   (b == 0 -> only a == 0 passes)
       None when a side is missing / non-finite (Foundry's 'unverified')."""
    if kind == "text":
        return (a == b) if rule == TEXT_RULE else None
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(fa) and math.isfinite(fb)):
        return None
    if rule == "numeric_gte":
        return fa >= fb
    if rule == "numeric_lte":
        return fa <= fb
    if rule == "numeric_equal":
        return fa == fb
    if rule.startswith("numeric_within_pct:"):
        n = float(rule.split(":", 1)[1])
        return (fa == 0.0) if fb == 0.0 else abs(fa - fb) / abs(fb) * 100.0 <= n
    return None


def validate_interfaces(if_df: pd.DataFrame, slots_df: pd.DataFrame,
                        attrs: dict[str, dict[str, tuple[str, str]]]) -> list[str]:
    """Return a list of violations (empty when valid). Also checks that the BASELINE configuration satisfies
    every interface under the directional semantics — a synthetic assembly whose baseline violates its own
    interfaces would show a 'conflict' before the user has decided anything."""
    baseline = dict(zip(slots_df["slot_id"], slots_df["baseline_nsn"]))
    problems: list[str] = []
    for _, r in if_df.iterrows():
        attr, rule = r["constrained_attribute"], r["match_rule"]
        if not valid_rule(rule):
            problems.append(f"{r['interface_id']}: illegal match_rule {rule!r}")
        ok_sides = True
        for side in ("slot_a", "slot_b"):
            nsn = baseline.get(r[side], "")
            have = attrs.get(nsn, {})
            if attr not in have:
                problems.append(f"{r['interface_id']}: constrained_attribute {attr!r} is not a "
                                f"requirement_name in requirement_profiles for {side}={r[side]} (nsn {nsn})")
                ok_sides = False
            elif have[attr][0] == "text" and rule != TEXT_RULE:
                problems.append(f"{r['interface_id']}: text attribute {attr!r} needs match_rule exact")
            elif have[attr][0] == "numeric" and rule == TEXT_RULE:
                problems.append(f"{r['interface_id']}: numeric attribute {attr!r} cannot use exact")
        if ok_sides and valid_rule(rule):
            ka, va = attrs[baseline[r["slot_a"]]][attr]
            _, vb = attrs[baseline[r["slot_b"]]][attr]
            if rule_holds(rule, va, vb, ka) is False:
                problems.append(f"{r['interface_id']}: baseline violates its own interface — {attr} {rule} with "
                                f"slot_a={va!r} slot_b={vb!r}")
    return problems

ASSEMBLIES_COLS = ["assembly_id", "name", "parent_assembly_id"]
SLOTS_COLS = ["slot_id", "assembly_id", "slot_name", "baseline_nsn"]
INTERFACES_COLS = ["interface_id", "slot_a", "slot_b", "constrained_attribute", "match_rule"]


def load_items(logger, dropped) -> pd.DataFrame:
    fi = DATA_DIR / "federal_items.csv"
    if fi.exists():
        df = read_csv_str(fi)
        log_rows(logger, "read data/federal_items.csv", len(df))
        src = "federal_items.csv"
    else:
        logger.warning("data/federal_items.csv not found — generating from target items (fake-data mode)")
        df = load_target_items(logger, dropped)
        src = "target_items"
    df = df[df["nsn"] != ""].copy()
    if len(df) == 0:
        raise SystemExit(f"No items with an NSN in {src}; nothing to assign to slots.")
    return df[["nsn", "fsc", "item_name"]].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    rng = random.Random(args.seed)

    items = load_items(logger, dropped)
    fsc_map = load_fsc_map(logger)
    driving = {row["fsc"]: split_list(row["driving_attributes"]) for _, row in fsc_map.iterrows()}
    profile_attrs = load_profile_attrs(logger)
    if profile_attrs is None:
        logger.warning("=" * 78)
        logger.warning("data/requirement_profiles.csv not found: interfaces are UNVALIDATED (fake-data mode).")
        logger.warning("Re-run S11 after S5 so constrained_attribute is checked against both baseline profiles.")
        logger.warning("=" * 78)

    # ---- assemblies
    assemblies = [{"assembly_id": "ASM-000", "name": ROOT_NAME, "parent_assembly_id": ""}]
    for i, name in enumerate(SUBASSEMBLIES, start=1):
        assemblies.append({"assembly_id": f"ASM-{i:03d}", "name": name, "parent_assembly_id": "ASM-000"})
    asm_df = pd.DataFrame(assemblies)
    leaf_ids = [a["assembly_id"] for a in assemblies[1:]]
    leaf_name = {a["assembly_id"]: a["name"] for a in assemblies}

    # ---- slots: shuffle items deterministically, deal round-robin so every sub-assembly is populated
    order = list(range(len(items)))
    rng.shuffle(order)
    slots = []
    per_asm_counter = {a: 0 for a in leaf_ids}
    for k, idx in enumerate(order):
        row = items.iloc[idx]
        asm = leaf_ids[k % len(leaf_ids)]
        per_asm_counter[asm] += 1
        base = slugify(row["item_name"].split(",")[0]) or "item"
        slots.append({
            "slot_id": f"SLOT-{k + 1:03d}",
            "assembly_id": asm,
            "slot_name": f"{leaf_name[asm]}_{base}_{per_asm_counter[asm]:02d}",
            "baseline_nsn": row["nsn"],
            "_fsc": row["fsc"],
        })
    slots_df = pd.DataFrame(slots)

    # ---- interfaces on ~6 slots: adjacent pairs within the first two sub-assemblies (the demo click path)
    demo_slots = slots_df[slots_df["assembly_id"].isin(leaf_ids[:2])].head(N_INTERFACE_SLOTS)
    if len(demo_slots) < 2:
        demo_slots = slots_df.head(N_INTERFACE_SLOTS)
    demo_ids = list(demo_slots["slot_id"])
    demo_fsc = dict(zip(demo_slots["slot_id"], demo_slots["_fsc"]))
    demo_nsn = dict(zip(demo_slots["slot_id"], demo_slots["baseline_nsn"]))
    interfaces = []
    # chain: (1,2), (2,3) ... plus one cross-link (first,last) so a substitution can cascade
    pairs = [(demo_ids[i], demo_ids[i + 1]) for i in range(len(demo_ids) - 1)]
    if len(demo_ids) >= 4:
        pairs.append((demo_ids[0], demo_ids[-1]))
    n = 0
    for a, b in pairs:
        if profile_attrs is not None:
            # validated mode: only attributes present in BOTH baseline profiles are legal
            have_a, have_b = profile_attrs.get(demo_nsn[a], {}), profile_attrs.get(demo_nsn[b], {})
            shared = sorted(set(have_a) & set(have_b))
            if not shared:
                logger.warning("no shared requirement between %s (%s) and %s (%s) — interface skipped",
                               a, demo_nsn[a], b, demo_nsn[b])
                continue
            preferred = [x for x in (driving.get(demo_fsc[a]) or []) if x in shared]
            # deterministic: try the preferred attributes first, keep the first (attr, rules) the baseline satisfies
            attr = rule = None
            for cand_attr in preferred + [x for x in shared if x not in preferred]:
                kind, va = have_a[cand_attr]
                vb = have_b[cand_attr][1]
                rules = [TEXT_RULE] if kind == "text" else NUMERIC_RULES
                ok = [ru for ru in rules if rule_holds(ru, va, vb, kind) is True]
                if ok:
                    attr, rule = cand_attr, rng.choice(ok)
                    break
            if attr is None:
                logger.warning("no shared requirement of %s and %s is satisfied at baseline under any legal rule — "
                               "interface skipped", a, b)
                continue
        else:
            attrs = driving.get(demo_fsc[a]) or [FALLBACK_CONSTRAINT]
            shared = [x for x in attrs if x in (driving.get(demo_fsc[b]) or [])]
            attr = rng.choice(shared) if shared else FALLBACK_CONSTRAINT
            rule = rng.choice(NUMERIC_RULES)
        n += 1
        interfaces.append({"interface_id": f"IF-{n:03d}", "slot_a": a, "slot_b": b,
                           "constrained_attribute": attr, "match_rule": rule})
    if_df = pd.DataFrame(interfaces, columns=INTERFACES_COLS)
    if len(if_df) == 0:
        logger.warning("no interfaces generated (fewer than 2 slots, or no shared requirements)")

    if profile_attrs is not None:
        problems = validate_interfaces(if_df, slots_df, profile_attrs)
        if problems:
            for p in problems:
                logger.error(p)
            raise SystemExit(f"{len(problems)} interface violation(s) — nothing written. Fix profiles or generator.")
        logger.info("interfaces validated: every constrained_attribute exists in both baseline profiles, "
                    "every match_rule is in the closed grammar")
    bad_rules = [r for r in if_df["match_rule"] if not valid_rule(r)]
    if bad_rules:
        raise SystemExit(f"illegal match_rule generated: {bad_rules}")

    write_data_csv(asm_df, "assemblies.csv", ASSEMBLIES_COLS, logger)
    write_data_csv(slots_df.drop(columns=["_fsc"]), "slots.csv", SLOTS_COLS, logger)
    write_data_csv(if_df, "interfaces.csv", INTERFACES_COLS, logger)
    logger.info("demo click-path slots (interfaces defined): %s", ", ".join(demo_ids))
    if profile_attrs is None:
        logger.warning("REMINDER: interfaces above are unvalidated — re-run S11 after S5.")
    logger.info("SYNTHETIC structure: names are generic, assignment is seeded random (seed=%d).", args.seed)
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
