#!/usr/bin/env python
"""
S4 — spec_extract.py

LLM extraction of structured qualification requirements from the specification PDFs S3 indexed.

Inputs:  data/spec_index.csv (S3) — rows with a pdf_path; or data/specs/*.pdf directly
         config/spec_requirements_overrides.csv — manual corrections, applied AFTER extraction
         config/attribute_aliases.csv — the canonical vocabulary offered to the model
         env ANTHROPIC_API_KEY, SPEC_EXTRACT_MODEL
Outputs: data/specifications.csv     spec_id, title, requirement_count            (one row per document)
                                     (+ pages, text_chars, chunks, pdf_path, sha256, status extras)
         data/spec_requirements.csv  spec_requirement_id, spec_id, requirement_name, operator, value, uom,
                                     requirement_class, confidence, source_page  (one row per requirement)
                                     (+ evidence, chunk_id, model, origin extras; origin ∈ llm | override)
         spec_requirement_id = "{spec_id}__{requirement_name}" (unique after dedupe).
         Both files are PROVENANCE ONLY (PRD): the application reads requirement_profiles.csv (S5).
Cache:   cache/anthropic/<hash>.json — one entry per (model, prompt version, spec, chunk text). A re-run
         with unchanged PDFs makes zero API calls.

Method
  1. pypdf extracts text page by page. Pages are grouped into chunks (<= CHUNK_CHARS) with explicit
     "=== PAGE n ===" markers so the model can cite source_page. A PDF with (almost) no extractable
     text is a scanned image: it is logged and skipped — no OCR here (would need a new dependency).
  2. One Messages API call per chunk asks for a JSON array only. Output is parsed defensively (code
     fences stripped, first '[' .. last ']'), every item validated: requirement_class in the PRD's
     five values, operator in the closed enum eq|gte|lte|range|in_set|boolean (symbol spellings such as ">="
     are accepted and mapped), confidence in [0,1], source_page inside the chunk. Values are then
     pre-normalised to the contract form (base unit, 'min|max' ranges) — verbatim text stays in value_raw.
     Anything invalid is dropped WITH a reason; nothing is silently coerced.
  3. Duplicates of (spec_id, requirement_name) keep the highest-confidence item.
  4. Overrides: a row in spec_requirements_overrides.csv with the same (spec_id, requirement_name)
     REPLACES the extracted row (or is appended if absent); value == DELETE removes it. Override rows
     get confidence 1.0 and origin=override unless they say otherwise.

An unverifiable requirement is worse than a missing one (PRD): confidence and source_page are
recorded for every row, and the model is told to return nothing rather than guess.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CONFIG_DIR, CachedSession, DATA_DIR, DroppedRows, env, log_rows, norm_key, read_csv_str, read_data_csv,
    setup_logging, write_data_csv, REQUIREMENT_CLASSES,
)
from s08_normalize import (  # noqa: E402
    NUMERIC_ATTRS, OPERATORS, TEXT_ATTRS, bare_value, canonical_operator, normalize_boolean, normalize_set,
    normalize_value,
)

SCRIPT = "s04_spec_extract"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
PROMPT_VERSION = "v2"            # bump to invalidate the cache when the prompt changes (v2: closed operator enum)
CHUNK_CHARS = 28_000
MAX_PAGES_DEFAULT = 80
MAX_TOKENS = 4096
MIN_TEXT_CHARS = 200             # below this the PDF is treated as scanned / image-only
SPEC_COLS = ["spec_requirement_id", "spec_id", "requirement_name", "operator", "value", "uom", "requirement_class",
             "confidence", "source_page"]
EXTRA_COLS = ["value_raw", "uom_raw", "parse_status", "evidence", "chunk_id", "model", "origin"]
SPECIFICATIONS_COLS = ["spec_id", "title", "requirement_count"]
SPECIFICATIONS_EXTRAS = ["pages", "text_chars", "chunks", "pdf_path", "sha256", "status"]


def spec_requirement_id(spec_id: str, requirement_name: str) -> str:
    return f"{spec_id}__{requirement_name}"
CANON_VOCAB = sorted(set(NUMERIC_ATTRS) | TEXT_ATTRS)

SYSTEM_PROMPT = f"""You extract qualification requirements from United States military / federal component specifications (MIL-PRF, MIL-DTL, MIL-STD, MS sheets, federal specs) for a procurement engineering tool.

