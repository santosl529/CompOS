#!/usr/bin/env python
"""
S9 — score.py

For each (federal item, candidate) pair, compare every requirement in the item's profile
against the candidate's normalised attributes. Per requirement emit pass / fail / marginal /
unknown. Check QPL status. Roll up into a risk level with a plain-language rationale.

Inputs:
  data/requirement_profiles_normalized.csv   (S8)   required
  data/candidates_normalized.csv             (S8)   required
  data/federal_items.csv                     (S1)   optional — governing_spec_ref for the QPL check
  data/qpl.csv                               (S2)   optional — without it nothing can be QPL-listed
  data/commercial_parts_normalized.csv       (S8 of S7) optional — second pass: lifecycle/availability
  config/fsc_category_map.csv                tolerance_rules (pct bands for 'eq' comparisons)
  data/price_history.csv                     (S10) optional — government unit price / quantity
Outputs (contract: docs/SCHEMA.txt §3, plus CHANGE-qualification-burden.md):
  data/substitution_candidates.csv  candidate_id, nsn, candidate_mpn, risk_level, risk_rank, pass_count,
                                    fail_count, marginal_count, unknown_count, qpl_listed, gov_unit_price,
                                    gov_quantity, commercial_unit_price, commercial_basis_qty,
                                    price_delta_indicative, rationale
                                    (+ qualification_gap_count, qualification_gap_summary, source, rank_within_nsn,
                                    composite_score, weight_coverage_pct, gates_failed, country_of_origin,
                                    manufacturer, digikey_pn, governing_spec_ref, enriched, gov_price_fiscal_year extras)
  data/spec_deltas.csv              delta_id, candidate_id, nsn, candidate_mpn, requirement_name, required_value,
                                    candidate_value, verdict  (+ scored, gate_type, operator, uom, *_display, source extras)

candidate_id = "{nsn}__{candidate_mpn}" (surrogate PK; Foundry takes a single key).
delta_id     = "{candidate_id}__{requirement_name}".
risk_rank    = 1/2/3 for low/medium/high. Foundry sorts on this integer because sorting the
               risk_level strings alphabetically gives high -> low -> medium.
Prices: gov_unit_price / gov_quantity come from price_history (most recent fiscal year with a price);
commercial_unit_price / commercial_basis_qty from commercial_parts (median_price at its quantity break).
price_delta_indicative = gov_unit_price - commercial_unit_price. It is NOT savings: the two prices sit on
different quantity and qualification bases. Null when either side is missing — never 0.
required_value / candidate_value are bare numbers in the attribute's base unit (or normalised text for
text attributes) so Foundry can compare them without unit conversion; the human-readable forms are in
required_value_display / candidate_value_display. Operators are the closed enum eq|gte|lte|range|in_set|boolean.

QPL check (SCHEMA §3): listed iff a qpl row for the governing spec matches on qualified_part_number AND
(cage_code OR manufacturer_name). Part number alone or manufacturer alone never qualifies.

Risk (docs/CURSOR-composite-risk.md). Hard gates run first: any actual violation → high,
composite_score=100, skip the weighted composite. Missing values are unverified, not fails.
Survivors get a renormalized 0-100 composite (universal 60 + category 40). Bands: 0-33 low,
34-66 medium, 67-100 high. available_weight 0 → composite_score null, coverage 0, medium.
Qualification/traceability stay a burden (CHANGE-qualification-burden.md): excluded from
counts, verdict unknown, never fail. `low` does not require qpl_listed.
`unknown` is never treated as pass. pass/fail/marginal/unknown counts stay on the contract.

Second pass (after S7): if commercial_parts_normalized.csv has the MPN, lifecycle obsolete raises risk
to high; nrnd or <=1 stocked distributor raises it to at least medium.

The pipeline runs this script twice by design: once on unenriched candidates so S7 knows the
top 3 per item, then again after enrichment for final scores.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DATA_DIR, DroppedRows, infer_class, load_fsc_map, log_rows, norm_key, parse_rules, read_data_csv,
    setup_logging, spec_key, write_data_csv,
)
from s08_normalize import TEXT_ATTRS, bare_number, bare_value, canonical_operator  # noqa: E402

__all__ = ["compare", "rollup", "rank_candidates", "qpl_listed", "build_qpl_index", "spec_key", "infer_class",
           "risk_rank", "RISK_RANK", "candidate_id", "delta_id", "price_delta", "baseline_prices",
           "qualification_delta", "qualification_gap", "is_scored_class", "SCORED_CLASSES", "BURDEN_CLASSES",
           "evaluate_hard_gate", "hard_gates_failed", "hard_gate_attrs", "attr_soft_risk", "composite",
           "risk_from_composite", "gate_type", "CATEGORY_WEIGHTS", "UNIVERSAL_WEIGHTS"]

SCRIPT = "s09_score"
MARGINAL_BAND = 0.10          # within 10% on the wrong side of a threshold -> marginal, not fail
DEFAULT_PCT = 5.0             # 'eq' numeric comparisons without an fsc-map pct rule: ±5% pass, ±10% marginal
UNKNOWN_RATIO_MEDIUM = 0.30   # SCHEMA §10: >30% unknown -> medium
MARGINAL_RATIO_MEDIUM = 0.30  # SCHEMA §10: >30% marginal -> medium (eleven near-misses are not a low-risk part)
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unscored": 3}
RISK_RANK = {"low": 1, "medium": 2, "high": 3, "unscored": 4}
SCORED_CLASSES = {"electrical", "mechanical", "environmental"}
BURDEN_CLASSES = {"qualification", "traceability"}
OP_DISPLAY = {"eq": "==", "gte": ">=", "lte": "<=", "range": "in", "in_set": "one of", "boolean": "is"}

# SCHEMA §3: there is deliberately NO estimated_savings. Both prices and both quantity bases are carried,
# and the difference is named for what it is — indicative. The UI shows all four, never the delta alone.
SC_COLS = ["candidate_id", "nsn", "candidate_mpn", "risk_level", "risk_rank", "pass_count", "fail_count",
           "marginal_count", "unknown_count", "qpl_listed", "gov_unit_price", "gov_quantity",
           "commercial_unit_price", "commercial_basis_qty", "price_delta_indicative", "rationale"]
SD_COLS = ["delta_id", "candidate_id", "nsn", "candidate_mpn", "requirement_name", "required_value", "candidate_value",
           "verdict"]


def risk_rank(risk_level: str) -> int:
    """low -> 1, medium -> 2, high -> 3, unscored -> 4. Raises on anything else."""
    try:
        return RISK_RANK[risk_level]
    except KeyError:
        raise ValueError(f"risk_level must be one of {sorted(RISK_RANK)}, got {risk_level!r}") from None


def candidate_id(nsn: str, mpn: str) -> str:
    return f"{nsn}__{mpn}"


def delta_id(cid: str, requirement_name: str) -> str:
    return f"{cid}__{requirement_name}"


def bare(v: float | None) -> str:
    """Numeric value as written to the contract: plain repr in base unit, '' if None."""
    return bare_number(v)

def _f(x) -> float | None:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(v: float | None, uom: str) -> str:
    if v is None:
        return ""
    if abs(v) >= 1e6 or (abs(v) < 1e-3 and v != 0):
        s = f"{v:.4g}"
    else:
        s = f"{v:.6g}"
    return f"{s} {uom}".strip()


# ------------------------------------------------------------------ per-requirement comparison
class Delta(NamedTuple):
    verdict: str
    required: str          # human-readable, e.g. ">= 0.125 W"
    candidate: str         # human-readable, e.g. "0.1 W"
    required_bare: str     # contract value: base-unit number, "min|max" for a range, or normalised text
    candidate_bare: str
    uom: str


def _bare_of(row: dict | None) -> str:
    return "" if row is None else bare_value(row)


def compare(req: dict, cand: dict | None, pct_rule: float | None = None) -> Delta:
    """
    req:  {requirement_name, operator, value_num, value_min, value_max, value_text, uom, parse_status}
    cand: {value_num, value_min, value_max, value_text, uom, parse_status} or None if the candidate
          has no such attribute.
    """
    verdict, required, candidate = _verdict(req, cand, pct_rule)
    return Delta(verdict, required, candidate, _bare_of(req), _bare_of(cand), req.get("uom", "") or "")


def is_scored_class(requirement_class: str) -> bool:
    """Electrical / mechanical / environmental are scored. Qualification and traceability are a burden."""
    return (requirement_class or "") in SCORED_CLASSES


def qualification_gap(reqs: list[dict]) -> tuple[int, str]:
    """Count of qualification/traceability clauses on the baseline profile, and a stable pipe-delimited
    name list. Identical for every candidate of an NSN — the gap is a property of the substitution."""
    names: list[str] = []
    seen: set[str] = set()
    for r in reqs:
        name = r.get("requirement_name", "") or ""
        cls = infer_class(name, r.get("requirement_class", ""))
        if cls in BURDEN_CLASSES and name and name not in seen:
            seen.add(name)
            names.append(name)
    names.sort()
    return len(names), "|".join(names)


def qualification_delta(req: dict, listed: bool) -> Delta:
    """A `qualification` requirement is not scored. Verdict is always unknown: not verifiable from
    commercial data is not the same claim as does not meet. qpl_listed stays on the candidate row."""
    required = req.get("value_text") or req.get("value") or "QPL listing required"
    return Delta("unknown", f"is {required}", "QPL-listed" if listed else "not QPL-listed",
                 str(required), "QPL_LISTED" if listed else "NOT_QPL_LISTED", "")


def _text_subsumes(short: str, long: str) -> bool:
    """`short` is a clean_text form ("ALUMINUM") that `long` extends ("ALUMINUMALLOY"). Both are already
    upper-case alphanumerics with separators removed, so containment is the only structure left; a 3-char
    floor keeps single letters and two-letter codes from matching everything."""
    return len(short) >= 3 and short != long and short in long


def _as_bool(text: str) -> bool | None:
    t = (text or "").strip().lower()
    if t in ("true", "yes", "y", "1", "compliant", "required"):
        return True
    if t in ("false", "no", "n", "0", "non-compliant", "not required"):
        return False
    return None


def _verdict(req: dict, cand: dict | None, pct_rule: float | None = None) -> tuple[str, str, str]:
    """Returns (verdict, required_value_display, candidate_value_display)."""
    attr = req["requirement_name"]
    op = canonical_operator(req.get("operator") or "") or "eq"
    uom = req.get("uom", "")
    r_num, r_min, r_max = _f(req.get("value_num")), _f(req.get("value_min")), _f(req.get("value_max"))
    r_txt = req.get("value_text", "") or ""
    r_status = req.get("parse_status", "")

    if r_status == "range":
        required = f"{fmt(r_min, uom)} to {fmt(r_max, uom)}"
    elif r_status == "set":
        required = "one of " + ", ".join(r_txt.split("|"))
    elif r_status in ("text", "boolean"):
        required = f"{OP_DISPLAY.get(op, op)} {r_txt}"
    else:
        required = f"{OP_DISPLAY.get(op, op)} {fmt(r_num, uom)}" if r_num is not None else (r_txt or req.get("value", ""))

    if cand is None:
        return "unknown", required, ""
    c_status = cand.get("parse_status", "")
    c_num, c_min, c_max = _f(cand.get("value_num")), _f(cand.get("value_min")), _f(cand.get("value_max"))
    c_txt = cand.get("value_text", "") or ""
    candidate = (f"{fmt(c_min, cand.get('uom', ''))} to {fmt(c_max, cand.get('uom', ''))}" if c_status == "range"
                 else fmt(c_num, cand.get("uom", "")) if c_num is not None else c_txt)

    # anything unparseable on either side is unknown — never pass
    if r_status in ("unparsed", "empty") or c_status in ("unparsed", "empty"):
        return "unknown", required, candidate

    # --- in_set: candidate's normalised value (number in base unit, or text) must be a member
    if r_status == "set" or op == "in_set":
        members = set(r_txt.split("|")) if r_txt else set()
        c_val = bare_number(c_num) if c_num is not None else c_txt
        if not members or not c_val:
            return "unknown", required, candidate
        return ("pass" if c_val in members else "fail"), required, candidate

    # --- boolean: candidate side is whatever text the distributor gives ("Yes", "RoHS3") — only an explicit
    # truthy/falsy token is comparable; everything else is unknown
    if r_status == "boolean" or op == "boolean":
        c_bool = _as_bool(c_txt)
        if r_txt not in ("true", "false") or c_bool is None:
            return "unknown", required, candidate
        return ("pass" if (r_txt == "true") == c_bool else "fail"), required, candidate

    # --- text comparison
    if r_status == "text" or attr in TEXT_ATTRS:
        if not r_txt or not c_txt:
            return "unknown", required, candidate
        if r_txt == c_txt:
            return "pass", required, candidate
        # one side is a qualified form of the other: "SPDT" vs "SPDT (1 Form C)", "ALUMINUM" vs "ALUMINUM ALLOY",
        # "GOLD" vs "GOLD OVER NICKEL". Not provably equal, not provably different — a human decides.
        if _text_subsumes(r_txt, c_txt) or _text_subsumes(c_txt, r_txt):
            return "marginal", required, candidate
        return "fail", required, candidate

    # --- numeric
    if r_status == "range":
        if r_min is None or r_max is None:
            return "unknown", required, candidate
        if c_status == "range" and c_min is not None and c_max is not None:
            if c_min <= r_min and c_max >= r_max:
                return "pass", required, candidate
            span = (r_max - r_min) or 1.0
            if c_min <= r_min + MARGINAL_BAND * span and c_max >= r_max - MARGINAL_BAND * span:
                return "marginal", required, candidate
            return "fail", required, candidate
        if c_num is None:
            return "unknown", required, candidate
        if r_min <= c_num <= r_max:
            return "pass", required, candidate
        span = (r_max - r_min) or abs(r_max) or 1.0
        if r_min - MARGINAL_BAND * span <= c_num <= r_max + MARGINAL_BAND * span:
            return "marginal", required, candidate
        return "fail", required, candidate

    if r_num is None:
        return "unknown", required, candidate

    # candidate given as a range (e.g. an operating span): its capability is the bound on the
    # requirement's side — upper bound for a ">=" rating floor, lower bound for a "<=" ceiling.
    if c_num is None:
        if c_status == "range" and c_min is not None and c_max is not None:
            if op == "gte":
                c_num = c_max
            elif op == "lte":
                c_num = c_min
            else:
                c_num = (c_min + c_max) / 2.0
        else:
            return "unknown", required, candidate

    if op == "gte":
        if c_num >= r_num:
            return "pass", required, candidate
        if c_num >= r_num * (1 - MARGINAL_BAND) if r_num >= 0 else c_num >= r_num * (1 + MARGINAL_BAND):
            return "marginal", required, candidate
        return "fail", required, candidate
    if op == "lte":
        if c_num <= r_num:
            return "pass", required, candidate
        if c_num <= r_num * (1 + MARGINAL_BAND) if r_num >= 0 else c_num <= r_num * (1 - MARGINAL_BAND):
            return "marginal", required, candidate
        return "fail", required, candidate

    # 'eq' numeric: pct band from fsc map, else DEFAULT_PCT
    pct = pct_rule if pct_rule is not None else DEFAULT_PCT
    if r_num == 0:
        diff = abs(c_num)
        return ("pass" if diff < 1e-12 else "fail"), required, candidate
    rel = abs(c_num - r_num) / abs(r_num) * 100.0
    if rel <= pct + 1e-9:
        return "pass", required, candidate
    if rel <= 2 * pct + 1e-9:
        return "marginal", required, candidate
    return "fail", required, candidate


# ------------------------------------------------------------------ hard gates + weighted composite (CURSOR-composite-risk)
UNIVERSAL_WEIGHTS = {"lead_time": 38, "unit_cost": 22}
CATEGORY_WEIGHTS: dict[str, dict[str, int]] = {
    "5905": {"resistance_tolerance": 23, "temperature_coefficient": 17},
    "5910": {"capacitance_tolerance": 13, "dissipation_factor": 13, "dielectric_type": 14},
    "5915": {"insertion_loss": 20, "current_rating": 20},
    "5930": {"mechanical_life": 23, "actuation_pressure": 17},
    "5935": {"contact_resistance": 13, "insulation_resistance": 10, "contact_plating": 10, "shell_material": 7},
    "5945": {"coil_resistance": 40},
    "5950": {"inductance_tolerance": 13, "dc_resistance": 13, "q_factor": 14},
    "5961": {"voltage_rating": 20, "current_rating": 20},
    "5999": {"contact_plating": 20, "shell_material": 20},
}
TYPE_A = {"dissipation_factor", "insertion_loss", "contact_resistance", "dc_resistance",
          "temperature_coefficient", "coil_resistance"}
TYPE_B = {"mechanical_life", "q_factor", "insulation_resistance", "current_rating", "voltage_rating"}
TYPE_C = {"actuation_pressure", "capacitance_tolerance", "resistance_tolerance", "inductance_tolerance"}
TYPE_D = {"contact_plating", "shell_material", "dielectric_type"}
_BAND_TOLERANCE = {"resistance": "resistance_tolerance", "capacitance": "capacitance_tolerance",
                   "inductance": "inductance_tolerance"}
_FSC_BAND_PCT = {"5905": 1.0, "5910": 5.0, "5950": 10.0}
_FSC_HARD: dict[str, list[tuple[str, str]]] = {
    "5905": [("resistance", "band"), ("power_rating", "gte")],
    "5910": [("capacitance", "band"), ("voltage_rating", "gte")],
    "5915": [("frequency_range_max", "gte"), ("current_rating", "gte"), ("voltage_rating", "gte")],
    "5930": [("current_rating", "gte"), ("voltage_rating", "gte"), ("contact_configuration", "exact_text")],
    "5935": [("contact_count", "exact_num"), ("shell_size", "exact_num"),
             ("dielectric_withstanding_voltage", "gte"), ("coupling_type", "exact_text")],
    "5945": [("contact_rating", "gte"), ("coil_voltage", "exact_num"),
             ("contact_configuration", "exact_text"), ("sealing", "sealing")],
    "5950": [("inductance", "band"), ("current_rating", "gte")],
    "5961": [("voltage_rating", "gte"), ("current_rating", "gte")],
    "5999": [("current_rating", "gte"), ("contact_count", "exact_num"), ("insulation_resistance", "gte")],
}
_SEALING_RANK = {
    "none": 0, "unsealed": 0, "open": 0, "openframe": 0,
    "dust": 1, "dustproof": 1, "dusttight": 1,
    "splash": 2, "splashproof": 2,
    "panel": 3, "panelsealed": 3, "ip54": 3, "ip65": 3,
    "sealed": 4, "epoxy": 4, "epoxysealed": 4,
    "hermetic": 5, "hermeticallysealed": 5,
}


class Composite(NamedTuple):
    score: float | None
    weight_coverage_pct: float
    available_weight: float


def _clamp100(x: float) -> float:
    return max(0.0, min(100.0, x))


def _clean_token(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _has_gate_value(row: dict | None) -> bool:
    if row is None:
        return False
    if str(row.get("parse_status") or "") in ("unparsed", "empty"):
        return False
    if _f(row.get("value_num")) is not None:
        return True
    if _f(row.get("value_min")) is not None or _f(row.get("value_max")) is not None:
        return True
    return bool(str(row.get("value_text") or "").strip())


def _sealing_rank(text: str) -> int | None:
    c = _clean_token(text)
    if not c:
        return None
    for key, rank in sorted(_SEALING_RANK.items(), key=lambda kv: -len(kv[0])):
        if _clean_token(key) in c or c in _clean_token(key):
            return rank
    return None


def _text_match(required: str, candidate: str) -> bool:
    r, c = _clean_token(required), _clean_token(candidate)
    if not r or not c:
        return False
    if r == c:
        return True
    return (len(r) >= 3 and r in c) or (len(c) >= 3 and c in r)


def hard_gate_attrs(fsc: str) -> set[str]:
    attrs = {"operating_temp_min", "operating_temp_max"}
    if fsc != "5945":
        attrs.add("voltage_rating")
    attrs.update(a for a, _ in _FSC_HARD.get(fsc, []))
    return attrs


def _iter_hard_gates(fsc: str) -> list[tuple[str, str]]:
    out = [("operating_temp_min", "lte"), ("operating_temp_max", "gte")]
    if fsc != "5945":
        out.append(("voltage_rating", "gte"))
    out.extend(_FSC_HARD.get(fsc, []))
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for attr, kind in out:
        if attr in seen:
            continue
        seen.add(attr)
        uniq.append((attr, kind))
    return uniq


def evaluate_hard_gate(kind: str, req: dict, cand: dict | None, band_pct: float | None = None) -> str:
    """pass | fail | unverified. Missing candidate value is unverified — never a fail."""
    if not _has_gate_value(cand):
        return "unverified"
    assert cand is not None
    if kind == "gte":
        c = _f(cand.get("value_num"))
        if c is None and str(cand.get("parse_status")) == "range":
            c = _f(cand.get("value_max"))
        r = _f(req.get("value_num"))
        if c is None or r is None:
            return "unverified"
        return "pass" if c >= r else "fail"
    if kind == "lte":
        c = _f(cand.get("value_num"))
        if c is None and str(cand.get("parse_status")) == "range":
            c = _f(cand.get("value_min"))
        r = _f(req.get("value_num"))
        if c is None or r is None:
            return "unverified"
        return "pass" if c <= r else "fail"
    if kind == "exact_num":
        c, r = _f(cand.get("value_num")), _f(req.get("value_num"))
        if c is None or r is None:
            ct, rt = str(cand.get("value_text") or ""), str(req.get("value_text") or "")
            if not ct or not rt:
                return "unverified"
            return "pass" if _text_match(rt, ct) else "fail"
        return "pass" if abs(c - r) < 1e-9 else "fail"
    if kind == "exact_text":
        ct, rt = str(cand.get("value_text") or ""), str(req.get("value_text") or req.get("value") or "")
        if not ct or not rt:
            return "unverified"
        return "pass" if _text_match(rt, ct) else "fail"
    if kind == "band":
        c, r = _f(cand.get("value_num")), _f(req.get("value_num"))
        if c is None or r is None:
            return "unverified"
        if r == 0:
            return "pass" if abs(c) < 1e-12 else "fail"
        pct = band_pct if band_pct is not None else DEFAULT_PCT
        return "pass" if abs(c - r) / abs(r) * 100.0 <= pct + 1e-9 else "fail"
    if kind == "sealing":
        cr = _sealing_rank(str(cand.get("value_text") or ""))
        rr = _sealing_rank(str(req.get("value_text") or req.get("value") or ""))
        if cr is None or rr is None:
            return "unverified"
        return "pass" if cr >= rr else "fail"
    raise ValueError(f"unknown hard-gate kind {kind!r}")


def hard_gates_failed(fsc: str, reqs_by_name: dict[str, dict], cand_attrs: dict[str, dict],
                      band_pcts: dict[str, float] | None = None) -> list[str]:
    """Names of hard gates the candidate actually violates, in gate-table order."""
    failed: list[str] = []
    for attr, kind in _iter_hard_gates(fsc):
        req = reqs_by_name.get(attr)
        if req is None:
            continue
        pct = None
        if kind == "band":
            if band_pcts and attr in band_pcts:
                pct = band_pcts[attr]
            else:
                tol = _BAND_TOLERANCE.get(attr)
                if tol and tol in reqs_by_name:
                    pct = _f(reqs_by_name[tol].get("value_num"))
                if pct is None:
                    pct = _FSC_BAND_PCT.get(fsc, DEFAULT_PCT)
        if evaluate_hard_gate(kind, req, cand_attrs.get(attr), pct) == "fail":
            failed.append(attr)
    return failed


def _type_d_risk(attr: str, raw: str) -> float:
    c = _clean_token(raw)
    if attr == "contact_plating":
        for key, score in (("gold", 0.0), ("silver", 20.0), ("tin", 45.0)):
            if key.upper() in c:
                return score
        return 80.0
    if attr == "shell_material":
        if "STAINLESS" in c:
            return 20.0
        if "COMPOSITE" in c:
            return 10.0
        if "ALUMINUM" in c or "ALUMINIUM" in c:
            return 30.0
        return 75.0
    if attr == "dielectric_type":
        if "C0G" in c or "NP0" in c:
            return 0.0
        if "X7R" in c:
            return 30.0
        if "Z5U" in c or "Y5V" in c:
            return 70.0
        return 85.0
    return 80.0


def attr_soft_risk(attr: str, value, original) -> float | None:
    """Per-attribute composite risk 0-100, or None when the formula cannot run."""
    if attr == "lead_time":
        if value is None or value == "":
            return None
        return _clamp100(float(value) / 180.0 * 100.0)
    if attr == "unit_cost":
        if value is None or value == "" or original is None or original == "":
            return None
        hist = float(original)
        if hist == 0:
            return None
        return _clamp100(50.0 + (float(value) - hist) / hist * 100.0)
    if attr in TYPE_D:
        if value is None or value == "":
            return None
        return _type_d_risk(attr, str(value))
    if value is None or value == "" or original is None or original == "":
        return None
    try:
        orig = float(original)
        val = float(value)
    except (TypeError, ValueError):
        return None
    if orig == 0:
        return None
    if attr in TYPE_A:
        denom = orig * 1.20 - orig
        if denom == 0:
            return None
        return _clamp100((val - orig) / denom * 100.0)
    if attr in TYPE_B:
        denom = orig - orig * 0.80
        if denom == 0:
            return None
        return _clamp100((orig - val) / denom * 100.0)
    if attr in TYPE_C:
        return _clamp100(abs(val - orig) / orig * 500.0)
    return None


def composite(fsc: str, candidate_values: dict, original_values: dict,
              lead_time_days=None, candidate_cost=None, historical_cost=None) -> Composite:
    weighted = 0.0
    available = 0.0
    lt = attr_soft_risk("lead_time", lead_time_days, None)
    if lt is not None:
        w = UNIVERSAL_WEIGHTS["lead_time"]
        weighted += w * lt
        available += w
    uc = attr_soft_risk("unit_cost", candidate_cost, historical_cost)
    if uc is not None:
        w = UNIVERSAL_WEIGHTS["unit_cost"]
        weighted += w * uc
        available += w
    for attr, w in CATEGORY_WEIGHTS.get(fsc, {}).items():
        risk = attr_soft_risk(attr, candidate_values.get(attr), original_values.get(attr))
        if risk is None:
            continue
        weighted += w * risk
        available += w
    if available == 0:
        return Composite(None, 0.0, 0.0)
    return Composite(weighted / available, available / 100.0 * 100.0, available)


def risk_from_composite(score: float | None, gates_failed: list[str]) -> str:
    if gates_failed:
        return "high"
    if score is None:
        return "unscored"
    if score <= 33:
        return "low"
    if score <= 66:
        return "medium"
    return "high"


def gate_type(fsc: str, requirement_name: str, requirement_class: str = "") -> str:
    if (requirement_class or "") in BURDEN_CLASSES or requirement_name == "qualification":
        return "burden"
    if requirement_name in hard_gate_attrs(fsc):
        return "hard"
    return "soft"


def _soft_value(row: dict | None):
    """Numeric if present, else text — None when the row has nothing usable."""
    if row is None or str(row.get("parse_status") or "") in ("unparsed", "empty"):
        return None
    n = _f(row.get("value_num"))
    if n is not None:
        return n
    t = str(row.get("value_text") or "").strip()
    return t or None


# ------------------------------------------------------------------ QPL
def _pn_key(pn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(pn or "").upper())


def build_qpl_index(qpl: pd.DataFrame | None) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """spec_key -> {normalised qualified_part_number -> [(cage_code, normalised manufacturer_name), ...]}

    Keyed by part number FIRST because the join is a PAIR (SCHEMA §3): part number AND cage, or part number
    AND manufacturer name. Nothing in this index lets a caller match on manufacturer alone or on part
    number alone."""
    idx: dict[str, dict[str, list[tuple[str, str]]]] = {}
    if qpl is None or len(qpl) == 0:
        return idx
    for _, r in qpl.iterrows():
        k = spec_key(r.get("governing_spec", ""))
        pn = _pn_key(r.get("qualified_part_number", ""))
        if not k or not pn:
            continue
        idx.setdefault(k, {}).setdefault(pn, []).append(
            (str(r.get("cage_code", "") or "").strip().upper(), norm_key(r.get("manufacturer_name", "") or "")))
    return idx


def _mfr_match(a: str, b: str) -> bool:
    """Normalised manufacturer names match exactly or one contains the other ('vishaydale' ~ 'vishay')."""
    if not a or not b:
        return False
    return a == b or (len(a) > 3 and len(b) > 3 and (a in b or b in a))


def qpl_listed(idx: dict, spec_ref: str, manufacturer: str, mpn: str, cage: str = "") -> tuple[bool, str]:
    """SCHEMA §3: listed iff a qpl row for the item's governing spec has qualified_part_number == mpn AND
    (cage_code == cage OR manufacturer_name ~ manufacturer). Part number alone is forbidden (MPNs are not
    unique across manufacturers); manufacturer alone is not a qualification of THIS part. When the join
    cannot be made confidently the answer is False — never null coerced to true."""
    k = spec_key(spec_ref)
    if not spec_ref:
        return False, "no governing specification on the federal item"
    if not idx:
        return False, "no QPL data available"
    slot = idx.get(k)
    if not slot:
        return False, f"no QPL entries loaded for spec {spec_ref}"
    pn = _pn_key(mpn)
    entries = slot.get(pn) if pn else None
    if not entries:
        return False, f"part number {mpn} is not on the QPL for {spec_ref}"
    cage_k = str(cage or "").strip().upper()
    mk = norm_key(manufacturer or "")
    for q_cage, q_mfr in entries:
        if cage_k and q_cage and cage_k == q_cage:
            return True, f"part number {mpn} is QPL-listed against {spec_ref} (CAGE {q_cage})"
        if _mfr_match(mk, q_mfr):
            return True, f"part number {mpn} is QPL-listed against {spec_ref} (manufacturer {manufacturer})"
    return False, (f"part number {mpn} is on the QPL for {spec_ref} but under a different manufacturer/CAGE — "
                   f"not treated as listed")


# ------------------------------------------------------------------ rollup
def rollup(verdicts: list[tuple[str, str, str]], is_qpl: bool, qpl_note: str,
           enrichment: dict | None = None, spec_ref: str = "") -> tuple[str, str, dict]:
    """
    verdicts: [(requirement_name, requirement_class, verdict)]
    Counts and risk use scored classes only (electrical/mechanical/environmental).
    Qualification/traceability are a burden, not a verdict. `is_qpl` / `qpl_note` are kept
    for the qpl_listed column and are not part of the rollup.
    Returns (risk_level, rationale, counts)
    """
    scored = [(n, c, v) for n, c, v in verdicts if is_scored_class(c)]
    burden_names = sorted({n for n, c, _ in verdicts if c in BURDEN_CLASSES})
    counts = {"pass": 0, "fail": 0, "marginal": 0, "unknown": 0}
    for _, _, v in scored:
        counts[v] += 1
    total = len(scored)
    fails = [(n, c) for n, c, v in scored if v == "fail"]
    unknown_ratio = (counts["unknown"] / total) if total else 1.0
    marginal_ratio = (counts["marginal"] / total) if total else 0.0

    extra: list[str] = []
    if total == 0:
        risk = "medium"
        extra.append("no measurable requirements available to evaluate — nothing is known about fit")
    elif counts["fail"] > 2:
        risk = "high"
    elif counts["fail"] >= 1:
        risk = "medium"
    elif unknown_ratio > UNKNOWN_RATIO_MEDIUM:
        risk = "medium"
        extra.append(f"{counts['unknown']} of {total} measurable requirements could not be verified ({unknown_ratio:.0%} unknown)")
    elif marginal_ratio > MARGINAL_RATIO_MEDIUM:
        risk = "medium"
        extra.append(f"{counts['marginal']} of {total} measurable requirements only marginally met ({marginal_ratio:.0%} marginal)")
    else:
        risk = "low"

    # second pass: lifecycle / availability from S7 (lifecycle_status is the closed enum from SCHEMA §8)
    if enrichment:
        life = enrichment.get("lifecycle_status") or "unknown"
        dist = _f(enrichment.get("distributor_count"))
        if life == "obsolete":
            risk = "high"
            extra.append("lifecycle status is obsolete")
        elif life == "nrnd":
            risk = "medium" if RISK_ORDER[risk] < RISK_ORDER["medium"] else risk
            extra.append("lifecycle status is NRND")
        elif life == "unknown":
            extra.append("lifecycle status unknown")
        if dist is not None and dist <= 1:
            risk = "medium" if RISK_ORDER[risk] < RISK_ORDER["medium"] else risk
            extra.append("no distributor has stock" if dist == 0 else "single-sourced (1 distributor)")
        elif dist is not None:
            extra.append(f"{int(dist)} distributors")
    else:
        extra.append("lifecycle/availability unknown (not enriched)")

    summary = f"Meets {counts['pass']} of {total} measurable requirements"
    if fails:
        summary += f"; fails {', '.join(n for n, _ in fails)}"
    if counts["unknown"]:
        summary += f"; {counts['unknown']} unknown"
    if counts["marginal"]:
        summary += f"; {counts['marginal']} marginal"
    gap_n = len(burden_names)
    spec_bit = f" under {spec_ref}" if spec_ref else ""
    burden = f"{gap_n} qualification clauses{spec_bit} require testing to verify."
    rationale = f"{summary}. {burden}"
    if extra:
        rationale += " " + "; ".join(extra)
    return risk, rationale, counts


def baseline_prices(ph: pd.DataFrame | None, logger=None) -> dict[str, tuple[float, float | None, int | None]]:
    """nsn -> (unit_price_avg, quantity, fiscal_year) of the most recent fiscal year with a non-null price.
    quantity is None when S10 could not derive one (USAspending rarely states it)."""
    out: dict[str, tuple[float, float | None, int | None]] = {}
    if ph is None or len(ph) == 0:
        return out
    df = ph.copy()
    df["_price"] = pd.to_numeric(df["unit_price_avg"], errors="coerce")
    df["_fy"] = pd.to_numeric(df["fiscal_year"], errors="coerce")
    df["_qty"] = pd.to_numeric(df.get("quantity", pd.Series([""] * len(df))), errors="coerce")
    df = df.dropna(subset=["_price", "_fy"]).sort_values(["nsn", "_fy"])
    for nsn, grp in df.groupby("nsn"):
        last = grp.iloc[-1]
        qty = None if pd.isna(last["_qty"]) else float(last["_qty"])
        out[nsn] = (float(last["_price"]), qty, int(last["_fy"]))
    if logger is not None:
        logger.info("government unit prices available for %d of %d NSN(s) in price_history", len(out), ph["nsn"].nunique())
    return out


def price_delta(gov_unit_price: float | None, commercial_unit_price: float | None) -> str:
    """price_delta_indicative = gov_unit_price - commercial_unit_price (SCHEMA §3). INDICATIVE: the two prices
    sit on different quantity and qualification bases, so this measures mostly quantity and qualification,
    not savings. Null ('') when either side is missing — never defaulted to zero."""
    if gov_unit_price is None or commercial_unit_price is None:
        return ""
    return f"{gov_unit_price - commercial_unit_price:.4f}"


def rank_candidates(sc: pd.DataFrame) -> pd.DataFrame:
    """Deterministic ranking within each NSN: risk_rank ascending, then composite_score ascending
    (nulls last). Remaining keys (price_delta descending, fail/unknown/pass/marginal, mpn) break ties
    only and never cross a risk band."""
    df = sc.copy()
    for c in ("pass_count", "fail_count", "marginal_count", "unknown_count"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    df["_risk"] = df["risk_level"].map(RISK_ORDER).fillna(9)
    if "composite_score" in df.columns:
        df["_comp"] = pd.to_numeric(df["composite_score"], errors="coerce").fillna(float("inf"))
    else:
        df["_comp"] = 0.0
    delta = pd.to_numeric(df["price_delta_indicative"], errors="coerce")
    df["_neg_delta"] = (-delta).fillna(float("inf"))
    df["_neg_pass"] = -df["pass_count"] if "pass_count" in df.columns else 0
    df = df.sort_values(["nsn", "_risk", "_comp", "_neg_delta", "fail_count", "unknown_count", "_neg_pass",
                         "marginal_count", "candidate_mpn"])
    df["rank_within_nsn"] = df.groupby("nsn").cumcount() + 1
    return df.drop(columns=["_risk", "_comp", "_neg_delta", "_neg_pass"])


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)

    if not (DATA_DIR / "requirement_profiles_normalized.csv").exists() and (DATA_DIR / "requirement_profiles.csv").exists():
        raise SystemExit("data/requirement_profiles.csv exists but is not normalised — run scripts/s08_normalize.py first.")
    prof = read_data_csv("requirement_profiles_normalized.csv", logger=logger)
    cands = read_data_csv("candidates_normalized.csv", logger=logger)
    items = read_data_csv("federal_items.csv", required=False, logger=logger)
    qpl = read_data_csv("qpl.csv", required=False, logger=logger)
    cp = read_data_csv("commercial_parts_normalized.csv", required=False, logger=logger)
    ph = read_data_csv("price_history.csv", required=False, logger=logger)
    fsc_map = load_fsc_map(logger)

    if qpl is None:
        logger.warning("data/qpl.csv not present — qpl_listed will be false for every candidate (does not gate risk)")
    if cp is None:
        logger.info("first pass: no commercial_parts_normalized.csv — lifecycle/availability not considered, "
                    "commercial_unit_price / price_delta_indicative will be null")
    else:
        logger.info("second pass: incorporating lifecycle/availability for %d enriched MPN(s)", len(cp))
    if ph is None:
        logger.warning("data/price_history.csv not present (S10) — gov_unit_price / price_delta_indicative will be null "
                       "for every candidate")
    baseline = baseline_prices(ph, logger)

    spec_of = dict(zip(items["nsn"], items["governing_spec_ref"])) if items is not None else {}
    fsc_of = dict(zip(items["nsn"], items["fsc"])) if items is not None else {}
    pct_rules: dict[str, dict[str, float]] = {}
    for _, r in fsc_map.iterrows():
        rules = parse_rules(r["tolerance_rules"])
        pct_rules[r["fsc"]] = {a: float(arg) for a, (rule, arg) in rules.items() if rule == "pct" and arg}
    qpl_idx = build_qpl_index(qpl)
    enrich = {r["mpn"].upper(): r.to_dict() for _, r in cp.iterrows()} if cp is not None else {}

    # candidate attribute lookup: (nsn, mpn) -> {attr: row}
    cand_attrs: dict[tuple[str, str], dict[str, dict]] = {}
    cand_meta: dict[tuple[str, str], dict] = {}
    for _, r in cands.iterrows():
        key = (r["nsn"], r["candidate_mpn"])
        cand_attrs.setdefault(key, {})
        # keep the first parsed value if an attribute appears twice
        if r["attribute_name"] not in cand_attrs[key] or cand_attrs[key][r["attribute_name"]]["parse_status"] in ("unparsed", "empty"):
            cand_attrs[key][r["attribute_name"]] = r.to_dict()
        cand_meta.setdefault(key, {"manufacturer": r.get("manufacturer", ""), "digikey_pn": r.get("digikey_pn", ""),
                                   "source": r.get("source", "") or "digikey"})
    log_rows(logger, "(nsn, candidate) pairs", len(cand_attrs))

    prof_by_nsn: dict[str, list[dict]] = {}
    for _, r in prof.iterrows():
        prof_by_nsn.setdefault(r["nsn"], []).append(r.to_dict())
    items_without_profile = {n for n, _ in cand_attrs} - set(prof_by_nsn)
    if items_without_profile:
        logger.warning("%d item(s) with candidates but no requirement profile — their candidates score as all-unknown: %s",
                       len(items_without_profile), ", ".join(sorted(items_without_profile)[:10]))
    items_without_cands = set(prof_by_nsn) - {n for n, _ in cand_attrs}
    if items_without_cands:
        logger.warning("%d item(s) with a profile but no candidates: %s", len(items_without_cands),
                       ", ".join(sorted(items_without_cands)[:10]))

    sc_rows: list[dict] = []
    sd_rows: list[dict] = []
    for (nsn, mpn), attrs in sorted(cand_attrs.items()):
        cid = candidate_id(nsn, mpn)
        reqs = prof_by_nsn.get(nsn, [])
        fsc = fsc_of.get(nsn) or nsn[:4]
        pcts = pct_rules.get(fsc, {})
        verdicts: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        meta = cand_meta[(nsn, mpn)]
        # Digi-Key / Nexar return no CAGE code, so the pair join runs on (part number, manufacturer name)
        listed, note = qpl_listed(qpl_idx, spec_of.get(nsn, ""), meta["manufacturer"], mpn, cage=meta.get("cage_code", ""))
        reqs_by_name: dict[str, dict] = {}
        for req in reqs:
            name = req["requirement_name"]
            if name in seen:
                continue  # spec wins in S5; a duplicate here would double count
            seen.add(name)
            reqs_by_name[name] = req
            rclass = infer_class(name, req.get("requirement_class", ""))
            if name == "qualification":
                d = qualification_delta(req, listed)
            else:
                d = compare(req, attrs.get(name), pcts.get(name))
                if rclass in BURDEN_CLASSES:
                    # not verifiable from commercial data ≠ does not meet
                    d = d._replace(verdict="unknown")
            scored = is_scored_class(rclass)
            verdicts.append((name, rclass, d.verdict))
            sd_rows.append({"delta_id": delta_id(cid, name), "candidate_id": cid, "nsn": nsn, "candidate_mpn": mpn,
                            "requirement_name": name, "required_value": d.required_bare,
                            "candidate_value": d.candidate_bare, "verdict": d.verdict,
                            "scored": str(scored).lower(),
                            "gate_type": gate_type(fsc, name, rclass),
                            "operator": req.get("operator", ""), "uom": d.uom, "required_value_display": d.required,
                            "candidate_value_display": d.candidate, "requirement_class": rclass,
                            "source": req.get("source", ""), "source_spec_id": req.get("source_spec_id", "") or ""})
        enr = enrich.get(mpn.upper())
        spec_ref = spec_of.get(nsn, "")
        gap_count, gap_summary = qualification_gap(reqs)
        gov_price, gov_qty, gov_fy = baseline.get(nsn, (None, None, None))
        com_price = _f(enr.get("median_price")) if enr else None
        com_basis = _f(enr.get("price_basis_qty")) if enr else None
        failed = hard_gates_failed(fsc, reqs_by_name, attrs, pcts)
        if failed:
            score, cov = 100.0, 0.0
        else:
            cand_vals = {k: _soft_value(v) for k, v in attrs.items()}
            orig_vals = {k: _soft_value(v) for k, v in reqs_by_name.items()}
            comp = composite(fsc, cand_vals, orig_vals,
                             lead_time_days=_f(enr.get("lead_time_days")) if enr else None,
                             candidate_cost=com_price, historical_cost=gov_price)
            score, cov = comp.score, comp.weight_coverage_pct
        risk = risk_from_composite(100.0 if failed else score, failed)
        _, rollup_rationale, counts = rollup(verdicts, listed, note, enr, spec_ref=spec_ref)
        if enr:
            life = enr.get("lifecycle_status") or "unknown"
            dist = _f(enr.get("distributor_count"))
            if life == "obsolete":
                risk = "high"
            elif life == "nrnd" and RISK_ORDER[risk] < RISK_ORDER["medium"]:
                risk = "medium"
            if dist is not None and dist <= 1 and RISK_ORDER[risk] < RISK_ORDER["medium"]:
                risk = "medium"
        if failed:
            head = f"Hard gate failure: {'|'.join(failed)}. composite skipped (100)."
        elif score is None:
            head = "No scoreable composite attributes (weight coverage 0%); insufficient data to score."
        else:
            head = f"Composite {score:.1f} (coverage {cov:.0f}%)."
        rationale = f"{head} {rollup_rationale}"
        sc_rows.append({"candidate_id": cid, "nsn": nsn, "candidate_mpn": mpn, "risk_level": risk,
                        "risk_rank": risk_rank(risk),
                        "pass_count": counts["pass"], "fail_count": counts["fail"], "marginal_count": counts["marginal"],
                        "unknown_count": counts["unknown"], "qpl_listed": str(listed).lower(),
                        "gov_unit_price": bare(gov_price), "gov_quantity": bare(gov_qty),
                        "commercial_unit_price": bare(com_price), "commercial_basis_qty": bare(com_basis),
                        "price_delta_indicative": price_delta(gov_price, com_price), "rationale": rationale,
                        "qualification_gap_count": gap_count, "qualification_gap_summary": gap_summary,
                        "composite_score": "" if (not failed and score is None) else f"{score:.6g}",
                        "weight_coverage_pct": f"{cov:.6g}",
                        "gates_failed": "|".join(failed),
                        "country_of_origin": "unknown",
                        "manufacturer": meta["manufacturer"], "digikey_pn": meta["digikey_pn"],
                        "governing_spec_ref": spec_ref, "enriched": str(enr is not None).lower(),
                        "source": meta.get("source", "") or "digikey",
                        # SCHEMA §8 puts these on commercial_parts; they are repeated here (non-contract columns)
                        # so an unenriched candidate visibly reads unknown / null rather than silently omitting them
                        "lifecycle_status": (enr.get("lifecycle_status") or "unknown") if enr else "unknown",
                        "distributor_count": bare(_f(enr.get("distributor_count"))) if enr else "",
                        "gov_price_fiscal_year": "" if gov_fy is None else str(gov_fy)})

    sc = rank_candidates(pd.DataFrame(sc_rows, columns=SC_COLS + [
        "qualification_gap_count", "qualification_gap_summary", "source",
        "composite_score", "weight_coverage_pct", "gates_failed", "country_of_origin",
        "manufacturer", "digikey_pn", "governing_spec_ref",
        "enriched", "lifecycle_status", "distributor_count", "gov_price_fiscal_year"]))
    sd = pd.DataFrame(sd_rows, columns=SD_COLS + ["scored", "gate_type", "operator", "uom", "required_value_display",
                                                  "candidate_value_display", "requirement_class", "source",
                                                  "source_spec_id"])
    if sc["candidate_id"].duplicated().any():
        raise SystemExit("candidate_id is not unique — this should be impossible; inspect candidates_normalized.csv")
    if sd["delta_id"].duplicated().any():
        raise SystemExit("delta_id is not unique — a requirement_name repeats within a candidate")
    write_data_csv(sc, "substitution_candidates.csv", SC_COLS, logger)
    write_data_csv(sd, "spec_deltas.csv", SD_COLS, logger)
    if len(sc):
        logger.info("risk distribution: %s", sc["risk_level"].value_counts().to_dict())
        if "source" in sc.columns:
            for src, grp in sc.groupby("source"):
                logger.info("risk by source=%s: %s (%d candidates)", src, grp["risk_level"].value_counts().to_dict(),
                            len(grp))
        if "qualification_gap_count" in sc.columns:
            logger.info("qualification_gap_count distribution (per candidate): %s",
                        sc["qualification_gap_count"].value_counts().sort_index().to_dict())
        logger.info("verdict distribution: %s", sd["verdict"].value_counts().to_dict() if len(sd) else {})
        logger.info("price_delta_indicative populated for %d of %d candidates (gov price for %d, commercial price for %d)",
                    (sc["price_delta_indicative"] != "").sum(), len(sc), (sc["gov_unit_price"] != "").sum(),
                    (sc["commercial_unit_price"] != "").sum())
        if "gates_failed" in sc.columns:
            n_gate = (sc["gates_failed"] != "").sum()
            logger.info("hard-gate failures: %d of %d candidates", n_gate, len(sc))
        if "composite_score" in sc.columns:
            cs = pd.to_numeric(sc["composite_score"], errors="coerce")
            logger.info("composite_score null=%d  min=%s median=%s max=%s",
                        int(cs.isna().sum()),
                        f"{cs.min():.1f}" if cs.notna().any() else "n/a",
                        f"{cs.median():.1f}" if cs.notna().any() else "n/a",
                        f"{cs.max():.1f}" if cs.notna().any() else "n/a")
        if "weight_coverage_pct" in sc.columns:
            logger.info("weight_coverage_pct distribution: %s",
                        pd.to_numeric(sc["weight_coverage_pct"], errors="coerce").value_counts().sort_index().to_dict())
        if "gate_type" in sd.columns:
            logger.info("gate_type distribution: %s", sd["gate_type"].value_counts().to_dict())
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
