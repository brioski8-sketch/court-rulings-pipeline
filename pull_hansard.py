#!/usr/bin/env python3
"""
Pull recent Ontario Hansard transcripts and criminal-law relevant debates.
Stores results as JSON lines in data/ for downstream analysis.

Two data paths:
1. House Documents page - lists transcript dates for current session (44-1)
2. Keyword Search API - criminal-law keyword search across all sessions
"""
import json
import os
import sys
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SESSION = "44-1"  # Current Ontario Parliament session
HOUSE_DOCS_URL = "https://www.ola.org/en/legislative-business/house-documents/parliament-44/session-1"
HANSARD_API = "https://aihansardsearch-apim.azure-api.net/api/search/keyword"
TRANSCRIPT_TPL = "https://www.ola.org/en/legislative-business/house-documents/parliament-44/session-1/{date}/hansard"

# --- Data-access etiquette ---------------------------------------------------
# Identify honestly — do not impersonate a browser. (Note: the empty Path 1
# result was NOT caused by the old "Mozilla/5.0" UA; the Ontario Legislature is
# in summer recess, last sitting 2026-06-02. The UA change is about not
# misrepresenting the client, not about fixing that.)
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesCourtBriefing/1.0 (mailto:{MAILTO})"
REQUEST_DELAY = 3.0  # seconds between requests (was 0.5)
# The Hansard search API publishes its own budget in X-RateLimit-Limit/Remaining.
# Stop before exhausting it rather than getting 429s.
RATE_LIMIT_FLOOR = 2

# Criminal-law search terms for the keyword API
SEARCH_TERMS = [
    "bail reform",
    "criminal code",
    "sentencing",
    "police",
    "drug offences",
    "administration of justice",
    "bail violation",
    "court delays",
    "R. v.",
]

# Keywords for filtering paragraph relevance
CRIME_KEYWORDS = [
    "criminal", "bail", "sentencing", "firearm", "gun", "drug",
    "assault", "police", "justice", "court", "offence", "offense",
    "charter", "arrest", "warrant", "search", "seizure",
    "imprisonment", "custody", "remand", "probation", "parole",
    "victim", "witness", "evidence", "prosecut", "defence", "defense",
    "convict", "acquit", "appeal", "trial", "judge", "magistrate",
    "youth justice", "young offender", "gang", "organized crime",
    "human trafficking", "sexual assault", "domestic violence",
    "intimate partner violence", "peace bond", "surety",
    "bail system", "cash bail", "bail supervision", "administration of justice",
    "pretrial", "pre-trial", "remand", "corrections", "prison",
    "jail", "incarcerat", "detention", "detain",
]


def fetch_transcript_dates():
    """Get list of available transcript dates for current session."""
    req = Request(HOUSE_DOCS_URL, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR fetching house docs page: {e}", file=sys.stderr)
        return []

    pattern = r'/en/legislative-business/house-documents/parliament-44/session-1/(\d{4}-\d{2}-\d{2})/hansard'
    dates = sorted(set(re.findall(pattern, html)), reverse=True)
    return dates


def extract_debate_text(date):
    """Fetch a Hansard transcript and extract the debate text.

    Uses a simple approach: find the main content region and grab everything
    until footer content, strip HTML tags.
    """
    url = TRANSCRIPT_TPL.format(date=date)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR fetching transcript {date}: {e}", file=sys.stderr)
        return None, url

    # Find main content - look for region--content or the body field div
    start = raw.find("region--content")
    if start == -1:
        start = raw.find('field--name-body field--type-text-with-summary')

    if start == -1:
        return None, url

    # Find end - look for footer, related content, or similar
    end = raw.find("region--footer", start)
    if end == -1:
        end = raw.find("Related Content", start)
    if end == -1:
        end = start + 300000  # 300KB safety limit

    chunk = raw[start:end]

    # Strip scripts and styles
    clean = re.sub(r'<script[^>]*>.*?</script>', '', chunk, flags=re.DOTALL)
    clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)

    # Strip HTML tags
    clean = re.sub(r'<[^>]+>', '\n', clean)
    clean = re.sub(r'\n[ \t]+', '\n', clean)
    clean = re.sub(r'\n\s*\n', '\n', clean)
    clean = clean.strip()

    return clean, url


def is_crime_relevant(text):
    """Check if text is relevant to criminal law/policing."""
    t = text.lower()
    return any(kw in t for kw in CRIME_KEYWORDS)


_LAST_RATE_REMAINING = None


