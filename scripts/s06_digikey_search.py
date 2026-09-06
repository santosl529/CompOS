#!/usr/bin/env python
"""
S6 — digikey_search.py

Candidate generation. For each federal item, translate its requirement profile into a
Digi-Key Product Information API v4 parametric search using config/fsc_category_map.csv,
apply tolerance bands, retrieve candidate MPNs, rank by attribute closeness, cap at 25.

Digi-Key's parametric filters must reference internal ValueIds taken from a previous
response, and (verified live 2026-09-05) they are only returned for LEAF categories, so the
search is two-step per leaf category listed for the FSC:
  1. search inside the leaf (value keywords such as "150 Ohms", else empty = whole leaf)
     -> read FilterOptions.ParametricFilters
  2. pick the ValueIds whose text satisfies each driving attribute's tolerance rule and search
     again with ParameterFilterRequest. Step-2 products are the candidates. Step-1 products are
     used only when no filter could be built or Digi-Key rejected it (400), and the row's
     search_mode says so — an unfiltered "first 50 of 2.4M" is not a candidate list.
An FSC may list several leaves ('|'-separated in fsc_category_map.csv); the leaf whose name
shares a token with the item name is used ("...Backshell" -> Backshells and Cable Clamps),
otherwise all listed leaves are searched and pooled. Results are ranked by closeness, capped.

Inputs:  data/federal_items.csv (S1)  [or --nsn/--keywords/--category for the hardcoded thin slice]
         data/requirement_profiles_normalized.csv (S8) or requirement_profiles.csv (S5, normalised on the fly)
         config/fsc_category_map.csv
Output:  data/candidates.csv  nsn, candidate_mpn, manufacturer, digikey_pn, raw_attributes_json
         (+ closeness_score, search_mode extras)
         data/part_images.csv  mpn, image_url  (Digi-Key PhotoUrl; one row per MPN that has a photo)
         --federal-images also writes data/federal_item_images.csv (nsn, image_url);
         federal_items.csv is left unchanged (SCHEMA has no image_url)
Cache:   cache/digikey/<request-hash>.json — every response, checked before every request.
Env:     DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET, DIGIKEY_BASE_URL (prod or sandbox)

Utilities:
  --list-categories [--grep TEXT]   dump the category tree to cache/digikey/categories.json and print ids
  --dry-run                         build and print the request bodies without calling the API
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Aliases, CachedSession, DATA_DIR, DroppedRows, env, load_fsc_map, load_target_items, log_rows,
    normalize_nsn, parse_rules, read_data_csv, request_hash, setup_logging, split_list, write_data_csv,
)
from s08_normalize import (  # noqa: E402
    NUMERIC_ATTRS, RANGE_ATTRS, TEXT_ATTRS, Norm, canonical_attr, contextual_attr, normalize_profiles,
    normalize_value, split_range_attr,
)

SCRIPT = "s06_digikey_search"
CAP_PER_ITEM = 25
PAGE_LIMIT = 50
MISSING_ATTR_PENALTY = 1.0
CAND_COLS = ["nsn", "candidate_mpn", "manufacturer", "digikey_pn", "raw_attributes_json"]
IMAGE_COLS = ["mpn", "image_url"]
FI_IMAGE_COLS = ["nsn", "image_url"]


# ------------------------------------------------------------------ API client
class DigiKey:
    def __init__(self, logger, session: CachedSession, dry_run: bool = False):
        self.logger = logger
        self.session = session
        self.dry_run = dry_run
        self.base = (env("DIGIKEY_BASE_URL", "https://api.digikey.com") or "").rstrip("/")
        self.client_id = env("DIGIKEY_CLIENT_ID", required=not dry_run) or ""
        self.client_secret = env("DIGIKEY_CLIENT_SECRET", required=not dry_run) or ""
        self._token: str | None = None
        self._token_expiry = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.session.session.post(
            f"{self.base}/v1/oauth2/token",
            data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Digi-Key OAuth token request failed: HTTP {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 600))
        self.logger.info("Digi-Key token obtained (expires in %ss)", body.get("expires_in"))
        return self._token  # type: ignore[return-value]

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token()}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "X-DIGIKEY-Locale-Site": "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "USD",
            "Content-Type": "application/json",
        }

    def keyword_search(self, body: dict) -> dict | None:
        """Returns the response body, or None on a 400 (bad filter). Cached by request body."""
        url = f"{self.base}/products/v4/search/keyword"
        key = request_hash({"u": url, "j": body})
        cached = self.session.read_cache(key)
        if cached is not None:
            self.session.cache_hits += 1
            return cached["body"]
        if self.dry_run:
            self.logger.info("DRY RUN would POST %s\n%s", url, json.dumps(body, indent=1))
            return None
        try:
            entry = self.session.request("POST", url, key=key, headers=self.headers(), json_body=body)
        except RuntimeError as exc:
            if "HTTP 400" in str(exc):
                self.logger.warning("Digi-Key rejected the request (400): %s", str(exc)[:400])
                return None
            raise
        return entry["body"]

    def categories(self) -> dict:
        cached = self.session.read_cache("categories")
        if cached is not None:  # avoid even the token round-trip when the tree is on disk
            self.session.cache_hits += 1
            return cached["body"]
        if self.dry_run:
            return {}
        url = f"{self.base}/products/v4/search/categories"
        entry = self.session.request("GET", url, key="categories", headers=self.headers())
        return entry["body"]


# ------------------------------------------------------------------ profile -> search
def eng(value: float, unit: str) -> str:
    """10000 ohm -> '10 kOhms'; 1e-7 F -> '0.1 µF'; 0.125 W -> '0.125W'; 125 degC -> '125°C'."""
    if unit == "%":
        return f"{value:g}%"
    if unit == "degC":
        return f"{value:g}°C"
    if unit == "ppm/degC":
        return f"{value:g}ppm/°C"
    sym = {"ohm": "Ohms", "F": "F", "V": "V", "A": "A", "W": "W", "H": "H", "Hz": "Hz"}.get(unit, unit)
    for mult, pfx in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= mult * 0.999:
            return f"{value / mult:g} {pfx}{sym}".replace(" ", "" if not pfx and sym in ("W", "V", "A") else " ")
    return f"{value:g} {sym}"


def profile_targets(prof_rows: list[dict], driving: list[str]) -> dict[str, dict]:
    """Pick, per driving attribute, the requirement row to search/rank against."""
    targets: dict[str, dict] = {}
    for r in prof_rows:
        a = r["requirement_name"]
        if a in driving and a not in targets and r.get("parse_status") in ("number", "text", "range"):
            targets[a] = r
    return targets


# Primary quantities Digi-Key's keyword index handles well ("150 Ohms", "0.1 µF"). Temperatures,
# tolerances and counts are left to the parametric step — as keywords they only shrink the hit set.
KEYWORD_VALUE_ATTRS = {"resistance", "capacitance", "inductance", "voltage_rating", "coil_voltage",
                       "current_rating", "power_rating"}


def build_keywords(item_name: str, targets: dict[str, dict], driving: list[str]) -> str:
    """Value keywords only. The PUB LOG item name ("ELECTRICAL PLUG CONNECTOR BODY") is deliberately NOT
    used: verified live, it matches nothing inside the right leaf category while an empty keyword returns the
    whole leaf with its parametric filters. The category does the noun's job."""
    words: list[str] = []
    for a in driving[:2]:
        t = targets.get(a)
        if (a in KEYWORD_VALUE_ATTRS and t and t.get("parse_status") == "number"
                and t.get("value_num") not in ("", None) and t.get("uom")):
            words.append(eng(float(t["value_num"]), t.get("uom", "")))
    return " ".join(words)[:250]