Return ONLY a JSON array. No prose, no markdown fences. Each element:
{{"requirement_name": str, "operator": "gte" | "lte" | "eq" | "range" | "in_set" | "boolean", "value": str, "uom": str, "requirement_class": "electrical" | "mechanical" | "environmental" | "qualification" | "traceability", "confidence": number 0..1, "source_page": int, "evidence": str}}

Rules:
- requirement_name: use one of these canonical names when it fits: {", ".join(CANON_VOCAB)}. Otherwise a short lower_snake_case name.
- value: the number as written (e.g. "0.125"), or for ranges "lo to hi" (e.g. "-55 to 125"). Put the unit in uom, not in value. For text requirements (e.g. package, dielectric) put the text in value and leave uom empty. For in_set, pipe-delimit the allowed values ("0603|0805"). For boolean, value is "true" or "false".
- operator: "gte" for a minimum rating the part must meet or exceed; "lte" for a maximum (tolerance, TCR, minimum temperature as a ceiling); "eq" for an exact/nominal value or a text requirement; "range" only when value is "lo to hi"; "in_set" when several discrete values are allowed; "boolean" for yes/no requirements (QPL listing required, lot date code marking required) — name those `qualification` or a traceability name, value "true".
- requirement_class: electrical (resistance, capacitance, voltage, power, TCR...), mechanical (package, dimensions, mounting, terminals, marking), environmental (temperature, humidity, vibration, shock, salt spray), qualification (QPL/QML listing, qualification inspection, group A/B/C testing, conformance inspection), traceability (lot/date code, CAGE marking, certificate of conformance).
- source_page: the page number from the "=== PAGE n ===" marker where the evidence appears.
- evidence: a verbatim quote (<= 200 characters) from that page that states the requirement.
- confidence: your confidence that this is a binding requirement of the specification with the stated value. Use <= 0.5 when the text is ambiguous, tabular values are hard to attribute, or the requirement applies only to some part-number variants.
- Extract only requirements the specification imposes on the PART. Skip test procedures, definitions, packaging, ordering data, and anything not stated on these pages. If a page contains no requirements, return fewer items — never invent one.
- If nothing qualifies, return [].
"""


# ------------------------------------------------------------------ PDF text
def extract_pages(pdf_path: Path, max_pages: int, logger) -> list[tuple[int, str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is not installed: uv pip install --python .venv/bin/python -r requirements.txt")
    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    if n > max_pages:
        logger.warning("%s has %d pages; only the first %d are extracted (--max-pages)", pdf_path.name, n, max_pages)
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            txt = page.extract_text() or ""
        except Exception as exc:  # pypdf raises assorted errors on odd PDFs; skip the page, say so
            logger.warning("%s page %d: text extraction failed (%s)", pdf_path.name, i, exc)
            txt = ""
        txt = re.sub(r"[ \t]+", " ", txt).strip()
        pages.append((i, txt))
    return pages


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(DATA_DIR.parent))
    except ValueError:
        return str(path)


def pdf_title(pdf_path: Path, pages: list[tuple[int, str]], spec_id: str) -> str:
    """Document title: PDF metadata if present, else the first non-trivial line of page 1, else the spec_id."""
    try:
        from pypdf import PdfReader
        meta = PdfReader(str(pdf_path)).metadata
        t = (meta.title if meta else "") or ""
        if t.strip() and len(t.strip()) > 3:
            return t.strip()[:200]
    except Exception:
        pass
    for _, txt in pages[:1]:
        for line in txt.splitlines():
            line = line.strip()
            if len(line) >= 8 and re.search(r"[A-Za-z]{3}", line):
                return line[:200]
    return spec_id


def chunk_pages(pages: list[tuple[int, str]], chunk_chars: int = CHUNK_CHARS) -> list[tuple[str, list[int], str]]:
    """-> [(chunk_id, [page numbers], text with page markers)], skipping empty pages."""
    chunks: list[tuple[str, list[int], str]] = []
    buf: list[str] = []
    buf_pages: list[int] = []
    size = 0

    def flush() -> None:
        nonlocal buf, buf_pages, size
        if buf:
            cid = f"p{buf_pages[0]}-{buf_pages[-1]}"
            chunks.append((cid, list(buf_pages), "\n\n".join(buf)))
        buf, buf_pages, size = [], [], 0

    for pno, txt in pages:
        if not txt:
            continue
        block = f"=== PAGE {pno} ===\n{txt}"
        if size + len(block) > chunk_chars and buf:
            flush()
        buf.append(block)
        buf_pages.append(pno)
        size += len(block)
    flush()
    return chunks


# ------------------------------------------------------------------ LLM
def call_model(session: CachedSession, api_key: str, model: str, spec_id: str, chunk_id: str, text: str,
               dry_run: bool, logger) -> tuple[str | None, dict]:
    """Returns (assistant text or None on dry run, meta). Cached by (model, prompt version, spec, chunk hash)."""
    user_msg = (f"Specification: {spec_id}\nChunk: {chunk_id}\n\n{text}\n\n"
                f"Return the JSON array of requirements found on these pages.")
    key = "msg_" + hashlib.sha256(f"{model}|{PROMPT_VERSION}|{spec_id}|{chunk_id}|{text}".encode()).hexdigest()[:24]
    cached = session.read_cache(key)
    if cached is not None:
        session.cache_hits += 1
        body = cached["body"]
    elif dry_run:
        return None, {"cached": False}
    else:
        entry = session.request(
            "POST", API_URL, key=key,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"},
            json_body={"model": model, "max_tokens": MAX_TOKENS, "temperature": 0, "system": SYSTEM_PROMPT,
                       "messages": [{"role": "user", "content": user_msg}]},
        )
        body = entry["body"]
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected response body for {spec_id} {chunk_id}: {str(body)[:200]}")
    text_out = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    meta = {"cached": cached is not None, "stop_reason": body.get("stop_reason"), "usage": body.get("usage", {})}
    if body.get("stop_reason") == "max_tokens":
        logger.warning("%s %s: response hit max_tokens — the JSON may be truncated; lower CHUNK_CHARS", spec_id, chunk_id)
    return text_out, meta


def parse_json_array(text: str) -> list | None:
    """Defensive: strip fences, take first '[' .. last ']'. None if it is not a JSON array."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.MULTILINE).strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        arr = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
    return arr if isinstance(arr, list) else None