def keyword_search(term, page=1, page_size=20):
    """Search Hansard via keyword API.

    Sends an identifying UA and records the API's own remaining-request budget
    (``X-RateLimit-Remaining``) so the caller can stop before being throttled.
    The endpoint publishes `X-RateLimit-Limit: 20`; ignoring it is how you earn
    a 429.
    """
    global _LAST_RATE_REMAINING
    data = json.dumps({
        "keywords": term,
        "page": page,
        "pageSize": page_size
    }).encode()
    req = Request(
        HANSARD_API,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        resp = urlopen(req, timeout=15)
        rem = resp.headers.get("X-RateLimit-Remaining")
        if rem is not None:
            _LAST_RATE_REMAINING = int(rem)
        return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR searching '{term}' (page {page}): {e}", file=sys.stderr)
        return {"value": []}


def load_seen_ids():
    """Load previously seen Hansard entry IDs."""
    seen = set()
    if not os.path.isdir(DATA_DIR):
        return seen
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.startswith("hansard_") and fname.endswith(".jsonl"):
            path = os.path.join(DATA_DIR, fname)
            with open(path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        eid = entry.get("id", "")
                        if eid:
                            seen.add(eid)
                    except json.JSONDecodeError:
                        pass
    return seen


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    lookback = (now - timedelta(days=21)).strftime("%Y-%m-%d")  # Last 3 weeks

    print(f"=== Hansard Pull — {now.isoformat()} ===")
    seen = load_seen_ids()
    print(f"Loaded {len(seen)} already-seen entries")

    all_entries = []
    new_entries = []

    # ── Path 1: Current session transcripts ──
    print("\n── Path 1: Current session transcripts ──")
    dates = fetch_transcript_dates()
    recent_dates = [d for d in dates if d >= lookback]
    print(f"Found {len(recent_dates)} recent transcript dates (since {lookback})")

    for date in recent_dates[:8]:  # Check up to 8 most recent sitting days
        print(f"  Scanning {date}...", end="", flush=True)
        body, url = extract_debate_text(date)
        if not body:
            print(" (extraction failed)")
            continue

        # Split into paragraphs and filter crime-relevant ones
        paragraphs = [p.strip() for p in body.split('\n') if p.strip() and len(p.strip()) > 50]
        crime_paras = [(i, p) for i, p in enumerate(paragraphs) if is_crime_relevant(p)]

        print(f" {len(paragraphs)} paragraphs, {len(crime_paras)} crime-relevant")

        if crime_paras:
            # Create a single transcript entry with crime snippets
            entry_id = f"transcript_{SESSION}_{date}"
            snippets = [p[:600] for _, p in crime_paras[:8]]
            entry = {
                "source": "hansard_transcript",
                "id": entry_id,
                "date": date,
                "session": SESSION,
                "crime_para_count": len(crime_paras),
                "total_para_count": len(paragraphs),
                "crime_snippets": snippets,
                "url": url,
            }
            all_entries.append(entry)
            if entry_id not in seen:
                new_entries.append(entry)
        time.sleep(REQUEST_DELAY)  # polite spacing on ola.org

    # ── Path 2: Keyword search API ──
    print("\n── Path 2: Keyword search API ──")
    for term in SEARCH_TERMS:
        if _LAST_RATE_REMAINING is not None and _LAST_RATE_REMAINING < RATE_LIMIT_FLOOR:
            print(f"  Stopping: API budget nearly spent "
                  f"(X-RateLimit-Remaining={_LAST_RATE_REMAINING}). Resuming next run.")
            break
        print(f"  Searching: '{term}'...", end="", flush=True)
        results = keyword_search(term, page=1, page_size=20)
        items = results.get("value", [])
        print(f" {len(items)} results")

        for item in items:
            para = item.get("paragraph", "")
            if not para or not is_crime_relevant(para):
                continue

            item_id = item.get("id", f"api_{term}_{item.get('date','')}_{item.get('speaker','')[:20]}")
            date_str = (item.get("date", "") or "")[:10]

            entry = {
                "source": "hansard_api",
                "id": item_id,
                "date": date_str,
                "session": item.get("session", ""),
                "speaker": item.get("speaker", ""),
                "topic": item.get("topic", ""),
                "type_of_business": item.get("typeOfBusiness", ""),
                "paragraph": para[:1500],
                "url": item.get("url", ""),
                "search_term": term,
            }
            all_entries.append(entry)
            if item_id not in seen:
                new_entries.append(entry)

        time.sleep(REQUEST_DELAY)  # polite spacing on the Hansard API

    # ── Save ──
    outfile = os.path.join(DATA_DIR, f"hansard_{today}.jsonl")
    saved_ids = set()
    with open(outfile, "w") as f:
        for entry in all_entries:
            eid = entry.get("id", "")
            if eid in saved_ids:
                continue
            saved_ids.add(eid)
            f.write(json.dumps(entry, default=str) + "\n")
    print(f"\nSaved {len(saved_ids)} entries to {outfile}")

    # ── Summary for cron delivery ──
    if new_entries:
        transcript_new = [e for e in new_entries if e["source"] == "hansard_transcript"]
        api_new = [e for e in new_entries if e["source"] == "hansard_api"]

        print(f"\n── NEW ENTRIES ({len(new_entries)}) ──")

        if transcript_new:
            print(f"\n  Transcripts with crime-relevant content:")
            for e in sorted(transcript_new, key=lambda x: x["date"], reverse=True):
                print(f"    {e['date']} - {e['crime_para_count']} relevant paragraphs")
                print(f"      {e['url']}")
                if e.get("crime_snippets"):
                    first_snip = e["crime_snippets"][0]
                    topic_line = first_snip[:200] if len(first_snip) > 200 else first_snip
                    print(f"      Topic: {topic_line}")

        if api_new:
            speakers = set(e.get("speaker", "") for e in api_new if e.get("speaker"))
            topics = set(e.get("topic", "") for e in api_new if e.get("topic"))
            print(f"\n  Keyword matches by topic:")
            for t in sorted(topics)[:8]:
                count = sum(1 for e in api_new if e.get("topic") == t)
                print(f"    [{t}] — {count} mentions")

    else:
        print("\nNo new Hansard entries found.")


if __name__ == "__main__":
    main()