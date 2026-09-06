"""
Shared helpers for the pipeline scripts (S1–S11).

Not a pipeline step. Provides: paths, env loading, logging with row counts and
dropped-row reasons, CSV I/O, NSN normalisation, attribute aliasing, and a disk-caching
HTTP session with polite rate limiting and a simple bounded retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

# --------------------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"
RAW_DIR = ROOT / "raw"


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- env
_ENV_LOADED = False


def load_env() -> None:
    """Load ROOT/.env then ROOT/.env.local into os.environ. Idempotent.

    Precedence (highest first): real environment > .env.local > .env.
    A key whose value is empty in one file does not shadow a non-empty value
    from another, so `.env` can list every variable while `.env.local` fills
    in the secrets.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    merged: dict[str, str] = {}
    for name in (".env", ".env.local"):
        env_path = ROOT / name
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and (value or key not in merged):
                merged[key] = value
    for key, value in merged.items():
        if key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    load_env()
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(
            f"Missing required environment variable {name}. See .env.example."
        )
    return value


def env_int(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SystemExit(f"Environment variable {name}={raw!r} is not an integer.")


def contact_email() -> str:
    return env("PIPELINE_CONTACT_EMAIL", "unset@example.com") or "unset@example.com"


def user_agent() -> str:
    return f"DNHacks-part-substitution-pipeline/0.1 (hackathon research; contact: {contact_email()})"


# --------------------------------------------------------------------------- logging
def setup_logging(script_name: str) -> logging.Logger:
    logger = logging.getLogger(script_name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_rows(logger: logging.Logger, label: str, n: int) -> None:
    logger.info("rows | %-45s %8d", label, n)


class DroppedRows:
    """Collects dropped rows with reasons; writes them to cache/dropped/<script>.csv."""

    def __init__(self, logger: logging.Logger, script_name: str):
        self.logger = logger
        self.script_name = script_name
        self.frames: list[pd.DataFrame] = []
        self.counts: dict[str, int] = {}

    def drop(self, reason: str, rows: pd.DataFrame | int, stage: str = "") -> None:
        if isinstance(rows, int):
            n = rows
            if n == 0:
                return
        else:
            n = len(rows)
            if n == 0:
                return
            frame = rows.copy()
            frame.insert(0, "_drop_reason", reason)
            frame.insert(0, "_stage", stage)
            self.frames.append(frame)
        self.counts[reason] = self.counts.get(reason, 0) + n
        self.logger.warning("dropped | %-45s %8d  (%s)", reason, n, stage or "-")

    def finish(self) -> None:
        out_dir = CACHE_DIR / "dropped"
        ensure_dirs(out_dir)
        path = out_dir / f"{self.script_name}_dropped.csv"
        if self.frames:
            pd.concat(self.frames, ignore_index=True).astype(str).to_csv(path, index=False)
            self.logger.info("dropped rows written to %s", path.relative_to(ROOT))
        elif path.exists():
            path.unlink()
        total = sum(self.counts.values())
        self.logger.info("dropped total: %d across %d reasons", total, len(self.counts))


# --------------------------------------------------------------------------- CSV I/O
def read_csv_str(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read a CSV with every column as string and no NaN coercion ('' stays '')."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, **kwargs)


def read_data_csv(name: str, required: bool = True, logger: logging.Logger | None = None) -> pd.DataFrame | None:
    path = DATA_DIR / name
    if not path.exists():
        if required:
            raise SystemExit(f"Required input data/{name} does not exist. Run the earlier step first.")
        if logger:
            logger.info("optional input data/%s not present", name)
        return None
    df = read_csv_str(path)
    if logger:
        log_rows(logger, f"read data/{name}", len(df))
    return df


def write_data_csv(df: pd.DataFrame, name: str, columns: list[str], logger: logging.Logger) -> Path:
    """Write df to data/<name> with exactly `columns` in order (contract enforcement)."""
    ensure_dirs(DATA_DIR)
    out = df.copy()
    for c in columns:
        if c not in out.columns:
            out[c] = ""
    extra = [c for c in out.columns if c not in columns]
    if extra:
        logger.info("data/%s: keeping %d extra non-contract column(s): %s", name, len(extra), ", ".join(extra))
    out = out[columns + extra]
    path = DATA_DIR / name
    out.to_csv(path, index=False)
    log_rows(logger, f"wrote data/{name}", len(out))
    return path


# --------------------------------------------------------------------------- NSN helpers
_NON_DIGIT = re.compile(r"\D+")


def normalize_nsn(raw: str) -> str | None:
    """'5905-00-123-4567' -> '5905001234567'. Returns None if not 13 digits."""
    digits = _NON_DIGIT.sub("", str(raw or ""))
    return digits if len(digits) == 13 else None


def niin_from_nsn(nsn13: str) -> str:
    return nsn13[4:]


def fsc_from_nsn(nsn13: str) -> str:
    return nsn13[:4]


def format_nsn(nsn13: str) -> str:
    return f"{nsn13[:4]}-{nsn13[4:6]}-{nsn13[6:9]}-{nsn13[9:]}"


def load_target_items(logger: logging.Logger, dropped: DroppedRows | None = None) -> pd.DataFrame:
    """config/target_items.csv, else config/target_items.sample.csv. NSNs normalised to 13 digits."""
    path = CONFIG_DIR / "target_items.csv"
    if not path.exists():
        path = CONFIG_DIR / "target_items.sample.csv"
        logger.warning("config/target_items.csv not found — using %s", path.name)
    df = read_csv_str(path)
    for col in ("nsn", "fsc", "item_name", "notes"):
        if col not in df.columns:
            df[col] = ""
    df["nsn_raw"] = df["nsn"]
    df["nsn"] = df["nsn_raw"].map(lambda s: normalize_nsn(s) or "")
    bad = df[df["nsn"] == ""]
    if len(bad):
        if dropped:
            dropped.drop("target nsn is not 13 digits", bad, "load_target_items")
        df = df[df["nsn"] != ""]
    df = df.copy()
    df["fsc"] = [f if f else fsc_from_nsn(n) for f, n in zip(df["fsc"], df["nsn"])]
    dupes = df[df.duplicated("nsn", keep="first")]
    if len(dupes):
        if dropped:
            dropped.drop("duplicate target nsn", dupes, "load_target_items")
        df = df.drop_duplicates("nsn", keep="first")
    log_rows(logger, f"target items ({path.name})", len(df))
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- attribute aliasing
_PUNCT = re.compile(r"[^a-z0-9]+")


def norm_key(s: str) -> str:
    return _PUNCT.sub(" ", str(s or "").lower()).strip()


def slugify(s: str) -> str:
    return _PUNCT.sub("_", str(s or "").lower()).strip("_")


class Aliases:
    """config/attribute_aliases.csv: (source, raw_name) -> canonical_name."""

    def __init__(self) -> None:
        path = CONFIG_DIR / "attribute_aliases.csv"
        self.map: dict[tuple[str, str], str] = {}
        if path.exists():
            df = read_csv_str(path)
            for src, raw, canon in zip(df["source"], df["raw_name"], df["canonical_name"]):
                self.map[(src.strip().lower(), norm_key(raw))] = canon.strip()
        self.unaliased: dict[tuple[str, str], int] = {}

    def canonical(self, source: str, raw_name: str) -> tuple[str, bool]:
        key = (source.lower(), norm_key(raw_name))
        if key in self.map:
            return self.map[key], True
        self.unaliased[key] = self.unaliased.get(key, 0) + 1
        return slugify(raw_name), False

    def report(self, logger: logging.Logger, top: int = 25) -> None:
        if not self.unaliased:
            logger.info("aliases: every attribute name was recognised")
            return
        items = sorted(self.unaliased.items(), key=lambda kv: -kv[1])[:top]
        logger.warning("aliases: %d unrecognised attribute names (top %d shown; add to config/attribute_aliases.csv):",
                       len(self.unaliased), len(items))
        for (src, raw), n in items:
            logger.warning("  %-8s %-50s x%d", src, raw, n)


# --------------------------------------------------------------------------- caching HTTP
def request_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


class CacheMiss(Exception):
    pass


class CachedSession:
    """
    HTTP session that caches every response body to cache/<namespace>/<hash>.json on
    arrival and consults the cache before every request. Rate-limited (min seconds between
    live requests) and with a simple bounded retry on 429/5xx.

    Cached entries store: url, method, request payload, status, headers subset, body (json or text).
    """

    def __init__(self, namespace: str, logger: logging.Logger, min_interval_s: float = 1.0,
                 max_attempts: int = 3, timeout_s: int = 60):
        self.namespace = namespace
        self.logger = logger
        self.dir = CACHE_DIR / namespace
        ensure_dirs(self.dir)
        self.min_interval_s = min_interval_s
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent()
        self._last_request_t = 0.0
        self.live_requests = 0
        self.cache_hits = 0

    # -- cache primitives
    def cache_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def read_cache(self, key: str) -> dict | None:
        p = self.cache_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                self.logger.warning("corrupt cache file %s — will re-fetch", p)
        return None

    def write_cache(self, key: str, entry: dict) -> None:
        tmp = self.cache_path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=1, default=str))
        tmp.replace(self.cache_path(key))

    # -- rate limiting
    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)

    # -- main entry
    def request(self, method: str, url: str, *, key: str | None = None, headers: dict | None = None,
                params: dict | None = None, json_body: Any = None, data: Any = None,
                ok_statuses: Iterable[int] = (200,), force: bool = False) -> dict:
        """
        Returns the cache entry dict: {"status", "body", "from_cache", ...}.
        `key` defaults to a hash of (method, url, params, json_body, data).
        Raises RuntimeError after max_attempts failures.
        """
        if key is None:
            key = request_hash({"m": method, "u": url, "p": params, "j": json_body, "d": data})
        if not force:
            cached = self.read_cache(key)
            if cached is not None:
                self.cache_hits += 1
                cached["from_cache"] = True
                return cached

        last_err: str = ""
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            self._last_request_t = time.monotonic()
            self.live_requests += 1
            try:
                resp = self.session.request(method, url, headers=headers, params=params, json=json_body,
                                            data=data, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = f"network error: {exc}"
                self.logger.warning("%s %s attempt %d/%d: %s", method, url, attempt, self.max_attempts, last_err)
                time.sleep(min(2 ** attempt, 10))
                continue

            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text

            entry = {
                "key": key, "method": method, "url": url, "params": params, "json_body": json_body,
                "status": resp.status_code, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "body": body, "from_cache": False,
            }
            if resp.status_code in ok_statuses:
                self.write_cache(key, entry)
                return entry
            last_err = f"HTTP {resp.status_code}: {str(body)[:300]}"
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 15)
                self.logger.warning("%s %s attempt %d/%d: %s — retrying in %.0fs", method, url, attempt,
                                    self.max_attempts, last_err, sleep_s)
                time.sleep(sleep_s)
                continue
            # non-retryable: cache the failure too so we can inspect it, but under a distinct name
            self.write_cache(f"{key}.error", entry)
            raise RuntimeError(f"{method} {url} failed: {last_err}")
        raise RuntimeError(f"{method} {url} failed after {self.max_attempts} attempts: {last_err}")

    def download(self, url: str, dest: Path, headers: dict | None = None) -> bool:
        """Stream a binary file to dest if it does not already exist. Returns True if fetched live."""
        if dest.exists() and dest.stat().st_size > 0:
            self.cache_hits += 1
            return False
        ensure_dirs(dest.parent)
        last_err = ""
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            self._last_request_t = time.monotonic()
            self.live_requests += 1
            try:
                with self.session.get(url, headers=headers, stream=True, timeout=self.timeout_s) as resp:
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code}"
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                            time.sleep(min(2 ** attempt, 15))
                            continue
                        raise RuntimeError(f"GET {url} failed: {last_err}")
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(1 << 16):
                            fh.write(chunk)
                    tmp.replace(dest)
                    return True
            except requests.RequestException as exc:
                last_err = f"network error: {exc}"
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"GET {url} failed after {self.max_attempts} attempts: {last_err}")

    def summary(self) -> str:
        return f"{self.namespace}: {self.live_requests} live request(s), {self.cache_hits} cache hit(s)"


