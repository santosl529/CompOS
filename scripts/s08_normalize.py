#!/usr/bin/env python
"""
S8 — normalize.py

Unit conversion, range parsing, package-code alignment and tolerance normalisation across
every attribute source, so that S9 compares numbers with numbers.

Inputs  (whichever exist):
  data/requirement_profiles.csv   requirement_id, nsn, requirement_name, operator, value, uom, requirement_class,
                                  source, source_spec_id
  data/candidates.csv             nsn, candidate_mpn, manufacturer, digikey_pn, raw_attributes_json
  data/candidates_external.csv    nsn, candidate_mpn, manufacturer, attribute_name, attribute_value, uom
                                  (long format; same vocabulary as the spec; tagged source=external)
  data/commercial_parts.csv       mpn, manufacturer, lifecycle_status, median_price, stock_qty, ...
Outputs:
  data/requirement_profiles_normalized.csv
  data/candidates_normalized.csv        (long format: one row per candidate attribute)
  data/commercial_parts_normalized.csv

Every numeric value is converted to a base unit (ohm, F, V, A, W, degC, %, ppm/degC, H, Hz, s, mm)
and stored in value_num, or value_min/value_max for ranges. `operating_temp` ranges are split
into `operating_temp_min` and `operating_temp_max`. Text attributes (package, dielectric,
mounting) are upper-cased with punctuation removed. Anything unparseable keeps its raw text
and is marked parse_status=unparsed — it will score as `unknown`, never `pass`.

Pure functions in this module are imported by tests/ and by S9.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Aliases, DroppedRows, norm_key, read_data_csv, setup_logging, write_data_csv,
)

SCRIPT = "s08_normalize"

# ------------------------------------------------------------------ vocab
NUMERIC_ATTRS = {
    "resistance": "ohm", "capacitance": "F", "inductance": "H", "voltage_rating": "V",
    "current_rating": "A", "power_rating": "W", "operating_temp": "degC", "operating_temp_min": "degC",
    "operating_temp_max": "degC", "tolerance": "%", "temperature_coefficient": "ppm/degC",
    "dielectric_withstanding_voltage": "V", "insulation_resistance": "ohm", "frequency": "Hz",
    "junction_temp": "degC", "junction_temp_min": "degC", "junction_temp_max": "degC",
}
TEXT_ATTRS = {"package_case", "dielectric", "dielectric_type", "mounting_type", "composition", "terminal_type",
              "qualification"}
# "-55°C ~ 125°C" style values that split into <attr>_min / <attr>_max requirements
RANGE_ATTRS = {"operating_temp", "junction_temp"}

# MIL-DTL-38999 shell-size designators (Series III, Table I): letter -> shell size number. Digi-Key's
# "Shell Size, MIL" filter uses the letters; PUB LOG and MIL-DTL-5015-style sources use the numbers.
SHELL_SIZE_LETTERS = {"A": 9, "B": 11, "C": 13, "D": 15, "E": 17, "F": 19, "G": 21, "H": 23, "J": 25}

# Closed operator enum (SCHEMA.txt §8). The pipeline emits only these; anything else is a contract violation.
OPERATORS = {"eq", "gte", "lte", "range", "in_set", "boolean"}
# Legacy / human spellings accepted on INPUT (overrides, hand-made files, LLM output) and mapped to the enum.
OPERATOR_ALIASES = {
    "eq": "eq", "==": "eq", "=": "eq", "equal": "eq", "equals": "eq",
    "gte": "gte", ">=": "gte", ">": "gte", "min": "gte", "minimum": "gte", "at_least": "gte",
    "lte": "lte", "<=": "lte", "<": "lte", "max": "lte", "maximum": "lte", "at_most": "lte",
    "range": "range", "between": "range",
    "in_set": "in_set", "in": "in_set", "one_of": "in_set",
    "boolean": "boolean", "bool": "boolean", "flag": "boolean",
}


def canonical_operator(given: str) -> str | None:
    """Map any accepted spelling to the closed enum; None when unrecognised (caller decides what to do)."""
    return OPERATOR_ALIASES.get((given or "").strip().lower())


# Attributes whose PUB LOG / spec requirement, when no operator is given, is a rating floor/ceiling.
DEFAULT_OPERATORS = {
    "power_rating": "gte", "voltage_rating": "gte", "current_rating": "gte", "operating_temp_max": "gte",
    "dielectric_withstanding_voltage": "gte", "insulation_resistance": "gte",
    "tolerance": "lte", "resistance_tolerance": "lte", "capacitance_tolerance": "lte", "inductance_tolerance": "lte",
    "operating_temp_min": "lte", "temperature_coefficient": "lte", "junction_temp_max": "gte", "junction_temp_min": "lte",
    "contact_rating": "gte", "coil_voltage": "eq", "dc_resistance": "lte", "insertion_loss": "gte",
    "q_factor": "gte", "contact_resistance": "lte", "dissipation_factor": "lte", "mechanical_life": "gte",
    "thermal_shock_cycles": "gte", "vibration_grms": "gte", "frequency_range_max": "gte", "current_saturation": "gte",
    "switching_voltage": "gte", "shock_g": "gte",
}

# canonical uom -> (unit family, multiplier to base)
UNIT_TABLE: dict[str, tuple[str, float]] = {}


PREFIXES = {
    "p": 1e-12, "pico": 1e-12, "n": 1e-9, "nano": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "micro": 1e-6,
    "m": 1e-3, "milli": 1e-3, "k": 1e3, "K": 1e3, "kilo": 1e3, "M": 1e6, "meg": 1e6, "mega": 1e6,
    "G": 1e9, "giga": 1e9,
}


def _add_units(family: str, base_names: list[str]) -> None:
    """Register base unit spellings plus every prefixed form. Single-letter prefixes keep their case
    ('m' = milli, 'M' = mega); word prefixes and unit names are stored lower-case."""
    for name in base_names:
        UNIT_TABLE[name.lower()] = (family, 1.0)
        for pfx, mult in PREFIXES.items():
            key = pfx + name.lower() if len(pfx) == 1 else (pfx + name).lower()
            UNIT_TABLE[key] = (family, mult)


# Lookup order in _unit_lookup: exact token, then first-char-preserved + rest lower-cased ('MOhms' -> 'Mohms'),
# then fully lower-cased. So 'MOhms' = mega and 'mOhms' = milli, while 'KILOHMS' / 'MEGOHMS' work via words.
_add_units("ohm", ["ohm", "ohms", "Ω", "Ω"])
_add_units("F", ["f", "farad", "farads"])
_add_units("H", ["h", "henry", "henries", "henrys"])
_add_units("V", ["v", "volt", "volts", "vdc", "vac"])
_add_units("A", ["a", "amp", "amps", "ampere", "amperes"])
_add_units("W", ["w", "watt", "watts"])
_add_units("Hz", ["hz", "hertz"])
_add_units("s", ["s", "sec", "second", "seconds"])
_add_units("m", ["m", "meter", "meters", "metre", "metres"])
UNIT_TABLE.update({
    "%": ("%", 1.0), "percent": ("%", 1.0), "pct": ("%", 1.0),
    "degc": ("degC", 1.0), "°c": ("degC", 1.0), "c": ("degC", 1.0), "deg c": ("degC", 1.0),
    "deg celsius": ("degC", 1.0), "celsius": ("degC", 1.0), "deg. c": ("degC", 1.0),
    "ppm/°c": ("ppm/degC", 1.0), "ppm/degc": ("ppm/degC", 1.0), "ppm/c": ("ppm/degC", 1.0),
    "ppm/deg c": ("ppm/degC", 1.0), "ppm": ("ppm/degC", 1.0), "ppm_per_c": ("ppm/degC", 1.0),
    "in": ("mm", 25.4), "inch": ("mm", 25.4), "inches": ("mm", 25.4), "mm": ("mm", 1.0), "millimeter": ("mm", 1.0),
    "millimeters": ("mm", 1.0), "cm": ("mm", 10.0),
    "megohm": ("ohm", 1e6), "megohms": ("ohm", 1e6), "kilohm": ("ohm", 1e3), "kilohms": ("ohm", 1e3),
    "milliohm": ("ohm", 1e-3), "milliohms": ("ohm", 1e-3),
})

FAMILY_OF_ATTR = {attr: fam for attr, fam in NUMERIC_ATTRS.items()}

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_FRACTION = re.compile(rf"^({_NUM})\s*/\s*({_NUM})\s*([A-Za-zµμΩΩ°%/. ]*)$")
_NUM_UNIT = re.compile(rf"^(±|\+/-|\+/−|plus or minus)?\s*({_NUM})\s*([A-Za-zµμΩΩ°%][A-Za-zµμΩΩ°%/._ ]*)?$")
_RANGE = re.compile(
    rf"^({_NUM})\s*([A-Za-zµμΩΩ°%][A-Za-zµμΩΩ°%/. ]*?)?\s*(?:~|\bto\b|\bthru\b|\bthrough\b|\band\b|\.\.|\||/|–|—|-)\s*({_NUM})\s*([A-Za-zµμΩΩ°%][A-Za-zµμΩΩ°%/. ]*)?$",
    re.IGNORECASE,
)
_SIGNED_FRACTION = re.compile(rf"^[-+]|/\s*[-+]")
_PAREN = re.compile(r"\s*\([^)]*\)")
_QUAL_WORDS = re.compile(r"\b(nominal|nom\.?|maximum|max\.?|minimum|min\.?|rated|typical|typ\.?)\b", re.IGNORECASE)


@dataclass
class Norm:
    value_num: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    uom: str = ""
    value_text: str = ""
    kind: str = "empty"  # number | range | text | set | boolean | empty | unparsed

    def as_row(self) -> dict:
        d = asdict(self)
        for k in ("value_num", "value_min", "value_max"):
            d[k] = "" if d[k] is None else repr(float(d[k]))
        return d


def bare_number(v: float | None) -> str:
    return "" if v is None else f"{float(v):.10g}"


def bare_value(row: dict) -> str:
    """The contract form of a normalised value: base-unit number, 'min|max' for a range (SCHEMA.txt §8),
    normalised text, or '' when unparsed/empty. `row` needs parse_status, value_num, value_min, value_max,
    value_text. Accepts either a CSV row (parse_status) or Norm.as_row() (kind)."""
    st = row.get("parse_status") or row.get("kind") or ""

    def f(x):
        try:
            return None if x in (None, "") else float(x)
        except (TypeError, ValueError):
            return None

    if st == "range":
        lo, hi = f(row.get("value_min")), f(row.get("value_max"))
        return f"{bare_number(lo)}|{bare_number(hi)}" if lo is not None and hi is not None else ""
    if st == "number":
        return bare_number(f(row.get("value_num")))
    if st in ("text", "set", "boolean"):   # set: pipe-delimited normalised members; boolean: true|false
        return row.get("value_text", "") or ""
    return ""


def _unit_lookup(token: str) -> tuple[str, float] | None:
    t = (token or "").strip().replace("Ω", "ohm").replace("Ω", "ohm")
    if not t:
        return None
    for cand in (t, t[0] + t[1:].lower(), t.lower(), re.sub(r"\s+", " ", t.lower().replace(".", "").strip())):
        if cand in UNIT_TABLE:
            return UNIT_TABLE[cand]
    return None


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace("−", "-"))
    except (TypeError, ValueError):
        return None


def _apply_unit(num: float, unit_tok: str | None, attr: str | None) -> tuple[float, str] | None:
    """Return (value in base unit, canonical uom) or None if the unit is unknown / incompatible."""
    expected = FAMILY_OF_ATTR.get(attr or "")
    if not unit_tok or not unit_tok.strip():
        return (num, expected or "")
    found = _unit_lookup(unit_tok)
    if found is None:
        # tolerate trailing noise like "W (1/8)" or "V DC"
        first = unit_tok.strip().split()[0]
        found = _unit_lookup(first)
    if found is None:
        return None
    fam, mult = found
    if expected and fam != expected:
        # a temperature attr given as bare 'C' etc is handled by the table; genuine mismatch -> unparseable
        return None
    return (num * mult, fam)


def clean_text(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def normalize_package(raw: str) -> str:
    """'0603 (1608 Metric)' -> '0603'; 'Axial' -> 'AXIAL'; 'Radial, Can' -> 'RADIALCAN'."""
    s = _PAREN.sub("", str(raw or "")).strip()
    return clean_text(s)


_TRUE = {"true", "yes", "y", "required", "1", "t", "x"}
_FALSE = {"false", "no", "n", "not required", "0", "f"}


def normalize_set(raw: str, attr: str | None = None) -> Norm:
    """`in_set` requirement: pipe-delimited allowed values, each normalised like a single value
    (numbers to base unit, text cleaned). Members that fail to parse make the whole thing unparsed."""
    members: list[str] = []
    uom = ""
    for tok in str(raw or "").split("|"):
        if not tok.strip():
            continue
        n = normalize_value(tok, attr)
        if n.kind == "number":
            members.append(bare_number(n.value_num))
            uom = uom or n.uom
        elif n.kind == "text":
            members.append(n.value_text)
        else:
            return Norm(value_text=str(raw), kind="unparsed")
    if not members:
        return Norm(kind="empty")
    return Norm(value_text="|".join(dict.fromkeys(members)), uom=uom, kind="set")


def normalize_boolean(raw: str) -> Norm:
    """`boolean` requirement: value is exactly 'true' or 'false' in the contract."""
    s = str(raw or "").strip().lower()
    if not s:
        return Norm(kind="empty")
    if s in _TRUE:
        return Norm(value_text="true", kind="boolean")
    if s in _FALSE:
        return Norm(value_text="false", kind="boolean")
    return Norm(value_text=str(raw), kind="unparsed")


def normalize_value(raw: str, attr: str | None = None) -> Norm:
    """Parse one attribute value string. `attr` is the canonical attribute name (may be None)."""
    s = str(raw or "").strip()
    if not s or s.lower() in {"-", "n/a", "na", "none", "null", "unknown"}:
        return Norm(kind="empty")

    if attr in TEXT_ATTRS:
        if attr == "package_case":
            txt = normalize_package(s)
        elif attr in ("dielectric", "dielectric_type"):
            txt = normalize_dielectric(s)
        else:
            txt = clean_text(_PAREN.sub("", s))
        return Norm(value_text=txt, kind="text")

    if attr == "shell_size" and s.strip().upper() in SHELL_SIZE_LETTERS:
        # MIL-DTL-38999 shell-size letter designator -> the numeric shell size PUB LOG / MIL-DTL-5015 use
        n = SHELL_SIZE_LETTERS[s.strip().upper()]
        return Norm(value_num=float(n), uom="", kind="number", value_text=s)

    s1 = _QUAL_WORDS.sub(" ", _PAREN.sub("", s))  # drop "(TA)", "(1608 Metric)" style qualifiers
    s1 = re.sub(r"\s*@.*$", "", s1)                # drop test conditions: "30 @ 7.9MHz", "1.1V @ 1A" -> the value
    s1 = re.sub(r"\s+", " ", s1).strip().rstrip(",;")
    s1 = s1.replace("+/-", "±").replace("+/−", "±")

    # Digi-Key writes compound values like "0.125W, 1/8W" — try each comma segment, first parse wins.
    segments = [seg.strip() for seg in s1.split(",") if seg.strip()] if "," in s1 else [s1]
    for seg in segments:
        got = _parse_numeric_segment(seg, attr, s)
        if got is not None:
            return got

    # numeric-looking but not understood
    if attr in NUMERIC_ATTRS or attr is None:
        return Norm(value_text=s, kind="unparsed")
    return Norm(value_text=clean_text(s), kind="text")


def _parse_numeric_segment(s1: str, attr: str | None, original: str) -> Norm | None:
    # fractions first ("1/8 W", "1/4W"): unsigned a/b where the attribute is not a range attribute.
    m = _FRACTION.match(s1)
    if m and attr not in RANGE_ATTRS and not _SIGNED_FRACTION.search(s1):
        den = _to_float(m.group(2))
        if den not in (None, 0.0):
            num = _to_float(m.group(1)) / den  # type: ignore[operator]
            conv = _apply_unit(num, m.group(3), attr)
            if conv:
                return Norm(value_num=conv[0], uom=conv[1], kind="number", value_text=original)

    # ranges: "-55°C ~ 125°C", "-55.0/+125.0 DEG CELSIUS", "-55 to +125 C", "-55 DEG C AND +125 DEG C"
    m = _RANGE.match(s1)
    if m:
        lo, unit1, hi, unit2 = m.groups()
        lo_f, hi_f = _to_float(lo), _to_float(hi)
        unit_tok = (unit2 or unit1 or "").strip()
        if lo_f is not None and hi_f is not None and lo_f <= hi_f and (attr in RANGE_ATTRS or unit_tok or attr is None):
            conv_lo = _apply_unit(lo_f, unit_tok, attr)
            conv_hi = _apply_unit(hi_f, unit_tok, attr)
            if conv_lo and conv_hi:
                return Norm(value_min=conv_lo[0], value_max=conv_hi[0], uom=conv_lo[1], kind="range", value_text=original)
            if attr in RANGE_ATTRS:  # inherently a range; unit unknown but numbers usable
                return Norm(value_min=lo_f, value_max=hi_f, uom=FAMILY_OF_ATTR.get(attr, ""), kind="range",
                            value_text=original)

    # single number with optional ± and unit: "10.0 KILOHMS", "±5%", "0.125 WATTS", "50V", "100ppm/°C"
    m = _NUM_UNIT.match(s1)
    if m:
        _pm, num_s, unit_tok = m.groups()
        num = _to_float(num_s)
        if num is not None:
            if attr == "temperature_coefficient" or (attr or "").endswith("tolerance"):
                num = abs(num)  # "±5%" and "-55 ppm" are magnitudes
            conv = _apply_unit(num, unit_tok, attr)
            if conv:
                return Norm(value_num=conv[0], uom=conv[1], kind="number", value_text=original)
    return None


_BARE_NUMBER = re.compile(r"^\s*[+\-−±]?\s*\d+(?:\.\d+)?(?:\s*[/~-]\s*[+\-−]?\d+(?:\.\d+)?)?\s*$")


def value_with_uom(value: str, uom: str) -> str:
    """Sources that keep the unit in a separate column ("10000", uom "pF") must be read as "10000 pF", or the
    number lands in the base unit a million-fold wrong. Only applied when the value itself carries no unit and the
    uom is one the unit table knows; 'text', 'count', 'cycles' etc. leave the value alone."""
    v, u = str(value or "").strip(), str(uom or "").strip()
    if not u or not _BARE_NUMBER.match(v) or _unit_lookup(u) is None:
        return v
    return f"{v} {u}"


def infer_operator(attr: str, given: str, norm: Norm) -> str:
    """Always returns a member of OPERATORS. A given operator (any accepted spelling) wins; a parsed range
    is `range`; otherwise the attribute's rating-direction default; otherwise `eq`."""
    canon = canonical_operator(given)
    if canon:
        # a range value with a non-range operator makes no sense; the value shape wins
        return "range" if (norm.kind == "range" and canon in ("eq", "gte", "lte")) else canon
    if norm.kind == "range":
        return "range"
    return DEFAULT_OPERATORS.get(attr, "eq")