def value_satisfies(attr: str, rule: tuple[str, str], target: dict, cand_norm: Norm) -> bool:
    kind, arg = rule
    if cand_norm.kind in ("unparsed", "empty"):
        return False
    t = float(target["value_num"]) if target.get("value_num") not in ("", None) else None
    if kind == "exact" or attr in TEXT_ATTRS:
        if t is not None and cand_norm.value_num is not None:
            return abs(cand_norm.value_num - t) <= 1e-9 * max(1.0, abs(t))   # "5VDC" == 5 V, "37" == 37
        return bool(target.get("value_text")) and cand_norm.value_text == target.get("value_text")
    if t is None:
        return False
    c = cand_norm.value_num
    if c is None and cand_norm.kind == "range":
        c = cand_norm.value_max if kind == "gte" else cand_norm.value_min if kind == "lte" else None
    if c is None:
        return False
    if kind == "pct":
        pct = float(arg) if arg else 5.0
        return t != 0 and abs(c - t) / abs(t) * 100.0 <= pct + 1e-9
    if kind == "gte":
        return c >= t
    if kind == "lte":
        return c <= t
    return False


def target_attrs_for(attr: str, targets: dict[str, dict]) -> list[str]:
    """Which profile attribute(s) a Digi-Key parameter can be compared to.
    'operating_temp' (a range) maps onto the min/max split; 'tolerance' onto whichever *_tolerance the
    profile carries (a resistor profile has resistance_tolerance, never capacitance_tolerance)."""
    if attr in RANGE_ATTRS:
        return [a for a in (f"{attr}_min", f"{attr}_max") if a in targets]
    if attr == "tolerance":
        # a profile belongs to one item, so at most one *_tolerance requirement exists; use it even when the
        # primary quantity itself is missing from the profile (a resistor profile with tolerance but no resistance)
        specific = [a for a in targets if a.endswith("_tolerance") or a == "tolerance"]
        return specific[:1]
    return [attr] if attr in targets else []