# --------------------------------------------------------------------------- spec / requirement helpers
_SPEC_NUM = re.compile(r"(\d{3,6})")


def spec_key(spec_ref: str) -> str:
    """Join key between a PUB LOG spec reference number and a spec id.
    'M55342K06B10E0R' -> '55342'; 'MIL-PRF-55342/6' -> '55342'; 'MS3106' -> '3106'."""
    m = _SPEC_NUM.search(str(spec_ref or ""))
    return m.group(1) if m else ""


ENV_ATTRS = {"operating_temp", "operating_temp_min", "operating_temp_max", "temperature_coefficient"}
MECH_ATTRS = {"package_case", "mounting_type", "terminal_type"}
REQUIREMENT_CLASSES = {"electrical", "mechanical", "environmental", "qualification", "traceability"}


def infer_class(attr: str, given: str = "") -> str:
    g = (given or "").strip().lower()
    if g in REQUIREMENT_CLASSES:
        return g
    if attr == "qualification":
        return "qualification"
    if attr in ENV_ATTRS:
        return "environmental"
    if attr in MECH_ATTRS:
        return "mechanical"
    return "electrical"


# --------------------------------------------------------------------------- misc
def parse_rules(rule_str: str) -> dict[str, tuple[str, str]]:
    """'resistance:pct:5;power_rating:gte' -> {'resistance': ('pct','5'), 'power_rating': ('gte','')}"""
    out: dict[str, tuple[str, str]] = {}
    for part in str(rule_str or "").split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        attr = bits[0].strip()
        rule = bits[1].strip() if len(bits) > 1 else "exact"
        arg = bits[2].strip() if len(bits) > 2 else ""
        out[attr] = (rule, arg)
    return out


