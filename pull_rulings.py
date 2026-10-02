#!/usr/bin/env python3
"""
Pull recent Canadian and Ontario court rulings from CanLII RSS feeds.
Stores results as JSON lines in data/ for downstream analysis.
"""

import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robots_guard import require_allowed  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# --- Data-access etiquette ---------------------------------------------------
# Identify every request with a contact address. Do NOT impersonate a browser:
# it is misrepresentation, and it buys nothing — an honest identifying UA is
# accepted by every host this pipeline talks to.
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesCourtBriefing/1.0 (mailto:{MAILTO})"
REQUEST_DELAY = 3.0  # seconds between requests (polite default; was 0.5)

# Official court feeds that publish an explicit machine-readable endpoint and
# whose robots.txt permits us. robots.txt checked 2026-09-14.
FEEDS = {
    # SCC publishes its own JSON feed; robots.txt allows it. NOTE: this feed is
    # the WHOLE corpus (decisions back to the 1970s), not just new items —
    # main() filters it to RECENT_CUTOFF below.
    "scc": "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/json/rss.do",
}
RECENT_CUTOFF = "2026-01-01"  # drop SCC items older than this

# Federal Court / Federal Court of Appeal: Lexum/IcM hosts them but exposes no
# working JSON/RSS endpoint we could find (/{court}/{lang}/json/rss.do -> 404;
# /d/s/index.do -> the site's HTML homepage). Rather than scrape HTML we do not
# carry those two courts. Get them back via the CanLII API below.
#   "fca": ...   "fct": ...

# --- CanLII: DO NOT ENABLE WITHOUT READING THIS -------------------------------
# canlii.org/robots.txt ends with:
#     User-agent: *
#     Disallow: /
# i.e. a blanket "no automated clients" for anything not on its allow-list, and
# even the allow-listed search engines are told `Disallow: /*.xml` — CanLII does
# not want its RSS/XML pulled. CanLII also already serves this pipeline DataDome
# bot-blocks on full text. Scraping it with a spoofed UA is a terms violation.
#
# The sanctioned route is the official CanLII API (read-only REST, free for
# approved developers): https://github.com/canlii/API_documentation
# Request a key, then add the feeds back here — or set the env var below.
# The CanLII RSS feeds and the CANLII_SCRAPE_OK env flag that could re-enable them
# were DELETED on 2026-09-17. They were a one-keystroke path to a terms violation:
# canlii.org/robots.txt is a blanket `User-agent: * / Disallow: /`.
#
# Ontario coverage now comes from the sanctioned CanLII REST API — see pull_canlii_api.py.
# That API is metadata-only by design; it returns per-case `keywords`, `topics`,
# `decisionDate` and a citator, and CANNOT return judgment text.
#
# Do not reintroduce a feed URL here. robots_guard.py will refuse the fetch anyway,
# and test_robots_guard.py asserts that it does.


def fetch_rss(url, court_key):
    """Fetch an RSS feed and return parsed items."""
    require_allowed(url)  # terms-of-use boundary — raises rather than fetching
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {court_key}: {e}", file=sys.stderr)
        return []

    # Check if it's JSON (SCC feed)
    if raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
            items = []
            for item in data.get("items", []):
                items.append({
                    "court": court_key,
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "date_published": item.get("date_published", ""),
                    "date_modified": item.get("date_modified", ""),
                    "description": item.get("content_text", ""),
                    "neutral_citation": item.get("_neutral_citation", ""),
                    "docket_numbers": item.get("_docket_numbers", []),
                    "adjudicators": item.get("_adjudicators", []),
                    "subjects": item.get("_subjects", []),
                    "district": item.get("_district", ""),
                })
            return items
        except json.JSONDecodeError as e:
            print(f"  ERROR parsing JSON for {court_key}: {e}", file=sys.stderr)
            return []

    # XML RSS parsing
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ERROR parsing XML for {court_key}: {e}", file=sys.stderr)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for rss_item in channel.findall("item"):
        title = rss_item.findtext("title", "")
        link = rss_item.findtext("link", "")
        pub_date = rss_item.findtext("pubDate", "")
        desc = rss_item.findtext("description", "")

        items.append({
            "court": court_key,
            "title": title,
            "url": link,
            "date_published": pub_date,
            "description": desc,
        })

    return items


def is_new_ruling(item, seen_urls):
    """Check if we've already seen this ruling."""
    url = item.get("url", "")
    citation = item.get("neutral_citation", "")
    title = item.get("title", "")
    return url not in seen_urls and citation not in seen_urls and title not in seen_urls


def load_seen_rulings():
    """Load previously seen ruling URLs from existing data files."""
    seen = set()
    if not os.path.isdir(DATA_DIR):
        return seen
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.endswith(".jsonl"):
            path = os.path.join(DATA_DIR, fname)
            with open(path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("url"):
                            seen.add(entry["url"])
                        nc = entry.get("neutral_citation")
                        if nc:
                            seen.add(nc)
                    except json.JSONDecodeError:
                        pass
    return seen


def main():
    print(f"=== Court Rulings Pull — {datetime.now(timezone.utc).isoformat()} ===")
    
    seen_urls = load_seen_rulings()
    print(f"Loaded {len(seen_urls)} previously seen rulings")
    
    all_items = []
    new_items = []
    
    # Fetch every feed we are permitted to use.
    feeds = dict(FEEDS)
    print("Ontario coverage comes from the CanLII REST API (pull_canlii_api.py), "
          "not from feeds: canlii.org/robots.txt is a blanket disallow.")

    for i, (court_key, url) in enumerate(feeds.items()):
        if i:
            time.sleep(REQUEST_DELAY)  # polite spacing between hosts/requests
        print(f"Fetching {court_key}...")
        items = fetch_rss(url, court_key)
        # The SCC JSON feed is the whole corpus, not a "new items" feed — keep
        # only recent decisions so the briefing isn't flooded with 1970s cases.
        if court_key == "scc":
            before = len(items)
            items = [it for it in items
                     if isinstance(it.get("date_published"), str)
                     and it["date_published"] >= RECENT_CUTOFF]
            print(f"  Filtered {before} -> {len(items)} items (>= {RECENT_CUTOFF})")
        print(f"  Got {len(items)} items")
        all_items.extend(items)

    # Identify new ones across the merged set
    new_items = []
    for item in all_items:
        if is_new_ruling(item, seen_urls):
            new_items.append(item)
    
    print(f"\nTotal unique items: {len(all_items)}")
    print(f"New items: {len(new_items)}")
    
    # Save all items to daily file
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outfile = os.path.join(DATA_DIR, f"rulings_{today}.jsonl")
    
    # Deduplicate by URL
    seen_urls_save = set()
    with open(outfile, "w") as f:
        for item in all_items:
            url = item.get("url", "")
            if url in seen_urls_save:
                continue
            seen_urls_save.add(url)
            f.write(json.dumps(item, default=str) + "\n")
    
    print(f"Saved {len(seen_urls_save)} rulings to {outfile}")
    
    # Separate output for cron delivery
    if new_items:
        print(f"\n--- NEW RULINGS ({len(new_items)}) ---")
        for item in sorted(new_items, key=lambda x: x.get("date_published", ""), reverse=True):
            court = item.get("court", "?").upper()
            title = item.get("title", "Untitled")
            nc = item.get("neutral_citation", "")
            date = item.get("date_published", "")
            url = item.get("url", "")
            tag = f" [{nc}]" if nc else ""
            print(f"  [{court}]{tag} {title} ({date})")
            print(f"    {url}")
    else:
        print("No new rulings found since last pull.")


if __name__ == "__main__":
    main()