def choose_filter_values(filter_options: dict, aliases: Aliases, targets: dict[str, dict],
                         rules: dict[str, tuple[str, str]], logger) -> list[dict]:
    """From FilterOptions.ParametricFilters pick ValueIds satisfying each driving attribute's rule.

    v4 field names are FilterValues[].ValueId / ValueName (the pre-release docs said Values[].ValueText;
    both are accepted so a cached older response still parses)."""
    chosen: list[dict] = []
    for pf in (filter_options or {}).get("ParametricFilters", []) or []:
        pname = pf.get("ParameterName", "")
        attr, _ = canonical_attr(aliases, "digikey", pname)
        hit_attrs = [a for a in target_attrs_for(attr, targets) if a in rules]
        if not hit_attrs:
            continue
        values = pf.get("FilterValues") or pf.get("Values") or []
        ids: list[str] = []
        for v in values:
            text = v.get("ValueName") if v.get("ValueName") is not None else v.get("ValueText", "")
            norm = normalize_value(str(text or ""), attr)
            parts = split_range_attr(attr, norm) if attr in RANGE_ATTRS else [(hit_attrs[0], norm, "")]
            # a range filter value must satisfy every targeted side (min AND max); a scalar just its attribute
            checks = [value_satisfies(a, rules[a], targets[a], n) for a, n, _ in parts if a in hit_attrs]
            if checks and all(checks) and v.get("ValueId") is not None:
                ids.append(str(v["ValueId"]))
        if ids:
            chosen.append({"ParameterId": int(pf["ParameterId"]), "FilterValues": [{"Id": i} for i in ids[:100]]})
            logger.info("  filter %-28s -> %d of %d values satisfy %s", pname, len(ids), len(values),
                        {a: rules[a] for a in hit_attrs})
        else:
            logger.info("  filter %-28s -> none of %d values satisfy %s (not applied)", pname, len(values),
                        {a: rules[a] for a in hit_attrs})
    return chosen


def parse_product(p: dict) -> tuple[str, str, str, dict]:
    mpn = (p.get("ManufacturerProductNumber") or "").strip()
    mfr = ((p.get("Manufacturer") or {}).get("Name") or "").strip()
    variations = p.get("ProductVariations") or []
    dkpn = (variations[0].get("DigiKeyProductNumber") if variations else p.get("DigiKeyProductNumber")) or ""
    attrs: dict[str, str] = {}
    for prm in p.get("Parameters") or []:
        name, val = prm.get("ParameterText"), prm.get("ValueText")
        if name and val is not None:
            attrs[str(name)] = str(val)
    attrs["_status"] = ((p.get("ProductStatus") or {}).get("Status") or (p.get("ProductStatus") or {}).get("Text") or "")
    attrs["_datasheet"] = p.get("DatasheetUrl") or ""
    attrs["_photo"] = (p.get("PhotoUrl") or "").strip()
    attrs["_description"] = ((p.get("Description") or {}).get("ProductDescription") or "")
    attrs["_unit_price"] = p.get("UnitPrice")
    attrs["_qty_available"] = p.get("QuantityAvailable")
    cat = p.get("Category") or {}
    while cat.get("ChildCategories"):  # v4 nests the leaf under the top-level category
        cat = cat["ChildCategories"][0]
    attrs["_category"] = cat.get("Name") or ""
    attrs["_product_url"] = p.get("ProductUrl") or ""
    return mpn, mfr, dkpn, attrs


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def first_product_photo(products: list | None, query: str = "") -> tuple[str, str]:
    """First (PhotoUrl, MPN) from a Digi-Key Products list. Empty strings if none.

    When `query` is at least 6 alphanumerics (an NSN or MCRL PN), the product's
    MPN or description must contain it — a loose keyword hit is not the baseline part.
    """
    q = _alnum(query)
    for p in products or []:
        url = (p.get("PhotoUrl") or "").strip()
        mpn = (p.get("ManufacturerProductNumber") or "").strip()
        if not url:
            continue
        if len(q) >= 4:
            desc = ((p.get("Description") or {}).get("ProductDescription") or "")
            blob = _alnum(mpn + " " + desc)
            if q not in blob:
                continue
        return url, mpn
    return "", ""


