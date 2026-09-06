#!/usr/bin/env python
"""
S10 — usaspending_prices.py

Historical contract pricing per NSN from the USAspending API (no API key), for the cost
side of S9's gov_unit_price / gov_quantity / price_delta_indicative (SCHEMA §3 — indicative, not savings).

Inputs:  data/federal_items.csv (S1)  [falls back to config/target_items(.sample).csv]
Output:  data/price_history.csv  price_id, nsn, fiscal_year, unit_price_avg, quantity, vendor_cage, contract_count
         price_id = "{nsn}__{fiscal_year}" ("{nsn}__none" for the no-history null row) — SCHEMA §1 surrogate PK
         (+ total_obligation, transaction_count, vendor_name, vendor_uei, price_basis extras)
Cache:   cache/usaspending/<request-hash>.json — every page of every response, checked first.

How it works
  POST /api/v2/search/spending_by_transaction/ with a `description` substring filter, once per
  NSN spelling variant (5905-01-449-2399 | 5905 01-449-2399 | 5905014492399), contracts only
  (award types A/B/C/D), FY2008 onward (the API's floor). Every returned transaction is
  re-verified locally: its description must contain the 13 NSN digits, or it is dropped and logged.
  Transactions are grouped by federal fiscal year (Oct–Sep).

What USAspending can and cannot supply (verified live 2026-09-05)
  - Obligation amount, action date, recipient name/UEI, award id: yes.
  - Quantity and unit price: NOT fields. DLA's own bulk buys are described as
    "<PR number>!<ITEM NAME>" with no NSN at all, so they are invisible to an NSN search; the
    hits are mostly other services (e.g. Coast Guard) whose free-text descriptions include the
    NSN and sometimes "QTY: 4". `quantity` is parsed from that text when present; `unit_price_avg`
    is total obligation / total parsed quantity over the transactions that state a quantity,
    else NULL. price_basis says which. Nothing is ever estimated.
  - CAGE code: not available (UEI only). vendor_cage is left empty; vendor_name / vendor_uei of
    the top recipient by obligation are carried as extras.
  If an NSN has no history at all, one row with nsn set and everything else empty is emitted,
  so Foundry can distinguish "no savings" from "unknown savings" (PRD).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CachedSession, DATA_DIR, DroppedRows, format_nsn, load_target_items, log_rows, normalize_nsn, read_data_csv,
    request_hash, setup_logging, write_data_csv,
)

SCRIPT = "s10_usaspending_prices"
API = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
CONTRACT_TYPES = ["A", "B", "C", "D"]
EARLIEST = "2007-10-01"          # API floor for search endpoints
PAGE_LIMIT = 100
MAX_PAGES_PER_QUERY = 10         # bounded: 1,000 transactions per NSN variant is far beyond demo needs
FIELDS = ["Award ID", "Mod", "Recipient Name", "Recipient UEI", "Action Date", "Transaction Amount",
          "Transaction Description", "Awarding Agency", "Awarding Sub Agency", "PSC"]
PRICE_COLS = ["price_id", "nsn", "fiscal_year", "unit_price_avg", "quantity", "vendor_cage", "contract_count"]
EXTRA_COLS = ["total_obligation", "transaction_count", "vendor_name", "vendor_uei", "price_basis"]

_QTY = re.compile(r"\b(?:QTY|QUANTITY|QUAN)\b\s*[:.#-]?\s*(\d{1,7})\b", re.IGNORECASE)
_QTY_EA = re.compile(r"\b(\d{1,7})\s*(?:EA|EACH|PCS|PIECES|UNITS?)\b", re.IGNORECASE)


# ------------------------------------------------------------------ pure helpers
def nsn_variants(nsn13: str) -> list[str]:
    """Spellings seen in award descriptions: dashed, space-after-FSC, and bare 13 digits."""
    dashed = format_nsn(nsn13)
    return [dashed, f"{nsn13[:4]} {dashed[5:]}", nsn13]


def fiscal_year(action_date: str) -> int | None:
    """US federal fiscal year: Oct 1 – Sep 30, named for the calendar year in which it ends."""
    try:
        y, m = int(action_date[:4]), int(action_date[5:7])
    except (TypeError, ValueError):
        return None
    return y + 1 if m >= 10 else y


def description_has_nsn(description: str, nsn13: str) -> bool:
    return nsn13 in re.sub(r"\D", "", description or "")


def parse_quantity(description: str) -> int | None:
    """'... QTY: 04' -> 4. Only explicit quantity phrases; never inferred."""
    for rx in (_QTY, _QTY_EA):
        m = rx.search(description or "")
        if m:
            q = int(m.group(1))
            return q if q > 0 else None
    return None


def aggregate(nsn13: str, txs: list[dict]) -> list[dict]:
    """Group verified transactions by fiscal year. One null row if there are none."""
    if not txs:
        return [{"nsn": nsn13, "fiscal_year": "", "unit_price_avg": "", "quantity": "", "vendor_cage": "",
                 "contract_count": "", "total_obligation": "", "transaction_count": "", "vendor_name": "",
                 "vendor_uei": "", "price_basis": "no_history"}]
    by_fy: dict[int, list[dict]] = {}
    for t in txs:
        fy = fiscal_year(t.get("Action Date") or "")
        if fy is None:
            continue
        by_fy.setdefault(fy, []).append(t)
    rows: list[dict] = []
    for fy in sorted(by_fy):
        group = by_fy[fy]
        amounts = [float(t.get("Transaction Amount") or 0.0) for t in group]
        total = sum(amounts)
        awards = {t.get("Award ID") for t in group if t.get("Award ID")}
        # unit price only from transactions that state a quantity AND have a positive obligation
        priced = [(a, parse_quantity(t.get("Transaction Description") or ""))
                  for a, t in zip(amounts, group) if a > 0]
        priced = [(a, q) for a, q in priced if q]
        qty_total = sum(q for _, q in priced)
        if qty_total > 0:
            unit_price = f"{sum(a for a, _ in priced) / qty_total:.4f}"
            basis = f"obligation/quantity over {len(priced)} of {len(group)} transactions stating a quantity"
        else:
            unit_price, basis = "", "no quantity stated in any transaction description"
        # top recipient by obligation this FY (USAspending has no CAGE; UEI + name are what exists)
        by_vendor: dict[tuple[str, str], float] = {}
        for a, t in zip(amounts, group):
            k = (t.get("Recipient Name") or "", t.get("Recipient UEI") or "")
            by_vendor[k] = by_vendor.get(k, 0.0) + a
        (vname, vuei), _ = max(by_vendor.items(), key=lambda kv: kv[1])
        rows.append({
            "nsn": nsn13, "fiscal_year": str(fy), "unit_price_avg": unit_price,
            "quantity": str(qty_total) if qty_total > 0 else "", "vendor_cage": "",
            "contract_count": str(len(awards)), "total_obligation": f"{total:.2f}",
            "transaction_count": str(len(group)), "vendor_name": vname, "vendor_uei": vuei, "price_basis": basis,
        })
    return rows


# ------------------------------------------------------------------ API
def search_variant(session: CachedSession, variant: str, logger) -> list[dict]:
    """All transaction rows whose description contains `variant` (bounded pagination, cached per page)."""
    out: list[dict] = []
    for page in range(1, MAX_PAGES_PER_QUERY + 1):
        body = {
            "filters": {
                "description": variant,
                "award_type_codes": CONTRACT_TYPES,
                "time_period": [{"start_date": EARLIEST, "end_date": date.today().isoformat()[:4] + "-12-31"}],
            },
            "fields": FIELDS, "page": page, "limit": PAGE_LIMIT, "sort": "Action Date", "order": "desc",
        }
        # cache key excludes the end_date so a re-run tomorrow still hits the cache
        key_body = {k: v for k, v in body.items() if k != "filters"} | {"description": variant, "types": CONTRACT_TYPES}
        entry = session.request("POST", API, key=f"tx_{request_hash(key_body)}", json_body=body,
                                headers={"Content-Type": "application/json"})
        payload = entry["body"] if isinstance(entry["body"], dict) else {}
        results = payload.get("results") or []
        out.extend(results)
        meta = payload.get("page_metadata") or {}
        if not meta.get("hasNext"):
            break
        if page == MAX_PAGES_PER_QUERY:
            logger.warning("  %r: more than %d pages of transactions — truncated (raise MAX_PAGES_PER_QUERY if needed)",
                           variant, MAX_PAGES_PER_QUERY)
    return out


def fetch_nsn(session: CachedSession, nsn13: str, logger, dropped: DroppedRows) -> list[dict]:
    seen: dict[str, dict] = {}
    unverified: list[dict] = []
    for variant in nsn_variants(nsn13):
        rows = search_variant(session, variant, logger)
        for r in rows:
            rid = str(r.get("internal_id") or r.get("generated_internal_id") or f"{r.get('Award ID')}|{r.get('Mod')}|{r.get('Action Date')}")
            if rid in seen:
                continue
            if not description_has_nsn(r.get("Transaction Description") or "", nsn13):
                unverified.append({"nsn": nsn13, "variant": variant, **{k: r.get(k) for k in FIELDS}})
                continue
            seen[rid] = r
    if unverified:
        dropped.drop("transaction matched the substring filter but description lacks the NSN digits",
                     pd.DataFrame(unverified), f"verify:{nsn13}")
    return list(seen.values())


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nsn", help="only this NSN (13 digits or dashed); merges into an existing price_history.csv")
    args = ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    session = CachedSession("usaspending", logger, min_interval_s=1.0)

    items = read_data_csv("federal_items.csv", required=False, logger=logger)
    if items is None:
        logger.warning("data/federal_items.csv not found — using target items")
        items = load_target_items(logger, dropped)
    # The file's nsn value (dashed or 13-digit) is the FK every downstream join uses; the 13-digit
    # form is only for building USAspending queries and verifying descriptions.
    nsns: dict[str, str] = {}
    for n in items["nsn"]:
        n13 = normalize_nsn(n)
        if n and n13 and n not in nsns:
            nsns[n] = n13
        elif n and not n13:
            dropped.drop("nsn is not 13 digits", pd.DataFrame([{"nsn": n}]), "federal_items")
    if args.nsn:
        want = normalize_nsn(args.nsn)
        nsns = {k: v for k, v in nsns.items() if v == want}
        if not nsns:
            raise SystemExit(f"NSN {args.nsn} is not in the item list")
    log_rows(logger, "NSNs to price", len(nsns))

    all_rows: list[dict] = []
    with_history = 0
    for i, (nsn_key, nsn13) in enumerate(nsns.items(), start=1):
        txs = fetch_nsn(session, nsn13, logger, dropped)
        rows = aggregate(nsn13, txs)
        for r in rows:
            r["nsn"] = nsn_key
        priced_years = sum(1 for r in rows if r["unit_price_avg"])
        fy_count = len(rows) if txs else 0
        if txs:
            with_history += 1
        logger.info("[%d/%d] %s: %d verified transaction(s) across %d FY; unit price derivable for %d FY",
                    i, len(nsns), format_nsn(nsn13), len(txs), fy_count, priced_years)
        all_rows.extend(rows)

    out = pd.DataFrame(all_rows, columns=PRICE_COLS + EXTRA_COLS)
    existing = DATA_DIR / "price_history.csv"
    if existing.exists() and args.nsn:
        prev = read_data_csv("price_history.csv")
        prev = prev[~prev["nsn"].isin(set(out["nsn"]))]
        out = pd.concat([prev, out], ignore_index=True)
        logger.info("merged with existing price_history.csv (kept %d rows for other NSNs)", len(prev))
    out = out.sort_values(["nsn", "fiscal_year"]).reset_index(drop=True)
    # SCHEMA §1: surrogate PK. One row per (nsn, fiscal_year); the no-history null row gets fiscal_year "none".
    out["price_id"] = [f"{n}__{fy or 'none'}" for n, fy in zip(out["nsn"], out["fiscal_year"])]
    if out["price_id"].duplicated().any():
        raise SystemExit("price_id is not unique — more than one row per (nsn, fiscal_year); inspect the aggregation")
    write_data_csv(out, "price_history.csv", PRICE_COLS, logger)
    logger.info("NSNs with any contract history: %d of %d; fiscal-year rows with a derivable unit price: %d of %d",
                with_history, len(nsns), int((out["unit_price_avg"] != "").sum()), len(out))
    if int((out["unit_price_avg"] != "").sum()) == 0:
        logger.warning("no unit prices could be derived — gov_unit_price and price_delta_indicative will be null throughout (UI falls back "
                       "to sorting by commercial price). USAspending has no quantity field; see module docstring.")
    logger.info(session.summary())
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