def validate_item(item: dict, spec_id: str, pages: list[int]) -> tuple[dict | None, str]:
    """-> (clean row, '') or (None, reason)."""
    if not isinstance(item, dict):
        return None, "item is not an object"
    name = str(item.get("requirement_name", "")).strip()
    if not name:
        return None, "missing requirement_name"
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    op_given = str(item.get("operator", "")).strip()
    op = canonical_operator(op_given)
    if op is None:
        return None, f"operator {op_given!r} not in {sorted(OPERATORS)}"
    value = str(item.get("value", "")).strip()
    if not value:
        return None, "empty value"
    rclass = str(item.get("requirement_class", "")).strip().lower()
    if rclass not in REQUIREMENT_CLASSES:
        return None, f"requirement_class {rclass!r} not in {sorted(REQUIREMENT_CLASSES)}"
    try:
        conf = float(item.get("confidence"))
    except (TypeError, ValueError):
        return None, "confidence is not a number"
    if not 0.0 <= conf <= 1.0:
        return None, f"confidence {conf} outside [0,1]"
    try:
        page = int(item.get("source_page"))
    except (TypeError, ValueError):
        return None, "source_page is not an integer"
    if pages and page not in pages:
        return None, f"source_page {page} is not one of the chunk's pages {pages[0]}-{pages[-1]}"
    uom_raw = str(item.get("uom", "") or "").strip()
    op, cvalue, cuom, status = contract_value(name, op, value, uom_raw)
    return {
        "spec_id": spec_id, "requirement_name": name, "operator": op, "value": cvalue, "uom": cuom,
        "requirement_class": rclass, "confidence": f"{conf:.2f}", "source_page": str(page),
        "value_raw": value, "uom_raw": uom_raw, "parse_status": status,
        "evidence": str(item.get("evidence", "") or "").strip()[:300],
    }, ""