def candidate_photo_by_nsn(candidates: pd.DataFrame | None, images: pd.DataFrame | None) -> dict[str, tuple[str, str]]:
    """nsn -> (image_url, source_mpn) from existing candidate photos. First hit per NSN wins."""
    if candidates is None or images is None or len(candidates) == 0 or len(images) == 0:
        return {}
    url_of = dict(zip(images["mpn"], images["image_url"]))
    out: dict[str, tuple[str, str]] = {}
    for _, r in candidates.iterrows():
        nsn, mpn = r.get("nsn", ""), r.get("candidate_mpn", "")
        if nsn in out or not mpn:
            continue
        url = url_of.get(mpn, "")
        if url:
            out[nsn] = (url, mpn)
    return out


def part_images(rows: list[dict]) -> pd.DataFrame:
    """mpn -> Digi-Key PhotoUrl. One row per MPN; first non-empty URL wins. MPNs with no photo are omitted."""
    seen: dict[str, str] = {}
    for r in rows:
        mpn = str(r.get("candidate_mpn") or "").strip()
        if not mpn or mpn in seen:
            continue
        raw = r.get("raw_attributes_json") or "{}"
        try:
            attrs = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            continue
        url = str((attrs or {}).get("_photo") or "").strip()
        if url:
            seen[mpn] = url
    return pd.DataFrame([{"mpn": m, "image_url": u} for m, u in seen.items()], columns=IMAGE_COLS)


def closeness(attrs: dict, aliases: Aliases, targets: dict[str, dict], rules: dict[str, tuple[str, str]]) -> float:
    """Lower is closer. Sum over driving attributes of a normalised distance; missing attribute = 1.0."""
    parsed: dict[str, Norm] = {}
    canon = {k: canonical_attr(aliases, "digikey", k)[0] for k in attrs if not k.startswith("_")}
    present = set(canon.values())
    for raw_name, raw_val in attrs.items():
        if raw_name not in canon:
            continue
        attr = contextual_attr(canon[raw_name], present)
        norm = normalize_value(str(raw_val), attr)
        for a, n, _ in split_range_attr(attr, norm):
            parsed.setdefault(a, n)
    score = 0.0
    for a, t in targets.items():
        n = parsed.get(a)
        if n is None or n.kind in ("unparsed", "empty"):
            score += MISSING_ATTR_PENALTY
            continue
        kind, arg = rules.get(a, ("pct", "5"))
        if kind == "exact" or a in TEXT_ATTRS:
            score += 0.0 if n.value_text == t.get("value_text") else 1.0
            continue
        tv = float(t["value_num"]) if t.get("value_num") not in ("", None) else None
        cv = n.value_num if n.value_num is not None else (n.value_max if kind == "gte" else n.value_min)
        if tv is None or cv is None:
            score += MISSING_ATTR_PENALTY
            continue
        denom = abs(tv) or 1.0
        if kind == "gte":
            score += 0.0 if cv >= tv else min(1.0, (tv - cv) / denom)
        elif kind == "lte":
            score += 0.0 if cv <= tv else min(1.0, (cv - tv) / denom)
        else:
            score += min(1.0, abs(cv - tv) / denom)
    return round(score, 4)


# ------------------------------------------------------------------ per-item search
def _collect(products: dict[str, dict], response: dict | None) -> int:
    n = 0
    for p in (response or {}).get("Products", []) or []:
        mpn, mfr, dkpn, attrs = parse_product(p)
        if mpn and mpn not in products:
            products[mpn] = {"mpn": mpn, "mfr": mfr, "dkpn": dkpn, "attrs": attrs}
            n += 1
    return n


