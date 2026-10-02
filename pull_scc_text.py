#!/usr/bin/env python3
"""
Supreme Court of Canada — attach FULL TEXT to SCC rulings.

Discovery stays with pull_rulings.py (the official SCC JSON feed). This step takes
those rulings and fetches the judgment document for each:

    https://decisions.scc-csc.ca/scc-csc/scc-csc/en/{item_id}/1/document.do   -> PDF

robots.txt for decisions.scc-csc.ca disallows only four legacy ICM URLs, so this is
permitted. Verified 2026-09-17 (item 21346 returned a 161 KB PDF).

Output: data/scc_YYYY-MM-DD.jsonl — the ruling record plus `full_text_path`,
`text_chars` and `coverage_tier: full_text`. The loader merges these over the plain
`rulings_*` records so classification sees the text, not just the title.

PDF extraction is shared with pull_federal.py — do not duplicate it.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pull_federal import extract_pdf_text, text_path_for, REQUEST_DELAY  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEXT_DIR = os.path.join(BASE_DIR, "text")

SCC_HOST = "https://decisions.scc-csc.ca"
ITEM_RE = re.compile(r"/item/(\d+)/")


def load_scc_rulings(since, limit):
    """SCC records from data/rulings_*.jsonl within the window, newest first."""
    out = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "rulings_*.jsonl")), reverse=True):
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("court") or "").lower() != "scc":
                    continue
                if (r.get("date_published") or "") < since:
                    continue
                m = ITEM_RE.search(r.get("url") or "")
                if not m:
                    continue
                r["_item_id"] = m.group(1)
                out.append(r)
        if len(out) >= limit:
            break
    seen, uniq = set(), []
    for r in out:
        k = r.get("neutral_citation") or r["_item_id"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: r.get("date_published", ""), reverse=True)
    return uniq[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 7 days ago)")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = args.since or (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    os.makedirs(TEXT_DIR, exist_ok=True)
    print(f"=== SCC full-text pull — decisions on/after {since} ===")

    rulings = load_scc_rulings(since, args.limit)
    print(f"  SCC rulings in window: {len(rulings)}")

    enriched, fetched = [], 0
    for r in rulings:
        citation = r.get("neutral_citation") or f"SCC item {r['_item_id']}"
        tp = text_path_for(citation)
        if os.path.exists(tp) and os.path.getsize(tp) > 0:
            text = open(tp, encoding="utf-8", errors="replace").read()
        else:
            time.sleep(REQUEST_DELAY)
            url = f"{SCC_HOST}/scc-csc/scc-csc/en/{r['_item_id']}/1/document.do"
            text = extract_pdf_text(url)
            if text:
                with open(tp, "w", encoding="utf-8") as f:
                    f.write(text)
                fetched += 1
        rec = {k: v for k, v in r.items() if not k.startswith("_")}
        rec.update({
            "source": "scc_document",
            "coverage_tier": "full_text" if text else "metadata_only",
            "full_text_path": tp if text else None,
            "text_chars": len(text),
            "source_url": f"{SCC_HOST}/scc-csc/scc-csc/en/{r['_item_id']}/1/document.do",
        })
        enriched.append(rec)
        flag = "text" if text else "NO TEXT"
        print(f"    [{flag:7}] {citation:22} {len(text):>7} chars  {r.get('title','')[:48]}")

    got = [r for r in enriched if r["text_chars"]]
    print(f"\nfull texts fetched this run: {fetched} | records with text: {len(got)}/{len(enriched)}")
    if got:
        sizes = sorted(r["text_chars"] for r in got)
        print(f"text size chars: min {sizes[0]}, median {sizes[len(sizes)//2]}, max {sizes[-1]}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = os.path.join(DATA_DIR, f"scc_{today}.jsonl")
    with open(out, "w") as f:
        for r in enriched:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"\nWrote {len(enriched)} records -> {out}")


if __name__ == "__main__":
    main()