def split_range_attr(attr: str, norm: Norm) -> list[tuple[str, Norm, str]]:
    """operating_temp range -> [(operating_temp_min, Norm, 'lte'), (operating_temp_max, Norm, 'gte')]."""
    if attr in RANGE_ATTRS and norm.kind == "range":
        return [
            (f"{attr}_min", Norm(value_num=norm.value_min, uom=norm.uom, kind="number", value_text=norm.value_text), "lte"),
            (f"{attr}_max", Norm(value_num=norm.value_max, uom=norm.uom, kind="number", value_text=norm.value_text), "gte"),
        ]
    if attr in RANGE_ATTRS and norm.kind == "number":
        # a lone temperature is almost always the max rating
        return [(f"{attr}_max", norm, "gte")]
    return [(attr, norm, "")]


def canonical_attr(aliases: Aliases, source: str, name: str) -> tuple[str, bool]:
    """Alias lookup that leaves already-canonical names alone."""
    canon_set = set(NUMERIC_ATTRS) | TEXT_ATTRS
    if norm_key(name).replace(" ", "_") in canon_set:
        return norm_key(name).replace(" ", "_"), True
    return aliases.canonical(source, name)


# Distributors say "Tolerance" for resistors, capacitors and inductors alike; requirement profiles name the
# quantity it belongs to. Resolve by which primary quantity the same part/profile carries.
TOLERANCE_BY_PRIMARY = {"resistance": "resistance_tolerance", "capacitance": "capacitance_tolerance",
                        "inductance": "inductance_tolerance"}


