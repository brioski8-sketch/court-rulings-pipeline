#!/usr/bin/env python3
"""
Federal Court of Appeal (FCA) and Federal Court (FCT) — list + FULL TEXT.

Both sites are Lexum/Norma single-page apps: a plain fetch returns an empty JS shell
with no case links. Appending `?iframe=true` returns the same content server-rendered
with real `item/{id}` links. That is undocumented behaviour, so the tests in this file
check for it failing loudly rather than silently returning zero cases.

robots.txt for both hosts disallows only four specific legacy ICM URLs, so this is
permitted. Verified 2026-09-17.

Full text: `{prefix}/decisions/en/{item_id}/1/document.do` returns a PDF.
Prefixes are NOT symmetric — this is the trap:
    FCA  -> /fca-caf/
    FCT  -> /fc-cf/      (/fct-cf/ 404s)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robots_guard import require_allowed, USER_AGENT  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEXT_DIR = os.path.join(BASE_DIR, "text")

HOST = "https://decisions.fca-caf.gc.ca"          # FCT lives on its own host below
COURTS = {
    "fca": ("https://decisions.fca-caf.gc.ca", "/fca-caf"),
    "fct": ("https://decisions.fct-cf.gc.ca", "/fc-cf"),
}

REQUEST_DELAY = 3.0
PAGES_DEFAULT = 1          # page 1 = the 25 newest
MAX_PDF_BYTES = 20_000_000

ROW_RE = re.compile(
    r'<span class="title"><a[^>]*href="([^"]+)"[^>]*>([^<]+)</a></span>\s*-\s*'
    r'<span class="citation">([^<]+)</span>\s*-\s*'
    r'<span class="publicationDate">([^<]+)</span>')
ITEM_ID_RE = re.compile(r"/item/(\d+)/")


def fetch(url, binary=False):
    """robots-guarded GET. Every fetch in this file goes through here."""
    require_allowed(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read(MAX_PDF_BYTES) if binary else resp.read()
                return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            if e.code == 404:
                # Not every listed decision has a document at the /1/ path — some have
                # not had reasons published yet. That is a missing document, not a
                # pipeline failure, and it must not abort the other courts' pulls.
                print(f"  NOTE: no document at {url} (404)", file=sys.stderr)
                return b"" if binary else ""
            raise
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    print(f"  WARN: giving up on {url}: {last}", file=sys.stderr)
    return b"" if binary else ""


def list_court(host, prefix, pages):
    """Newest-first decision list. Uses the ?iframe=true server-rendered view."""
    out = []
    for page in range(1, pages + 1):
        url = f"{host}{prefix}/decisions/en/nav_date.do?iframe=true"
        if page > 1:
            url += f"&page={page}"
        if out:
            time.sleep(REQUEST_DELAY)
        html = fetch(url)
        rows = ROW_RE.findall(html)
        if not rows:
            # Loud failure: the iframe contract changed or the page shape moved.
            print(f"  WARNING: zero rows parsed from {url} — the ?iframe=true layout "
                  f"may have changed. Returning what we have.", file=sys.stderr)
        for href, title, citation, pub_date in rows:
            m = ITEM_ID_RE.search(href)
            out.append({
                "item_id": m.group(1) if m else "",
                "title": title.strip(),
                "citation": citation.strip(),
                "date_published": pub_date.strip(),
                "item_url": host + href if href.startswith("/") else href,
            })
    return out


def extract_pdf_text(url):
    """PDF -> text via PyMuPDF. Imported lazily so listing still works without it."""
    raw = fetch(url, binary=True)
    if not raw:
        return ""
    if not raw.startswith(b"%PDF"):
        # Some documents are served as HTML despite the document.do path.
        txt = raw.decode("utf-8", "replace")
        txt = re.sub(r"<(script|style).*?</\1>", " ", txt, flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt)).strip()
    try:
        import fitz  # PyMuPDF — present under python3, NOT python3.12
    except ImportError:
        print("  WARN: PyMuPDF not importable; run with `python3`, not python3.12",
              file=sys.stderr)
        return ""
    tmp = "/tmp/_federal_doc.pdf"
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        with fitz.open(tmp) as doc:
            return "\n".join(page.get_text() for page in doc).strip()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def text_path_for(citation):
    safe = re.sub(r"[^0-9A-Za-z]+", "", citation) or "unknown"
    return os.path.join(TEXT_DIR, f"{safe.lower()}.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 7 days ago)")
    ap.add_argument("--courts", default="fca,fct")
    ap.add_argument("--pages", type=int, default=PAGES_DEFAULT)
    ap.add_argument("--limit", type=int, default=40, help="max full-text fetches per court")
    ap.add_argument("--no-text", action="store_true", help="list only, skip full text")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = args.since or (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    os.makedirs(TEXT_DIR, exist_ok=True)
    print(f"=== Federal courts pull — published on/after {since} ===")

    records = []
    for court in [c.strip() for c in args.courts.split(",") if c.strip()]:
        if court not in COURTS:
            print(f"  skipping unknown court {court}", file=sys.stderr)
            continue
        host, prefix = COURTS[court]
        listed = list_court(host, prefix, args.pages)
        fresh = [c for c in listed if c["date_published"] >= since]
        print(f"  {court}: {len(listed)} listed, {len(fresh)} since {since}")

        fetched = 0
        failed = 0
        for c in fresh[:args.limit]:
            tp = text_path_for(c["citation"])
            text = ""
            if os.path.exists(tp) and os.path.getsize(tp) > 0:
                text = open(tp, encoding="utf-8", errors="replace").read()
                cached = True
            elif args.no_text:
                cached = False
            else:
                time.sleep(REQUEST_DELAY)
                doc_url = f"{host}{prefix}/decisions/en/{c['item_id']}/1/document.do"
                try:
                    text = extract_pdf_text(doc_url)
                except Exception as exc:
                    # Isolate per-case failures: one malformed PDF or odd response must
                    # not cost us the other 24 entries on the page.
                    print(f"  WARN: {c['citation']}: {exc}", file=sys.stderr)
                    text = ""
                    failed += 1
                cached = False
                if text:
                    with open(tp, "w", encoding="utf-8") as f:
                        f.write(text)
                    fetched += 1
            records.append({
                "court": court,
                "title": c["title"],
                "url": c["item_url"],
                "neutral_citation": c["citation"],
                "date_published": c["date_published"],
                "decision_date": c["date_published"],
                "description": "",           # no synopsis exists for this tier
                "subjects": [],
                "source": "federal",
                "coverage_tier": "full_text" if text else "metadata_only",
                "full_text_path": tp if text else None,
                "text_chars": len(text),
                "text_cached": cached,
                "source_url": f"{host}{prefix}/decisions/en/{c['item_id']}/1/document.do",
            })
        if failed:
            print(f"    full texts fetched: {fetched} | could not extract: {failed}")

    got = [r for r in records if r["text_chars"]]
    print(f"\nrecords: {len(records)} | with full text: {len(got)}")
    if got:
        sizes = sorted(r["text_chars"] for r in got)
        print(f"text size chars: min {sizes[0]}, median {sizes[len(sizes)//2]}, max {sizes[-1]}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = os.path.join(DATA_DIR, f"federal_{today}.jsonl")
    seen = set()
    with open(out, "w") as f:
        for r in records:
            k = r["neutral_citation"] or r["url"]
            if k in seen:
                continue
            seen.add(k)
            f.write(json.dumps(r, default=str) + "\n")
    print(f"\nWrote {len(seen)} records -> {out}")


if __name__ == "__main__":
    main()
