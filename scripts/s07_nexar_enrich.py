#!/usr/bin/env python
"""
S7 — nexar_enrich.py

Enrichment ONLY — never discovery. Takes the top-N (default 3) candidates per federal item as
ranked by S9's first pass and asks Nexar/Octopart for what Digi-Key cannot supply:
cross-distributor lifecycle status and multi-source availability (plus a median price).

QUOTA-GOVERNED. Nexar meters MATCHED PARTS, not calls: 100 for the entire project lifetime
(NEXAR_LIFETIME_BUDGET). Batching MPNs into one query saves nothing, so this script sends one
MPN per request, caches every response at cache/nexar/<mpn>.json on arrival, and consults the
cache before every request. Enforcement is in code, not discipline:
  - cache/nexar_usage.json persists the matched-part counter (incremented per matched part,
    written after every live response);
  - the number of UNCACHED MPNs this run would query is computed up front and the script RAISES
    (not warns) if it exceeds NEXAR_MAX_MPNS_PER_RUN or would push the lifetime counter past
    NEXAR_LIFETIME_BUDGET — before a single request is sent;
  - the counter is re-checked before every live request.
The S6 -> S7 handoff is capped here: only rows present in data/substitution_candidates.csv
(S9 output) with rank_within_nsn <= N are eligible. data/candidates.csv is never read for
selection, so an unfiltered Digi-Key result set cannot reach Nexar.

Inputs:  data/substitution_candidates.csv (S9 first pass) — required; the ranking is the handoff
         env NEXAR_ACCESS_TOKEN (optional Bearer; skips the identity server) or
             NEXAR_CLIENT_ID + NEXAR_CLIENT_SECRET, plus NEXAR_LIFETIME_BUDGET,
             NEXAR_MAX_MPNS_PER_RUN, NEXAR_PRO_FIELDS (1 = request specs/bestDatasheet)
Output:  data/commercial_parts.csv  mpn, manufacturer, lifecycle_status, median_price, stock_qty,
                                    lead_time_days, datasheet_url, distributor_count
         (+ nexar_part_id, matched_mpn, seller_count, authorized_distributor_count, octopart_url,
            fetched_at extras)
         Then run S8 (-> commercial_parts_normalized.csv) and S9 again for final scores.

Utilities:
  --dry-run              show the selection and the budget arithmetic; no network, no counter change
  --verify-counting MPN  query one MPN twice, bypassing the cache, so you can read the usage meter
                         on portal.nexar.com: +1 => distinct-part counting (re-queries free),
                         +2 => per-query counting (the cache is load-bearing). Costs <= 2 parts.
                         Do this ONCE before the first real run (PRD).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CACHE_DIR, CachedSession, DroppedRows, env, env_int, ensure_dirs, log_rows, read_data_csv, setup_logging,
    write_data_csv,
)
from s08_normalize import normalize_lifecycle  # noqa: E402

SCRIPT = "s07_nexar_enrich"
TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql"
USAGE_PATH = CACHE_DIR / "nexar_usage.json"
NEXAR_CACHE = CACHE_DIR / "nexar"
DEFAULT_TOP_N = 3

CP_COLS = ["mpn", "manufacturer", "lifecycle_status", "median_price", "stock_qty", "lead_time_days", "datasheet_url",
           "distributor_count"]
EXTRA_COLS = ["lifecycle_raw", "price_basis_qty", "nexar_part_id", "matched_mpn", "seller_count",
              "authorized_distributor_count", "octopart_url", "fetched_at"]
PRICE_BASIS_QTY = 1000   # median_price is Nexar's medianPrice1000: the median across sellers at the 1,000-unit break

# Fields behind Nexar's Tech Specs add-on. With NEXAR_PRO_FIELDS=0 they are omitted and
# datasheet_url is left empty and lifecycle_status is `unknown` (the closed enum's value for "no data").
PRO_FRAGMENT = """
      bestDatasheet { url }
      specs { attribute { shortname name } displayValue }"""

QUERY_TEMPLATE = """
query Enrich($mpn: String!) {
  supMultiMatch(queries: [{ mpn: $mpn, limit: 1 }], currency: "USD", country: "US") {
    hits
    parts {
      id
      mpn
      name
      octopartUrl
      manufacturer { name }
      medianPrice1000 { price currency quantity }
      totalAvail
      sellers {
        isAuthorized
        company { name }
        offers {
          inventoryLevel
          factoryLeadDays
          prices { quantity price currency }
        }
      }%s
    }
  }
}
"""


# ------------------------------------------------------------------ budget
class QuotaExceeded(RuntimeError):
    """Raised BEFORE any request that would exceed a Nexar ceiling. Never downgraded to a warning."""


def load_usage() -> dict:
    if USAGE_PATH.exists():
        try:
            return json.loads(USAGE_PATH.read_text())
        except json.JSONDecodeError:
            raise SystemExit(f"{USAGE_PATH} is corrupt — fix or restore it before running S7; the counter is load-bearing.")
    return {"matched_parts_total": 0, "live_requests_total": 0, "mpns": {}, "runs": []}


def save_usage(usage: dict) -> None:
    ensure_dirs(USAGE_PATH.parent)
    tmp = USAGE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(usage, indent=1, sort_keys=True))
    tmp.replace(USAGE_PATH)


def budget_check(uncached: list[str], usage: dict, max_per_run: int, lifetime: int) -> None:
    n = len(uncached)
    if n > max_per_run:
        raise QuotaExceeded(
            f"{n} uncached MPN(s) selected for Nexar but NEXAR_MAX_MPNS_PER_RUN={max_per_run}. Nothing was sent. "
            f"Lower --top, restrict with --nsn, or raise the ceiling deliberately.")
    used = int(usage.get("matched_parts_total", 0))
    if used + n > lifetime:
        raise QuotaExceeded(
            f"lifetime counter is {used} matched parts; querying {n} more would exceed NEXAR_LIFETIME_BUDGET={lifetime}. "
            f"Nothing was sent.")


def mpn_cache_path(mpn: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", mpn)
    if safe != mpn:
        safe = f"{safe}_{hashlib.sha1(mpn.encode()).hexdigest()[:8]}"
    return NEXAR_CACHE / f"{safe}.json"


# ------------------------------------------------------------------ API
class Nexar:
    def __init__(self, logger, session: CachedSession, pro_fields: bool, dry_run: bool = False):
        self.logger = logger
        self.session = session
        self.pro_fields = pro_fields
        self.dry_run = dry_run
        supplied = (env("NEXAR_ACCESS_TOKEN") or "").strip()
        need_oauth = not dry_run and not supplied
        self.client_id = env("NEXAR_CLIENT_ID", required=need_oauth) or ""
        self.client_secret = env("NEXAR_CLIENT_SECRET", required=need_oauth) or ""
        self._token: str | None = supplied or None
        self._token_source = "access_token" if supplied else None

    def token(self) -> str:
        if self._token:
            return self._token  # supplied Bearer, or a 24h identity token already fetched this run
        resp = self.session.session.post(TOKEN_URL, data={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
            "scope": "supply.domain"}, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Nexar token request failed: HTTP {resp.status_code} {resp.text[:300]}")
        self._token = resp.json()["access_token"]
        self._token_source = "oauth"
        self.logger.info("Nexar token obtained")
        return self._token

    def query(self) -> str:
        return QUERY_TEMPLATE % (PRO_FRAGMENT if self.pro_fields else "")

    def match_live(self, mpn: str, force: bool = False) -> dict:
        """One live request for one MPN. Returns the GraphQL `data.supMultiMatch[0]` block; raises on errors."""
        body = {"query": self.query(), "variables": {"mpn": mpn}}
        entry = self.session.request("POST", GRAPHQL_URL, key=f"raw_{hashlib.sha1(mpn.encode()).hexdigest()[:16]}",
                                     headers={"Authorization": f"Bearer {self.token()}",
                                              "Content-Type": "application/json"},
                                     json_body=body, force=force)
        payload = entry["body"] if isinstance(entry["body"], dict) else {}
        if payload.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if self.pro_fields and re.search(r"does not exist|not exist on type|Cannot query field|unauthori", msgs, re.I):
                raise RuntimeError(
                    f"Nexar rejected the query ({msgs[:300]}). If your plan lacks the Tech Specs add-on set "
                    f"NEXAR_PRO_FIELDS=0 and re-run. (Schema errors are raised before execution, so this should "
                    f"not have matched any parts — confirm on the portal usage meter.)")
            raise RuntimeError(f"Nexar GraphQL error for {mpn}: {msgs[:400]}")
        blocks = ((payload.get("data") or {}).get("supMultiMatch")) or []
        return blocks[0] if blocks else {"hits": 0, "parts": []}


# ------------------------------------------------------------------ response -> row
def _f(x) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def lifecycle_from_specs(part: dict) -> str:
    for s in part.get("specs") or []:
        a = s.get("attribute") or {}
        if (a.get("shortname") or "").lower() in ("lifecyclestatus", "lifecycle_status") or \
                (a.get("name") or "").lower() == "lifecycle status":
            return s.get("displayValue") or ""
    return ""


def part_to_row(mpn: str, block: dict, fetched_at: str) -> dict:
    parts = block.get("parts") or []
    if not parts:
        return {"mpn": mpn, "manufacturer": "", "lifecycle_status": "unknown", "median_price": "", "stock_qty": "",
                "lead_time_days": "", "datasheet_url": "", "distributor_count": "", "lifecycle_raw": "",
                "price_basis_qty": "", "nexar_part_id": "",
                "matched_mpn": "", "seller_count": "0", "authorized_distributor_count": "", "octopart_url": "",
                "fetched_at": fetched_at, "_matched": 0}
    p = parts[0]
    sellers = p.get("sellers") or []
    stocked = [s for s in sellers if any((_f(o.get("inventoryLevel")) or 0) > 0 for o in (s.get("offers") or []))]
    authorized_stocked = [s for s in stocked if s.get("isAuthorized")]
    lead_days = [_f(o.get("factoryLeadDays")) for s in sellers for o in (s.get("offers") or [])]
    lead_days = [d for d in lead_days if d is not None and d >= 0]
    median = (p.get("medianPrice1000") or {}).get("price")
    total_avail = _f(p.get("totalAvail"))
    life_raw = lifecycle_from_specs(p)
    return {
        "mpn": mpn,
        "manufacturer": (p.get("manufacturer") or {}).get("name") or "",
        "lifecycle_status": normalize_lifecycle(life_raw),          # closed enum (SCHEMA §8)
        "median_price": "" if median is None else f"{float(median):.6g}",
        "stock_qty": "" if total_avail is None else str(int(total_avail)),
        "lead_time_days": "" if not lead_days else str(int(min(lead_days))),
        "datasheet_url": (p.get("bestDatasheet") or {}).get("url") or "",
        "distributor_count": str(len(stocked)),
        "lifecycle_raw": life_raw,                                   # Nexar's wording, for audit
        "price_basis_qty": "" if median is None else str(PRICE_BASIS_QTY),
        "nexar_part_id": p.get("id") or "",
        "matched_mpn": p.get("mpn") or "",
        "seller_count": str(len(sellers)),
        "authorized_distributor_count": str(len(authorized_stocked)),
        "octopart_url": p.get("octopartUrl") or "",
        "fetched_at": fetched_at,
        "_matched": len(parts),
    }


# Slots whose every candidate already fails a hard gate — lead time cannot change risk.
ZERO_SURVIVOR_NSNS = {
    "5930-00-689-2834",  # S1
    "5930-01-110-7093",  # S2
    "5930-01-110-7094",  # S3
    "5930-01-174-6031",  # S4
    "5945-00-491-3597",  # K1
    "5950-01-199-2306",  # L2
    "5950-01-224-5861",  # L3
}
_ASSEMBLY_PRIORITY = {"interconnect": 0, "power_distribution": 1, "signal_conditioning": 2}
_SIGNAL_SLOT_PRIORITY = {"CR1": 0, "L1": 1, "R5": 2, "R4": 3, "R3": 4, "R2": 5, "R1": 6}


def _demo_nsn_order(nsns: list[str]) -> list[str]:
    """Interconnect first (the click path), then power, then diodes/coils/film before the loose WW resistors."""
    slots = read_data_csv("slots.csv", required=False)
    if slots is None or len(slots) == 0:
        return list(dict.fromkeys(nsns))
    meta = {r["baseline_nsn"]: r for _, r in slots.iterrows()}
    present = list(dict.fromkeys(nsns))

    def key(nsn: str):
        row = meta.get(nsn, {})
        asm = row.get("assembly_id", "") or ""
        slot = row.get("slot_id", "") or ""
        return (_ASSEMBLY_PRIORITY.get(asm, 9), _SIGNAL_SLOT_PRIORITY.get(slot, 50), slot, nsn)

    return sorted(present, key=key)


# ------------------------------------------------------------------ selection (the S6 -> S7 handoff cap)
def select_mpns(sc: pd.DataFrame, top_n: int, only_nsn: str | None, logger,
                skip_nsns: set[str] | None = None, max_nsns: int | None = None,
                demo_path: bool = False) -> list[str]:
    df = sc.copy()
    if "rank_within_nsn" not in df.columns:
        raise SystemExit("substitution_candidates.csv has no rank_within_nsn column — re-run S9 (first pass).")
    df["_rank"] = pd.to_numeric(df["rank_within_nsn"], errors="coerce")
    if only_nsn:
        df = df[df["nsn"] == only_nsn]
    skip = {n for n in (skip_nsns or set()) if n}
    if skip:
        before = df["nsn"].nunique()
        df = df[~df["nsn"].isin(skip)]
        logger.info("skipped %d NSN(s) (%d remain) — zero-survivor / excluded slots",
                    before - df["nsn"].nunique(), df["nsn"].nunique())
    if demo_path or max_nsns:
        order = _demo_nsn_order(list(df["nsn"].unique()))
        if max_nsns is not None:
            order = order[:max_nsns]
        logger.info("enriching %d NSN(s)%s: %s", len(order), " (demo-path first)" if demo_path else "",
                    ", ".join(order))
        df = df[df["nsn"].isin(order)]
    picked = df[df["_rank"] <= top_n]
    log_rows(logger, f"candidates ranked top {top_n} per item", len(picked))
    mpns = list(dict.fromkeys(m for m in picked["candidate_mpn"] if m))
    log_rows(logger, "distinct MPNs selected for enrichment", len(mpns))
    return mpns


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="candidates per federal item (PRD: 3)")
    ap.add_argument("--nsn", help="restrict to one NSN (13 digits)")
    ap.add_argument("--skip-nsn", action="append", default=[], help="exclude an NSN (repeatable or comma-separated)")
    ap.add_argument("--max-nsns", type=int, help="cap distinct NSNs after skip + demo-path ordering")
    ap.add_argument("--demo-path", action="store_true",
                    help="order NSNs interconnect → power → signal (CR1/L1/R5 first in signal)")
    ap.add_argument("--skip-zero-survivors", action="store_true",
                    help="exclude S1–S4, K1, L2, L3 (every candidate already fails a hard gate)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-counting", metavar="MPN", help="query MPN twice live; then read the portal meter")
    args = ap.parse_args()

    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    lifetime = env_int("NEXAR_LIFETIME_BUDGET", 100)
    max_per_run = env_int("NEXAR_MAX_MPNS_PER_RUN", 90)
    pro_fields = (env("NEXAR_PRO_FIELDS", "1") or "1").strip() not in ("0", "false", "no")
    # raw HTTP bodies go to cache/nexar_raw/ (retry/throttle plumbing); the per-MPN contract cache the
    # PRD names — cache/nexar/<mpn>.json — is written explicitly below and is what governs re-runs.
    session = CachedSession("nexar_raw", logger, min_interval_s=0.5)
    nx = Nexar(logger, session, pro_fields=pro_fields, dry_run=args.dry_run)
    if not args.dry_run:
        logger.info("Nexar auth: %s", "supplied Bearer token" if nx._token_source == "access_token"
                    else "client credentials (identity server)")
    usage = load_usage()
    logger.info("Nexar lifetime counter: %d of %d matched parts used; per-run ceiling %d; pro fields %s",
                usage.get("matched_parts_total", 0), lifetime, max_per_run, "on" if pro_fields else "off")

    # ---- counting-basis verification (PRD: do this once before the first real run)
    if args.verify_counting:
        mpn = args.verify_counting.strip()
        budget_check([mpn, mpn], usage, max_per_run, lifetime)
        before = usage["matched_parts_total"]
        for i in (1, 2):
            block = nx.match_live(mpn, force=True)
            matched = len(block.get("parts") or [])
            usage["matched_parts_total"] += matched
            usage["live_requests_total"] = usage.get("live_requests_total", 0) + 1
            usage["runs"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": f"verify_counting_{i}", "mpn": mpn,
                                  "matched": matched})
            save_usage(usage)
            logger.info("verify %d/2: %s matched %d part(s) (hits=%s)", i, mpn, matched, block.get("hits"))
        logger.warning("Local counter advanced by %d (worst case). NOW check the usage meter on portal.nexar.com: "
                       "+1 => distinct-part counting (re-queries are free); +2 => per-query counting (cache is "
                       "load-bearing). Record the answer in PROGRESS.md.", usage["matched_parts_total"] - before)
        return 0

    # ---- selection: only S9-ranked candidates can reach Nexar
    sc = read_data_csv("substitution_candidates.csv", required=False, logger=logger)
    if sc is None:
        raise SystemExit("data/substitution_candidates.csv not found. Run S9 (first pass) before S7 — the ranking "
                         "is the handoff cap.")
    skip: set[str] = set()
    for raw in args.skip_nsn:
        skip.update(x.strip() for x in raw.split(",") if x.strip())
    if args.skip_zero_survivors:
        skip |= ZERO_SURVIVOR_NSNS
    mpns = select_mpns(sc, args.top, args.nsn, logger, skip_nsns=skip, max_nsns=args.max_nsns,
                       demo_path=args.demo_path)
    if not mpns:
        raise SystemExit("no candidates selected — nothing to enrich")

    cached = [m for m in mpns if mpn_cache_path(m).exists()]
    uncached = [m for m in mpns if not mpn_cache_path(m).exists()]
    # previously-paid-for MPNs (cached) are always free to include, even if no longer top-N
    extra_cached = sorted({m for m in sc["candidate_mpn"] if m and m not in mpns and mpn_cache_path(m).exists()})
    logger.info("selected %d MPN(s): %d cached (free), %d uncached (cost 1 matched part each); "
                "%d further cached MPN(s) from earlier runs will be carried through",
                len(mpns), len(cached), len(uncached), len(extra_cached))

    budget_check(uncached, usage, max_per_run, lifetime)   # RAISES before any request
    if args.dry_run:
        for m in uncached:
            logger.info("DRY RUN would query Nexar for %s", m)
        logger.info("DRY RUN: budget after run would be %d of %d", usage["matched_parts_total"] + len(uncached), lifetime)
        return 0

    rows: list[dict] = []
    run_log = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "enrich", "requested": len(mpns),
               "live": 0, "matched": 0}
    for mpn in mpns + extra_cached:
        path = mpn_cache_path(mpn)
        if path.exists():
            entry = json.loads(path.read_text())
            session.cache_hits += 1
        else:
            # re-check right before spending: another process/run may have advanced the counter
            if usage["matched_parts_total"] + 1 > lifetime:
                save_usage(usage)
                raise QuotaExceeded(f"lifetime budget {lifetime} would be exceeded at {mpn}; stopping. "
                                    f"Partial results are cached; re-run to resume once the ceiling is raised.")
            block = nx.match_live(mpn)
            entry = {"mpn": mpn, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pro_fields": pro_fields,
                     "block": block}
            ensure_dirs(NEXAR_CACHE)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry, indent=1))
            tmp.replace(path)
            matched = len(block.get("parts") or [])
            usage["matched_parts_total"] += matched
            usage["live_requests_total"] = usage.get("live_requests_total", 0) + 1
            usage["mpns"][mpn] = {"first_seen": entry["fetched_at"], "matched": matched}
            run_log["live"] += 1
            run_log["matched"] += matched
            save_usage(usage)   # persisted after EVERY live response
            logger.info("  live %-28s matched %d  (lifetime %d/%d)", mpn, matched, usage["matched_parts_total"], lifetime)
        row = part_to_row(mpn, entry["block"], entry.get("fetched_at", ""))
        if row.pop("_matched") == 0:
            dropped.drop("no Nexar match for MPN", pd.DataFrame([{"mpn": mpn}]), "match")
            continue
        rows.append(row)

    usage["runs"].append(run_log)
    save_usage(usage)

    out = pd.DataFrame(rows, columns=CP_COLS + EXTRA_COLS).drop_duplicates("mpn").sort_values("mpn")
    write_data_csv(out, "commercial_parts.csv", CP_COLS, logger)
    if len(out):
        logger.info("lifecycle values seen: %s", out["lifecycle_status"].replace("", "<empty>").value_counts().to_dict())
        logger.info("single-distributor MPNs: %d; MPNs with no stocked distributor: %d",
                    int((pd.to_numeric(out["distributor_count"], errors="coerce") == 1).sum()),
                    int((pd.to_numeric(out["distributor_count"], errors="coerce") == 0).sum()))
    logger.info("%s | lifetime matched parts now %d of %d", session.summary(), usage["matched_parts_total"], lifetime)
    logger.info("next: python scripts/s08_normalize.py && python scripts/s09_score.py   (second pass)")
    dropped.finish()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QuotaExceeded as exc:
        # loud, non-zero, and distinct from every other failure
        print(f"\nQUOTA GUARD: {exc}\n", file=sys.stderr)
        raise SystemExit(3)
