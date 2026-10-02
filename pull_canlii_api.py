#!/usr/bin/env python3
"""
Ontario case-law discovery + classification metadata via the SANCTIONED CanLII API.

Why this file exists
--------------------
The previous design pulled CanLII RSS. That is forbidden by canlii.org/robots.txt
(`User-agent: * / Disallow: /`), so the feeds were disabled and Ontario coverage went
dark. The sanctioned route is the official REST API, which is METADATA ONLY.

That is enough for a real Ontario product. The per-case endpoint returns a `keywords`
field of ~1,400-2,000 characters giving, per issue: the charge, the statute and
section, the legal question, the authorities applied, and the disposition. Verified
on 12 recent Ontario criminal cases (median ~1,730 chars).

What this file does NOT do: fetch judgment text. CanLII's API carries no content,
their published limits forbid content access, and the website forbids crawlers.
See robots_guard.py.

Plan limits (CanLII, approved key): 5,000 queries/day, 2 req/s, 1 concurrent.
We run strictly sequentially with a delay, and hard-cap total calls per run.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robots_guard import USER_AGENT  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
KEY_FILE = os.path.expanduser("~/.hermes/canlii_api_key.txt")
API = "https://api.canlii.org/v1"

# Ontario courts, most authoritative first. Focus courts — this is the coverage that
# was lost when the CanLII RSS feeds were correctly disabled.
ONTARIO_DBS = ("onca", "onsc", "onscdc", "oncj")

REQUEST_DELAY = 1.5      # seconds between EVERY request; plan allows 2/s, we stay well under
MAX_RETRIES = 4
MAX_PAGES_PER_DB = 5     # 5 x 100 = 500 cases/db/run ceiling
PAGE_SIZE = 100          # docs allow up to 10,000; 100 keeps payloads small
TOTAL_CALL_BUDGET = 900  # refuse to approach the 5,000/day plan limit

_calls = 0


def load_key():
    if not os.path.exists(KEY_FILE):
        raise SystemExit(f"No CanLII API key at {KEY_FILE}")
    key = open(KEY_FILE).read().strip()
    if not key:
        raise SystemExit(f"CanLII API key file is empty: {KEY_FILE}")
    return key


def _get(url):
    """GET with 429 backoff. The key is never logged or printed."""
    global _calls
    if _calls >= TOTAL_CALL_BUDGET:
        raise SystemExit(f"Refusing to exceed the run budget of {TOTAL_CALL_BUDGET} API calls.")
    if _calls:
        time.sleep(REQUEST_DELAY)
    last = None
    for attempt in range(MAX_RETRIES):
        _calls += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                # Rate limited. Back off and retry — this is expected, not exceptional.
                time.sleep(2.0 * (attempt + 1))
                continue
            if e.code in (401, 403):
                raise SystemExit(
                    f"CanLII API rejected the key (HTTP {e.code}). "
                    f"Check {KEY_FILE} is current — keys can be rotated."
                )
            raise
        except Exception as e:  # transient network
            last = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  WARN: giving up after {MAX_RETRIES} attempts: {last}", file=sys.stderr)
    return {}


def api(path, **params):
    params["api_key"] = load_key()
    return _get(f"{API}{path}?{urllib.parse.urlencode(params)}")


def list_database(db, since):
    """All cases in a database published on/after `since`. `offset` is MANDATORY."""
    out, offset = [], 0
    for _ in range(MAX_PAGES_PER_DB):
        d = api(f"/caseBrowse/en/{db}/",
                offset=offset, resultCount=PAGE_SIZE, publishedAfter=since)
        cases = d.get("cases", []) if isinstance(d, dict) else []
        out.extend(cases)
        if len(cases) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def case_metadata(db, case_id):
    """Per-case metadata. NOTE the path is caseBrowse/{db}/{caseId} — there is no
    'caseMetadata' endpoint (an earlier note in this project asserted one; there isn't)."""
    return api(f"/caseBrowse/en/{db}/{case_id}/")


def normalise(db, listed, meta, window_start):
    """Fold list + per-case metadata into the pipeline's record schema."""
    case_id = listed.get("caseId")
    if isinstance(case_id, dict):
        case_id = case_id.get("en", "")
    keywords = (meta.get("keywords") or "").strip()
    topics = (meta.get("topics") or "").strip()
    decision_date = meta.get("decisionDate") or ""

    return {
        "court": db,
        "title": listed.get("title", ""),
        "url": listed.get("longUrl") or listed.get("url", ""),
        "short_url": meta.get("url", ""),
        "case_id": case_id,
        "date_published": decision_date or window_start,
        # caseBrowse's LIST endpoint returns no date; the per-case call usually does.
        "date_published_approx": not bool(decision_date),
        "decision_date": decision_date,
        "neutral_citation": listed.get("citation", ""),
        "docket": meta.get("docketNumber", ""),
        "keywords": keywords,
        # The existing pipeline classifies on `description`. Keywords use " | " between
        # issue blocks; normalise to newlines so extract_descriptions() splits them.
        "description": keywords.replace(" | ", "\n"),
        "subjects": [topics] if topics else [],
        "topics": topics,
        "source": "canlii_api",
        "coverage_tier": "metadata_only",
        "full_text_source": None,
    }


def looks_criminal(title):
    """Cheap pre-filter for the expensive per-case call.

    'R. v.' is high-precision for Ontario criminal matters — measured at 8/13 ONCJ,
    17/72 ONSC, 8/23 ONCA in a September 2026 window. We still fetch metadata for
    everything that passes, because the keywords field is what proves relevance.
    """
    t = (title or "").strip().lower()
    return t.startswith("r. v.") or t.startswith("r v ") or " v. her majesty" in t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 7 days ago)")
    ap.add_argument("--dbs", default=",".join(ONTARIO_DBS))
    ap.add_argument("--dry-run", action="store_true", help="fetch but write nothing")
    ap.add_argument("--criminal-only", action="store_true",
                    help="only fetch per-case metadata for R. v. matters (fewer API calls)")
    args = ap.parse_args()

    since = args.since or (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    dbs = [d.strip() for d in args.dbs.split(",") if d.strip()]
    print(f"=== CanLII API pull — published on/after {since} — courts: {', '.join(dbs)} ===")

    records, listed_total = [], 0
    for db in dbs:
        cases = list_database(db, since)
        listed_total += len(cases)
        print(f"  {db}: {len(cases)} listed")
        for c in cases:
            title = c.get("title", "")
            if args.criminal_only and not looks_criminal(title):
                continue
            cid = c.get("caseId")
            if isinstance(cid, dict):
                cid = cid.get("en", "")
            meta = case_metadata(db, cid) if cid else {}
            rec = normalise(db, c, meta, since)
            if not rec["keywords"]:
                # No keywords => cannot classify honestly. Keep it, flagged.
                rec["coverage_tier"] = "metadata_only"
                rec["keywords_missing"] = True
            records.append(rec)
        print(f"    -> {sum(1 for r in records if r['court'] == db)} detailed")

    crim = [r for r in records if looks_criminal(r["title"])]
    with_kw = [r for r in records if r.get("keywords")]
    lens = sorted(len(r["keywords"]) for r in with_kw)
    print(f"\nlist-tier records: {listed_total} | detailed: {len(records)} | criminal: {len(crim)}")
    print(f"records with keywords: {len(with_kw)}"
          + (f" | keywords chars min {lens[0]}, median {lens[len(lens)//2]}, max {lens[-1]}" if lens else ""))
    print(f"API calls used: {_calls} / {TOTAL_CALL_BUDGET} run budget")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = os.path.join(DATA_DIR, f"canlii_{today}.jsonl")
    seen = set()
    with open(out, "w") as f:
        for r in records:
            k = r.get("neutral_citation") or r.get("url")
            if k in seen:
                continue
            seen.add(k)
            f.write(json.dumps(r, default=str) + "\n")
    print(f"\nWrote {len(seen)} records -> {out}")


if __name__ == "__main__":
    main()