def contextual_attr(attr: str, present: set[str], value: str = "") -> str:
    """'tolerance' -> 'resistance_tolerance' when the same part has a resistance, etc.; otherwise unchanged.
    Digi-Key files a ceramic capacitor's dielectric class (X7R, C0G/NP0) under "Temperature Coefficient" —
    the value, not the name, says which attribute it is."""
    if attr == "tolerance":
        for primary, specific in TOLERANCE_BY_PRIMARY.items():
            if primary in present:
                return specific
    if attr == "temperature_coefficient" and value and _DIELECTRIC_CODE.search(value):
        return "dielectric_type"
    return attr


# EIA class-I/II ceramic dielectric codes; NP0 is the industry name for EIA C0G and is folded onto it
_DIELECTRIC_CODE = re.compile(r"\b(C0G|COG|NP0|NPO|X5R|X6S|X7R|X7S|X7T|X8R|X8L|Y5V|Z5U|U2J|C0H)\b", re.I)
DIELECTRIC_SYNONYMS = {"COG": "C0G", "NP0": "C0G", "NPO": "C0G"}
_DIELECTRIC_NOISE = {"CERAMIC", "DIELECTRIC", "TYPE", "CLASS"}


def normalize_dielectric(s: str) -> str:
    """'C0G, NP0' -> 'C0G'; 'CERAMIC COG' -> 'C0G'; 'Metallized Paper' -> 'METALLIZEDPAPER' (unchanged path)."""
    tokens = [t for t in re.split(r"[\s,/;()]+", s.upper()) if t and t not in _DIELECTRIC_NOISE]
    tokens = sorted({DIELECTRIC_SYNONYMS.get(t, t) for t in tokens})
    return "".join(tokens) if tokens else clean_text(s)