def _tokens(s: str) -> set[str]:
    toks = [t.rstrip("s") for t in re.findall(r"[a-z]+", (s or "").lower()) if len(t) > 2]
    # joined bigrams let "heat sink" meet "Heatsinks" and "back shell" meet "Backshells"
    return set(toks) | {a + b for a, b in zip(toks, toks[1:])}


_STOP = {"electrical", "electronic", "electric", "connector", "connectors", "fixed", "device", "and", "the", "for"}


def pick_categories(item_name: str, category_ids: list[str], names: dict[str, str]) -> list[str]:
    """Among the FSC's leaf categories choose the one(s) whose name shares a meaningful token with the item
    name ("Electrical Connector Backshell" -> 'Backshells and Cable Clamps'). No overlap -> search them all."""
    if len(category_ids) <= 1:
        return category_ids
    item_toks = _tokens(item_name) - _STOP
    scored = []
    for cid in category_ids:
        overlap = item_toks & (_tokens(names.get(cid, "")) - _STOP)
        scored.append((len(overlap), cid))
    best = max(s for s, _ in scored)
    if best == 0:
        return category_ids
    return [cid for s, cid in scored if s == best]


def search_item(dk: DigiKey, aliases: Aliases, logger, nsn: str, item_name: str, category_ids: list[str],
                driving: list[str], rules: dict[str, tuple[str, str]], prof_rows: list[dict],
                keywords_override: str | None = None, category_names: dict[str, str] | None = None
                ) -> tuple[list[dict], str]:
    """Two-step search per leaf category, results pooled.

    Step 1 (keyword or empty search inside the leaf) exists mainly to obtain FilterOptions — Digi-Key's
    parametric filters only come back for leaf categories, and an empty keyword returns the whole leaf.
    Step 2 applies the ValueIds that satisfy the tolerance rules. When step 2 returns products they are
    the candidates; step-1 products are used only when there was nothing to filter on (or the filter was
    rejected), because an unfiltered "first 50 of 2.4M connectors" is not a candidate list."""
    targets = profile_targets(prof_rows, driving)
    keywords = keywords_override if keywords_override is not None else build_keywords(item_name, targets, driving)
    if not category_ids and not keywords:
        # nothing scopes the search — the item noun is the only handle we have
        keywords = (item_name or "").split(",")[0].strip().lower()
    names = category_names or {}
    chosen_cats = pick_categories(item_name, category_ids, names) if category_ids else [""]
    logger.info("[%s] keywords=%r categories=%s driving=%s targets=%s", nsn, keywords,
                [f"{c}:{names.get(c, '?')}" for c in chosen_cats] if category_ids else ["<none>"], driving,
                {a: t.get("value") for a, t in targets.items()})

    products: dict[str, dict] = {}   # parametric (filtered) results
    filler: dict[str, dict] = {}     # unfiltered step-1 results, used only if nothing parametric came back
    modes: list[str] = []
    # name-picked leaves first; if they yield nothing parametric, the FSC's remaining leaves get a turn
    remaining = [c for c in category_ids if c not in chosen_cats]
    queue = list(chosen_cats)
    while True:
        if not queue:
            # only when a real parametric search in the picked leaf came back empty; a leaf with no usable
            # filters (backshells have no temperature/positions parameters) keeps its own unfiltered rows
            # rather than borrowing connector assemblies from a sibling leaf
            if products or not remaining or "parametric_empty" not in modes:
                break
            logger.info("[%s] name-picked leaves returned nothing parametric — trying %s", nsn,
                        [f"{c}:{names.get(c, '?')}" for c in remaining])
            queue, remaining = remaining, []
        category_id = queue.pop(0)
        base_body = {
            "Keywords": keywords, "Limit": PAGE_LIMIT, "Offset": 0,
            "FilterOptionsRequest": {"CategoryFilter": [{"Id": str(category_id)}]} if category_id else {},
        }
        r1 = dk.keyword_search(base_body)
        if r1 is not None and not (r1.get("Products") or []) and keywords and category_id:
            # PUB LOG nouns rarely match Digi-Key's index; an empty keyword inside the leaf always does
            logger.info("[%s] cat %s: keywords matched nothing — retrying with empty keywords", nsn, category_id)
            base_body["Keywords"] = ""
            r1 = dk.keyword_search(base_body)
        if r1 is None:
            modes.append("no_response")
            continue
        step1: dict[str, dict] = {}
        _collect(step1, r1)
        logger.info("[%s] cat %s step 1: %d products (ProductsCount=%s)", nsn, category_id or "-", len(step1),
                    r1.get("ProductsCount"))

        # Parametric filters are category-scoped; without a category there is nothing to filter within.
        filters = (choose_filter_values(r1.get("FilterOptions") or {}, aliases, targets, rules, logger)
                   if targets and category_id else [])
        if not category_id:
            modes.append("keyword_only_no_category" if targets else "keyword_only")
            filler.update(step1)
            continue
        if not filters:
            modes.append("no_filter_values_matched" if targets else "keyword_only")
            filler.update(step1)
            continue
        body2 = dict(base_body)
        body2["FilterOptionsRequest"] = {
            **base_body["FilterOptionsRequest"],
            "ParameterFilterRequest": {"CategoryFilter": {"Id": str(category_id)}, "ParameterFilters": filters},
        }
        r2 = dk.keyword_search(body2)
        if r2 is None:
            modes.append("parametric_rejected_fallback")
            logger.warning("[%s] cat %s: parametric filter rejected — using step-1 results", nsn, category_id)
            filler.update(step1)
            continue
        step2: dict[str, dict] = {}
        _collect(step2, r2)
        logger.info("[%s] cat %s step 2 (parametric): %d products (ProductsCount=%s) filters=%s", nsn, category_id,
                    len(step2), r2.get("ProductsCount"), [f["ParameterId"] for f in filters])
        if (r2.get("ProductsCount") or 0) >= 100_000:
            logger.warning("[%s] cat %s: %s products still match after filtering — the filters are too loose "
                           "(driving attributes without a Digi-Key alias?); top-50 is not a candidate set",
                           nsn, category_id, r2.get("ProductsCount"))
        if step2:
            # a six-figure hit set after filtering means the driving attributes did not constrain the
            # search (missing requirement or alias); the rows are kept but labelled so S9/UI can see it
            modes.append("parametric_loose" if (r2.get("ProductsCount") or 0) >= 100_000 else "parametric")
            for mpn, p in step2.items():
                products.setdefault(mpn, p)
        else:
            modes.append("parametric_empty")
    if products:
        # at least one leaf produced a parametric result set: that IS the candidate list. Unfiltered
        # step-1 rows from other leaves would only add noise that then gets scored convincingly.
        modes = [m for m in modes if m in ("parametric", "parametric_loose", "parametric_empty", "no_response")] or modes
        if filler:
            logger.info("[%s] discarding %d unfiltered step-1 product(s) — parametric results exist", nsn, len(filler))
    else:
        products = filler
    mode = "+".join(sorted(set(modes))) if modes else "no_response"

    rows = []
    for p in products.values():
        rows.append({
            "nsn": nsn, "candidate_mpn": p["mpn"], "manufacturer": p["mfr"], "digikey_pn": p["dkpn"],
            "raw_attributes_json": json.dumps(p["attrs"], ensure_ascii=False),
            "closeness_score": closeness(p["attrs"], aliases, targets, rules) if targets else "",
            "search_mode": mode,
        })
    rows.sort(key=lambda r: (r["closeness_score"] if r["closeness_score"] != "" else 9e9, r["candidate_mpn"]))
    return rows, mode