def split_list(s: str, sep: str = ";") -> list[str]:
    return [x.strip() for x in str(s or "").split(sep) if x.strip()]


def load_fsc_map(logger: logging.Logger) -> pd.DataFrame:
    path = CONFIG_DIR / "fsc_category_map.csv"
    if not path.exists():
        raise SystemExit("config/fsc_category_map.csv is required (hand-authored by the ME).")
    df = read_csv_str(path)
    df["fsc"] = df["fsc"].str.strip()
    # The documented list separator is ';' but the hand-authored file may use '|'. Canonicalise so
    # the rest of the pipeline sees one format: 'a;b;c' and 'attr:pct:N' (within_pct is an alias).
    _RULE_ALIASES = {"within_pct": "pct", "pct": "pct", "gte": "gte", "lte": "lte", "exact": "exact"}
    for col in ("digikey_category_id", "driving_attributes", "tolerance_rules"):
        if col in df.columns:
            df[col] = df[col].map(lambda s: ";".join(x.strip() for x in re.split(r"[;|]", str(s or "")) if x.strip()))
    if "tolerance_rules" in df.columns:
        def _canon_rules(s: str) -> str:
            parts = []
            for part in split_list(s):
                bits = [b.strip() for b in part.split(":")]
                if len(bits) > 1:
                    if bits[1] not in _RULE_ALIASES:
                        raise SystemExit(f"config/fsc_category_map.csv: unknown tolerance rule {bits[1]!r} in {part!r} "
                                         f"(allowed: pct|within_pct, gte, lte, exact)")
                    bits[1] = _RULE_ALIASES[bits[1]]
                parts.append(":".join(bits))
            return ";".join(parts)
        df["tolerance_rules"] = df["tolerance_rules"].map(_canon_rules)
    log_rows(logger, "fsc_category_map rows", len(df))
    return df