# Closed enum (SCHEMA §8): active | nrnd | obsolete | unknown. End-of-life / last-time-buy map to obsolete:
# for a substitution decision a part you can buy once more is not a part you can design in.
LIFECYCLES = {"active", "nrnd", "obsolete", "unknown"}
LIFECYCLE_MAP = {
    "active": "active", "production": "active", "new": "active", "in production": "active", "new product": "active",
    "nrnd": "nrnd", "not recommended for new designs": "nrnd", "not for new designs": "nrnd",
    "last time buy": "obsolete", "ltb": "obsolete", "end of life": "obsolete", "eol": "obsolete",
    "discontinued": "obsolete", "obsolete": "obsolete", "unknown": "unknown", "": "unknown",
}


def normalize_lifecycle(raw: str) -> str:
    k = norm_key(raw)
    if k in LIFECYCLE_MAP:
        return LIFECYCLE_MAP[k]
    for key, val in LIFECYCLE_MAP.items():
        if key and key in k:
            return val
    return "unknown"


def _num_or_blank(s: str) -> str:
    v = _to_float(str(s or "").replace(",", "").replace("$", "").strip())
    return "" if v is None else repr(v)


# ------------------------------------------------------------------ frame-level
PROFILE_OUT = ["requirement_id", "nsn", "requirement_name", "operator", "value", "uom", "requirement_class", "source",
               "source_spec_id", "conflicting_value", "conflicting_source", "value_num", "value_min", "value_max",
               "value_text", "parse_status", "raw_requirement_name"]