# ------------------------------------------------------------------ main
def load_profiles(logger, aliases: Aliases, dropped: DroppedRows) -> dict[str, list[dict]]:
    normalized = DATA_DIR / "requirement_profiles_normalized.csv"
    raw = DATA_DIR / "requirement_profiles.csv"
    if normalized.exists() and not (raw.exists() and raw.stat().st_mtime > normalized.stat().st_mtime):
        df = read_data_csv("requirement_profiles_normalized.csv", logger=logger)
    elif raw.exists():
        if normalized.exists():
            logger.warning("data/requirement_profiles_normalized.csv is older than requirement_profiles.csv (stale S8 output) — "
                           "normalising the S5 file on the fly instead")
        else:
            logger.info("normalising data/requirement_profiles.csv on the fly (S8 has not run yet)")
        df = normalize_profiles(read_data_csv("requirement_profiles.csv", logger=logger), aliases, dropped)
    else:
        logger.warning("no requirement profile found — searches will be keyword+category only, unranked")
        return {}
    out: dict[str, list[dict]] = {}
    for _, r in df.iterrows():
        out.setdefault(r["nsn"], []).append(r.to_dict())
    return out


def fetch_federal_item_images(dk: DigiKey, logger, dry_run: bool = False) -> int:
    """One PhotoUrl per federal item. Try Digi-Key keyword search on the NSN, then MCRL
    reference PNs; if neither hits, use an existing candidate photo as a stand-in."""
    items = read_data_csv("federal_items.csv", logger=logger)
    if items is None:
        raise SystemExit("data/federal_items.csv is required for --federal-images")
    mcrl = read_data_csv("mcrl.csv", required=False, logger=logger)
    cands = read_data_csv("candidates.csv", required=False, logger=logger)
    imgs = read_data_csv("part_images.csv", required=False, logger=logger)
    fallback = candidate_photo_by_nsn(cands, imgs)
    pns_by_nsn: dict[str, list[str]] = {}
    if mcrl is not None and len(mcrl) and "reference_part_number" in mcrl.columns:
        for nsn, grp in mcrl.groupby("nsn"):
            pns_by_nsn[str(nsn)] = list(dict.fromkeys(str(x) for x in grp["reference_part_number"] if x))

    rows: list[dict] = []
    for _, it in items.iterrows():
        nsn = str(it["nsn"])
        url, src, src_mpn = "", "", ""
        queries = [nsn, nsn.replace("-", "")]
        queries.extend(pns_by_nsn.get(nsn, []))
        seen: set[str] = set()
        for q in queries:
            q = (q or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            resp = dk.keyword_search({"Keywords": q, "Limit": 5, "Offset": 0})
            if resp is None:
                continue
            url, src_mpn = first_product_photo(resp.get("Products"), query=q)
            if url:
                src = "nsn_search" if q.replace("-", "") == nsn.replace("-", "") else "mcrl_pn"
                break
        if not url and nsn in fallback:
            url, src_mpn = fallback[nsn]
            src = "candidate_standin"
        rows.append({"nsn": nsn, "image_url": url, "source": src, "source_mpn": src_mpn})
        logger.info("[%s] image %s (%s %s)", nsn, "yes" if url else "none", src or "-", src_mpn or "-")

    out = pd.DataFrame(rows, columns=FI_IMAGE_COLS + ["source", "source_mpn"])
    write_data_csv(out, "federal_item_images.csv", FI_IMAGE_COLS, logger)
    n_yes = int((out["image_url"] != "").sum())
    logger.info("federal item images: %d of %d have a PhotoUrl %s",
                n_yes, len(out), out["source"].value_counts().to_dict())
    return 0


def print_categories(tree: dict, grep: str | None) -> None:
    def walk(node: dict, path: list[str]):
        name = node.get("Name", "")
        cid = node.get("CategoryId")
        here = path + [name]
        line = f"{cid:>6}  {' > '.join(here)}  ({node.get('ProductCount', '')})"
        if not grep or grep.lower() in line.lower():
            print(line)
        # v4 returns "Children"; older docs say "ChildCategories" — accept both
        for ch in node.get("Children") or node.get("ChildCategories") or []:
            walk(ch, here)
    for top in tree.get("Categories", []) or []:
        walk(top, [])


def leaf_names(dk: DigiKey, logger) -> dict[str, str]:
    """category id -> name, from the cached tree (fetched once if absent; 1 request). Used to pick leaves by
    item name and to make the log readable. Empty dict in dry-run when no cache exists."""
    try:
        tree = dk.categories()
    except Exception as exc:  # noqa: BLE001 — names are a convenience, never fatal
        logger.warning("could not load the category tree (%s) — leaf selection by item name disabled", exc)
        return {}
    names: dict[str, str] = {}

    def walk(node: dict):
        if node.get("CategoryId") is not None:
            names[str(node["CategoryId"])] = node.get("Name", "")
        for ch in node.get("Children") or node.get("ChildCategories") or []:
            walk(ch)
    for top in (tree or {}).get("Categories", []) or []:
        walk(top)
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nsn", help="only this NSN (13 digits or dashed)")
    ap.add_argument("--keywords", help="override search keywords (thin-slice mode)")
    ap.add_argument("--category", help="override Digi-Key leaf category id(s), '|'-separated (thin-slice mode)")
    ap.add_argument("--list-categories", action="store_true")
    ap.add_argument("--grep", help="filter --list-categories output")
    ap.add_argument("--dry-run", action="store_true", help="print request bodies, do not call the API")
    ap.add_argument("--federal-images", action="store_true",
                    help="look up a PhotoUrl per federal item (NSN, then MCRL PN, then candidate stand-in); "
                         "does not rewrite candidates.csv")
    args = ap.parse_args()

    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    session = CachedSession("digikey", logger, min_interval_s=0.5)
    dk = DigiKey(logger, session, dry_run=args.dry_run)

    if args.list_categories:
        tree = dk.categories()
        print_categories(tree, args.grep)
        logger.info("full tree cached at cache/digikey/categories.json")
        return 0

    if args.federal_images:
        return fetch_federal_item_images(dk, logger, dry_run=args.dry_run)

    aliases = Aliases()
    fsc_map = load_fsc_map(logger)
    fsc_rows = {r["fsc"]: r for _, r in fsc_map.iterrows()}

    items = read_data_csv("federal_items.csv", required=False, logger=logger)
    if items is None:
        logger.warning("data/federal_items.csv not found — using target items (thin-slice mode)")
        items = load_target_items(logger, dropped)
    if args.nsn:
        # match on digits so dashed and 13-digit spellings both work; keep the file's nsn value as the FK
        want = normalize_nsn(args.nsn)
        items = items[items["nsn"].map(normalize_nsn) == want]
        if len(items) == 0:
            raise SystemExit(f"NSN {args.nsn} not in the item list")
    profiles = load_profiles(logger, aliases, dropped)
    category_names = leaf_names(dk, logger)

    all_rows: list[dict] = []
    for _, it in items.iterrows():
        nsn, fsc = it["nsn"], it["fsc"] or (normalize_nsn(it["nsn"]) or "")[:4]
        fm = fsc_rows.get(fsc)
        if fm is None and not args.category:
            dropped.drop(f"fsc {fsc} not in fsc_category_map.csv", pd.DataFrame([it]), "fsc_map")
            continue
        categories = split_list(re.sub(r"[|,]", ";", args.category)) if args.category else (
            split_list(fm["digikey_category_id"]) if fm is not None else [])
        driving = split_list(fm["driving_attributes"]) if fm is not None else []
        rules = parse_rules(fm["tolerance_rules"]) if fm is not None else {}
        prof_rows = profiles.get(nsn, [])
        if not prof_rows and not args.keywords:
            logger.warning("[%s] no requirement profile — keyword+category search only", nsn)
        if not categories:
            logger.warning("[%s] fsc %s has no Digi-Key category — keyword-only search across the catalogue", nsn, fsc)
        rows, mode = search_item(dk, aliases, logger, nsn, it["item_name"], categories, driving, rules, prof_rows,
                                 keywords_override=args.keywords, category_names=category_names)
        if not rows and not args.dry_run:
            dropped.drop("no candidates returned by Digi-Key", pd.DataFrame([it]), f"search:{mode}")
            continue
        truncated = rows[CAP_PER_ITEM:]
        if truncated:
            logger.info("[%s] %d candidates, keeping top %d by closeness", nsn, len(rows), CAP_PER_ITEM)
        all_rows.extend(rows[:CAP_PER_ITEM])

    if args.dry_run:
        logger.info("dry run complete — nothing written")
        return 0

    out = pd.DataFrame(all_rows, columns=CAND_COLS + ["closeness_score", "search_mode"])
    # idempotent merge: keep existing rows for NSNs not searched in this run (e.g. --nsn mode)
    existing = DATA_DIR / "candidates.csv"
    if existing.exists() and args.nsn:
        prev = read_data_csv(existing)
        prev = prev[~prev["nsn"].isin(set(out["nsn"]))]
        out = pd.concat([prev, out], ignore_index=True)
        logger.info("merged with existing candidates.csv (kept %d rows for other NSNs)", len(prev))
    write_data_csv(out, "candidates.csv", CAND_COLS, logger)
    images = part_images(out.to_dict("records"))
    if (DATA_DIR / "part_images.csv").exists() and args.nsn:
        prev = read_data_csv("part_images.csv", logger=logger)
        prev = prev[~prev["mpn"].isin(set(images["mpn"]))]
        images = pd.concat([prev, images], ignore_index=True)
        logger.info("merged with existing part_images.csv (kept %d rows for other MPNs)", len(prev))
    write_data_csv(images, "part_images.csv", IMAGE_COLS, logger)
    if len(out):
        log_rows(logger, "items with candidates", out["nsn"].nunique())
        logger.info("search modes: %s", out["search_mode"].value_counts().to_dict())
        logger.info("part images: %d of %d distinct MPNs have a PhotoUrl",
                    len(images), out["candidate_mpn"].nunique())
    aliases.report(logger)
    logger.info(session.summary())
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