def contract_value(name: str, op: str, value: str, uom: str) -> tuple[str, str, str, str]:
    """Pre-normalise to the contract form (SCHEMA §8/§9): base-unit number, 'min|max', pipe-delimited set,
    'true'/'false', or cleaned text. -> (operator, value, uom, parse_status). When the value cannot be parsed
    it is kept verbatim with parse_status=unparsed — S5/S8 will log it and S9 scores it unknown."""
    raw = f"{value} {uom}".strip() if uom and op not in ("in_set", "boolean") else value
    if op == "in_set":
        n = normalize_set(value, name)
    elif op == "boolean":
        n = normalize_boolean(value)
    else:
        n = normalize_value(raw, name)
        if n.kind == "unparsed" and uom:          # maybe the uom string confused the parser; try bare
            n = normalize_value(value, name)
    if n.kind in ("number", "range", "text", "set", "boolean"):
        if n.kind == "range" and op in ("eq", "gte", "lte"):
            op = "range"
        return op, bare_value(n.as_row()), n.uom or ("" if n.kind in ("text", "set", "boolean") else uom), n.kind
    return op, value, uom, n.kind


def apply_overrides(df: pd.DataFrame, overrides: pd.DataFrame, dropped: DroppedRows, logger) -> pd.DataFrame:
    if overrides is None or len(overrides) == 0:
        return df
    out = df.copy()
    out["_k"] = [(s, norm_key(n)) for s, n in zip(out["spec_id"], out["requirement_name"])]
    replaced = appended = deleted = 0
    for _, o in overrides.iterrows():
        spec_id, name = str(o.get("spec_id", "")).strip(), str(o.get("requirement_name", "")).strip()
        if not spec_id or not name:
            dropped.drop("override row without spec_id/requirement_name", pd.DataFrame([o]), "overrides")
            continue
        k = (spec_id, norm_key(name))
        # explicit elementwise tuple compare: `Series == tuple` broadcasts (and fails) on an empty frame
        hit = pd.Series([kk == k for kk in out["_k"]], index=out.index, dtype=bool)
        if str(o.get("value", "")).strip().upper() == "DELETE":
            deleted += int(hit.sum())
            if hit.any():
                dropped.drop("extracted requirement deleted by override", out[hit].drop(columns=["_k"]), "overrides")
            out = out[~hit]
            continue
        op = canonical_operator(str(o.get("operator", "") or "eq"))
        if op is None:
            dropped.drop(f"override operator {o.get('operator')!r} not in {sorted(OPERATORS)}", pd.DataFrame([o]), "overrides")
            continue
        cname = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        value_raw, uom_raw = str(o.get("value", "")).strip(), str(o.get("uom", "") or "").strip()
        op, cvalue, cuom, status = contract_value(cname, op, value_raw, uom_raw)
        row = {
            "spec_id": spec_id, "requirement_name": cname, "operator": op, "value": cvalue, "uom": cuom,
            "requirement_class": str(o.get("requirement_class", "") or "electrical").strip().lower(),
            "confidence": str(o.get("confidence", "") or "1.00").strip(),
            "source_page": str(o.get("source_page", "") or "").strip(),
            "value_raw": value_raw, "uom_raw": uom_raw, "parse_status": status,
            "evidence": "manual override (config/spec_requirements_overrides.csv)", "chunk_id": "", "model": "",
            "origin": "override", "_k": k,
        }
        if hit.any():
            dropped.drop("extracted requirement replaced by override", out[hit].drop(columns=["_k"]), "overrides")
            out = out[~hit]
            replaced += 1
        else:
            appended += 1
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)
    logger.info("overrides applied: %d replaced, %d appended, %d deleted", replaced, appended, deleted)
    return out.drop(columns=["_k"])


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", help="only this spec_id")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="extract and chunk text, report sizes, call nothing")
    args = ap.parse_args()
    logger = setup_logging(SCRIPT)
    dropped = DroppedRows(logger, SCRIPT)
    session = CachedSession("anthropic", logger, min_interval_s=0.5, timeout_s=180)
    api_key = env("ANTHROPIC_API_KEY", required=not args.dry_run) or ""
    model = env("SPEC_EXTRACT_MODEL", "claude-sonnet-4-5") or "claude-sonnet-4-5"

    # ---- which PDFs
    idx = read_data_csv("spec_index.csv", required=False, logger=logger)
    pdfs: list[tuple[str, Path]] = []
    sha_of: dict[str, str] = {}
    if idx is not None:
        for _, r in idx.iterrows():
            if r.get("pdf_path") and r.get("spec_id"):
                p = Path(r["pdf_path"])
                p = p if p.is_absolute() else DATA_DIR.parent / p
                pdfs.append((r["spec_id"], p))
                sha_of[r["spec_id"]] = r.get("sha256", "")
    else:
        logger.warning("data/spec_index.csv not found — indexing data/specs/*.pdf directly (run S3 for provenance)")
        pdfs = [(p.stem, p) for p in sorted((DATA_DIR / "specs").glob("*.pdf"))]
    if args.spec:
        pdfs = [(s, p) for s, p in pdfs if s == args.spec]
    pdfs = [(s, p) for s, p in pdfs if p.exists()]
    log_rows(logger, "spec PDFs to extract", len(pdfs))
    if not pdfs:
        raise SystemExit("no spec PDFs available. Run S3 (and fill config/spec_sources.csv or drop PDFs in data/specs/).")

    overrides_path = CONFIG_DIR / "spec_requirements_overrides.csv"
    overrides = read_csv_str(overrides_path) if overrides_path.exists() else None

    rows: list[dict] = []
    docs: dict[str, dict] = {}   # spec_id -> specifications.csv row (one per document)
    calls = 0
    for spec_id, pdf in pdfs:
        pages = extract_pages(pdf, args.max_pages, logger)
        total_chars = sum(len(t) for _, t in pages)
        docs[spec_id] = {"spec_id": spec_id, "title": pdf_title(pdf, pages, spec_id), "requirement_count": "0",
                         "pages": str(len(pages)), "text_chars": str(total_chars), "chunks": "0",
                         "pdf_path": rel(pdf), "sha256": sha_of.get(spec_id, ""), "status": "extracted"}
        if total_chars < MIN_TEXT_CHARS:
            docs[spec_id]["status"] = "no_text"
            dropped.drop("PDF has no extractable text (scanned image?) — needs OCR or a text PDF",
                         pd.DataFrame([{"spec_id": spec_id, "pdf": str(pdf), "pages": len(pages), "chars": total_chars}]),
                         "extract")
            continue
        chunks = chunk_pages(pages)
        docs[spec_id]["chunks"] = str(len(chunks))
        logger.info("%s: %d pages, %d chars, %d chunk(s)", spec_id, len(pages), total_chars, len(chunks))
        for cid, cpages, text in chunks:
            out_text, meta = call_model(session, api_key, model, spec_id, cid, text, args.dry_run, logger)
            if out_text is None:
                logger.info("  DRY RUN %s: pages %d-%d, %d chars (would call %s)", cid, cpages[0], cpages[-1], len(text), model)
                continue
            calls += int(not meta["cached"])
            arr = parse_json_array(out_text)
            if arr is None:
                dropped.drop("LLM output is not a JSON array", pd.DataFrame([{"spec_id": spec_id, "chunk_id": cid,
                                                                             "head": out_text[:200]}]), "parse")
                continue
            ok = 0
            for item in arr:
                row, reason = validate_item(item, spec_id, cpages)
                if row is None:
                    dropped.drop(f"invalid extracted item: {reason}", pd.DataFrame([{"spec_id": spec_id, "chunk_id": cid,
                                                                                    "item": json.dumps(item)[:300]}]), "validate")
                    continue
                row.update({"chunk_id": cid, "model": model, "origin": "llm"})
                rows.append(row)
                ok += 1
            logger.info("  %s: %d requirement(s) kept of %d returned%s", cid, ok, len(arr), " [cache]" if meta["cached"] else "")

    if args.dry_run:
        logger.info("dry run complete — no API calls made, nothing written")
        return 0

    df = pd.DataFrame(rows, columns=SPEC_COLS + EXTRA_COLS)
    if len(df):
        df["_conf"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(0)
        before = len(df)
        df = df.sort_values("_conf", ascending=False).drop_duplicates(["spec_id", "requirement_name"], keep="first")
        dropped.drop("duplicate (spec_id, requirement_name) — kept highest confidence", before - len(df), "dedupe")
        df = df.drop(columns=["_conf"])
    df = apply_overrides(df, overrides, dropped, logger)
    df = df.sort_values(["spec_id", "requirement_class", "requirement_name"]).reset_index(drop=True)
    df["spec_requirement_id"] = [spec_requirement_id(s, n) for s, n in zip(df["spec_id"], df["requirement_name"])]
    if df["spec_requirement_id"].duplicated().any():
        raise SystemExit("spec_requirement_id is not unique — a requirement_name repeats within a spec after dedupe")
    write_data_csv(df, "spec_requirements.csv", SPEC_COLS, logger)

    # ---- specifications.csv: one row per document (provenance), including override-only specs
    counts = df.groupby("spec_id").size().to_dict() if len(df) else {}
    for spec_id in sorted(set(df["spec_id"]) - set(docs)):
        docs[spec_id] = {"spec_id": spec_id, "title": spec_id, "requirement_count": "0", "pages": "", "text_chars": "",
                         "chunks": "", "pdf_path": "", "sha256": "", "status": "override_only"}
    for spec_id, d in docs.items():
        d["requirement_count"] = str(int(counts.get(spec_id, 0)))
    specs_df = pd.DataFrame(list(docs.values()), columns=SPECIFICATIONS_COLS + SPECIFICATIONS_EXTRAS).sort_values("spec_id")
    write_data_csv(specs_df, "specifications.csv", SPECIFICATIONS_COLS, logger)
    if len(df):
        low = df[pd.to_numeric(df["confidence"], errors="coerce") < 0.5]
        logger.info("requirements per spec: %s", df.groupby("spec_id").size().to_dict())
        logger.info("class mix: %s; low-confidence (<0.5): %d — review these first", df["requirement_class"].value_counts().to_dict(), len(low))
        unaliased = sorted(set(df["requirement_name"]) - set(CANON_VOCAB) - {"qualification"})
        if unaliased:
            logger.warning("%d requirement name(s) outside the canonical vocabulary (add to attribute_aliases.csv "
                           "source=spec if they should score): %s", len(unaliased), ", ".join(unaliased[:15]))
    logger.info("%s (%d new API call(s))", session.summary(), calls)
    dropped.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