CAND_OUT = ["nsn", "candidate_mpn", "manufacturer", "digikey_pn", "attribute_name", "raw_name", "raw_value",
            "value_num", "value_min", "value_max", "uom", "value_text", "parse_status", "source"]
CANDIDATE_SOURCES = ("digikey", "external")   # `source` on candidates_normalized: where the candidate came from
CP_OUT = ["mpn", "manufacturer", "lifecycle_status", "median_price", "stock_qty", "lead_time_days", "datasheet_url",
          "distributor_count", "lifecycle_raw", "price_basis_qty"]


def normalize_profiles(df: pd.DataFrame, aliases: Aliases, dropped: DroppedRows) -> pd.DataFrame:
    rows: list[dict] = []
    inverted: list[str] = []
    for _, r in df.iterrows():
        source = (r.get("source") or "publog_characteristics").strip().lower()
        # spec_extraction and manual_override rows use spec-style attribute names; PUB LOG rows use MRC statements
        alias_src = "spec" if (source.startswith("spec") or source == "manual_override") else "publog"
        attr, _ = canonical_attr(aliases, alias_src, r["requirement_name"])
        given_op = r.get("operator", "") or ""
        if given_op and canonical_operator(given_op) is None:
            dropped.drop(f"operator {given_op!r} is not in the closed enum {sorted(OPERATORS)}", pd.DataFrame([r]), "profiles")
            continue
        canon_op = canonical_operator(given_op)
        if canon_op == "in_set":
            norm = normalize_set(r["value"], attr)
        elif canon_op == "boolean":
            norm = normalize_boolean(r["value"])
        else:
            norm = normalize_value(value_with_uom(r["value"], r.get("uom", "")), attr)
        if norm.kind == "empty":
            dropped.drop("requirement with empty value", pd.DataFrame([r]), "profiles")
            continue
        for a, n, forced_op in split_range_attr(attr, norm):
            op = forced_op or infer_operator(a, given_op, n)
            expected = DEFAULT_OPERATORS.get(a)
            if canon_op and expected in ("gte", "lte") and op in ("gte", "lte", "eq") and op != expected:
                # the source's operator contradicts the attribute's physical direction (a tolerance is a ceiling,
                # a rating is a floor). It is kept — the source is authoritative — but flagged, because scored
                # this way a *better* part fails. Fix at the source via config/spec_requirements_overrides.csv.
                inverted.append(f"{r['nsn']} {a} {op} {r['value']} ({r.get('source', '')})")
            rows.append({
                "requirement_id": f"{r['nsn']}__{a}",   # recomputed after any range split; matches S5's rule
                "nsn": r["nsn"], "requirement_name": a, "operator": op, "value": r["value"],
                "uom": n.uom or r.get("uom", ""), "requirement_class": r.get("requirement_class", "") or "",
                "source": r.get("source", "") or "", "source_spec_id": r.get("source_spec_id", "") or "",
                "conflicting_value": r.get("conflicting_value", "") or "",
                "conflicting_source": r.get("conflicting_source", "") or "",
                **n.as_row(), "parse_status": n.kind, "raw_requirement_name": r["requirement_name"],
            })
    if inverted:
        dropped.logger.warning("%d requirement operator(s) contradict the attribute's rating direction and will score "
                               "better parts as failing — correct them in config/spec_requirements_overrides.csv: %s",
                               len(inverted), "; ".join(inverted[:12]) + (" …" if len(inverted) > 12 else ""))
    out = pd.DataFrame(rows, columns=PROFILE_OUT + ["kind"]).drop(columns=["kind"], errors="ignore")
    return out


def normalize_candidates(df: pd.DataFrame, aliases: Aliases, dropped: DroppedRows) -> pd.DataFrame:
    rows: list[dict] = []
    for _, r in df.iterrows():
        try:
            attrs = json.loads(r["raw_attributes_json"] or "{}")
        except json.JSONDecodeError:
            dropped.drop("candidate raw_attributes_json is not valid JSON", pd.DataFrame([r]), "candidates")
            continue
        if not isinstance(attrs, dict) or not attrs:
            dropped.drop("candidate has no attributes", pd.DataFrame([r]), "candidates")
            continue
        canon = {raw_name: canonical_attr(aliases, "digikey", raw_name)[0]
                 for raw_name in attrs if not str(raw_name).startswith("_")}  # "_status" etc. are metadata
        present = set(canon.values())
        for raw_name, raw_value in attrs.items():
            if raw_name not in canon:
                continue
            attr = contextual_attr(canon[raw_name], present, str(raw_value))
            norm = normalize_value(str(raw_value), attr)
            for a, n, _op in split_range_attr(attr, norm):
                rows.append({
                    "nsn": r["nsn"], "candidate_mpn": r["candidate_mpn"], "manufacturer": r.get("manufacturer", ""),
                    "digikey_pn": r.get("digikey_pn", ""), "attribute_name": a, "raw_name": raw_name,
                    "raw_value": str(raw_value), **n.as_row(), "parse_status": n.kind, "source": "digikey",
                })
    out = pd.DataFrame(rows, columns=CAND_OUT + ["kind"]).drop(columns=["kind"], errors="ignore")
    return out


def normalize_external_candidates(df: pd.DataFrame, aliases: Aliases, dropped: DroppedRows) -> pd.DataFrame:
    """data/candidates_external.csv — hand-collected candidates in LONG format, one attribute per row:
    nsn, candidate_mpn, manufacturer, attribute_name, attribute_value, uom. Attribute names use the spec
    vocabulary (canonical names), the unit sits in its own column, there is no Digi-Key part number.
    Same output shape as normalize_candidates so S9 cannot tell them apart except by `source`."""
    rows: list[dict] = []
    need = {"nsn", "candidate_mpn", "attribute_name", "attribute_value"}
    missing = need - set(df.columns)
    if missing:
        dropped.drop(f"candidates_external.csv lacks column(s) {sorted(missing)} — file ignored", df, "external")
        return pd.DataFrame(columns=CAND_OUT)
    dup = df.duplicated(["nsn", "candidate_mpn", "attribute_name"], keep="first")
    if dup.any():
        dropped.drop("duplicate (nsn, candidate_mpn, attribute_name) in candidates_external.csv (kept first)",
                     df[dup], "external")
        df = df[~dup]
    for _, r in df.iterrows():
        nsn, mpn = str(r["nsn"]).strip(), str(r["candidate_mpn"]).strip()
        if not nsn or not mpn:
            dropped.drop("external candidate row without nsn / candidate_mpn", pd.DataFrame([r]), "external")
            continue
        attr, _ = canonical_attr(aliases, "spec", r["attribute_name"])
        raw_value, uom = str(r.get("attribute_value", "") or ""), str(r.get("uom", "") or "")
        norm = normalize_value(value_with_uom(raw_value, uom), attr)
        if norm.kind == "empty":
            dropped.drop("external candidate attribute with empty value", pd.DataFrame([r]), "external")
            continue
        for a, n, _op in split_range_attr(attr, norm):
            rows.append({
                "nsn": nsn, "candidate_mpn": mpn, "manufacturer": str(r.get("manufacturer", "") or ""),
                "digikey_pn": "", "attribute_name": a, "raw_name": str(r["attribute_name"]),
                "raw_value": f"{raw_value} {uom}".strip(), **n.as_row(), "parse_status": n.kind, "source": "external",
            })
    return pd.DataFrame(rows, columns=CAND_OUT + ["kind"]).drop(columns=["kind"], errors="ignore")


def gather_normalized_candidates(aliases: Aliases, dropped: DroppedRows,
                                 digikey: pd.DataFrame | None = None,
                                 external: pd.DataFrame | None = None) -> pd.DataFrame:
    """Normalise Digi-Key (wide JSON) and external (long) candidates into one long frame.
    `source` is digikey | external so S9 can split them. Same MPN on two NSNs is two candidates."""
    parts: list[pd.DataFrame] = []
    if digikey is not None and len(digikey):
        parts.append(normalize_candidates(digikey, aliases, dropped))
    if external is not None and len(external):
        parts.append(normalize_external_candidates(external, aliases, dropped))
    if not parts:
        return pd.DataFrame(columns=CAND_OUT)
    return pd.concat(parts, ignore_index=True)


def normalize_commercial_parts(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("median_price", "stock_qty", "lead_time_days", "distributor_count", "price_basis_qty"):
        if col in out.columns:
            out[col] = out[col].map(_num_or_blank)
    if "price_basis_qty" not in out.columns:
        out["price_basis_qty"] = ""
    # lifecycle_status is the closed enum in the contract; the distributor's wording is kept in lifecycle_raw.
    # Idempotent: S7 already writes the enum (raw in lifecycle_raw), so a second pass changes nothing.
    status = out.get("lifecycle_status", pd.Series([""] * len(out), index=out.index)).fillna("")
    raw = out["lifecycle_raw"] if "lifecycle_raw" in out.columns else pd.Series([""] * len(out), index=out.index)
    out["lifecycle_raw"] = [r if r else ("" if s in LIFECYCLES else s) for s, r in zip(status, raw.fillna(""))]
    out["lifecycle_status"] = status.map(normalize_lifecycle)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    aliases = Aliases()
    did_anything = False

    prof = read_data_csv("requirement_profiles.csv", required=False, logger=logger)
    if prof is not None:
        out = normalize_profiles(prof, aliases, dropped)
        unparsed = out[out["parse_status"] == "unparsed"]
        if len(unparsed):
            logger.warning("%d requirement value(s) could not be parsed (will score as unknown); examples: %s",
                           len(unparsed), "; ".join(unparsed["value"].head(5)))
        write_data_csv(out, "requirement_profiles_normalized.csv", PROFILE_OUT, logger)
        did_anything = True

    cands = read_data_csv("candidates.csv", required=False, logger=logger)
    ext = read_data_csv("candidates_external.csv", required=False, logger=logger)
    if cands is not None or ext is not None:
        out = gather_normalized_candidates(aliases, dropped, digikey=cands, external=ext)
        unparsed = out[out["parse_status"] == "unparsed"] if len(out) else out
        if len(unparsed):
            logger.warning("%d candidate attribute value(s) could not be parsed; examples: %s", len(unparsed),
                           "; ".join((unparsed["raw_name"] + "=" + unparsed["raw_value"]).head(5)))
        by_src = out["source"].value_counts().to_dict() if len(out) else {}
        logger.info("candidate sources: %s (%d attribute rows, %d (nsn, mpn) pairs)",
                    by_src, len(out), out.groupby(["nsn", "candidate_mpn"]).ngroups if len(out) else 0)
        write_data_csv(out, "candidates_normalized.csv", CAND_OUT, logger)
        did_anything = True

    cp = read_data_csv("commercial_parts.csv", required=False, logger=logger)
    if cp is not None:
        write_data_csv(normalize_commercial_parts(cp), "commercial_parts_normalized.csv", CP_OUT, logger)
        did_anything = True

    aliases.report(logger)
    dropped.finish()
    if not did_anything:
        logger.error("nothing to normalise: none of requirement_profiles.csv / candidates.csv / "
                     "candidates_external.csv / commercial_parts.csv exist")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
