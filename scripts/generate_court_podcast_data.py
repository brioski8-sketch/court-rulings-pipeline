#!/usr/bin/env python3
"""
Extract top 5 most relevant new court rulings for podcast generation.
Outputs structured JSON for the agent to use in podcast script writing.

Uses the same .last_report and CRIME_KEYWORDS classification as
analyze_rulings.py so it reports on the same set of rulings.

Focuses on criminal-law and Charter rulings most relevant to
a crime analyst and law enforcement audience.
"""

import json
import os
import re
import glob
from datetime import datetime, timezone
from collections import Counter, OrderedDict

BASE_DIR = os.path.expanduser("~/.hermes/court-rulings")
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
LAST_RUN_FILE = os.path.join(REPORTS_DIR, ".last_report")
PODCAST_DIR = os.path.join(BASE_DIR, "podcast")
OUTPUT_FILE = os.path.join(PODCAST_DIR, "podcast_rulings.json")

# Courts that set binding precedent in Ontario (hierarchy)
PRECEDENT_WEIGHT = OrderedDict([
    ("scc", "BINDING - Supreme Court of Canada"),
    ("onca", "BINDING - Ontario Court of Appeal"),
    ("fca", "PERSUASIVE - Federal Court of Appeal"),
    ("fct", "PERSUASIVE - Federal Court"),
    ("onsc", "PERSUASIVE - Ontario Superior Court"),
    ("onscdc", "PERSUASIVE - Ontario Divisional Court"),
    ("oncj", "PERSUASIVE - Ontario Court of Justice"),
])

COURT_ORDER = {"scc": 0, "onca": 1, "fca": 2, "fct": 3, "onsc": 4, "onscdc": 5, "oncj": 6}

COURT_LABELS = {
    "scc": "Supreme Court of Canada",
    "onca": "Ontario Court of Appeal",
    "fca": "Federal Court of Appeal",
    "fct": "Federal Court",
    "onsc": "Ontario Superior Court",
    "onscdc": "ONSC Divisional Court",
    "oncj": "Ontario Court of Justice",
}

# Criminal-law specific keywords to highlight for a crime analyst
CRIME_KEYWORDS = [
    "criminal", "sentencing", "evidence", "search", "seizure",
    "charter", "murder", "assault", "robbery", "drug", "weapon",
    "firearm", "impaired", "driving", "dangerous", "offender",
    "bail", "remand", "custody", "probation", "conditional sentence",
    "reasonable doubt", "identification", "confession", "statement",
    "right to counsel", "detention", "arrest", "warrant", "wiretap",
    "DNA", "forensic", "expert evidence", "accomplice", "kienapple",
    "ywca", "young offender", "youth", "gang", "organized crime",
    "human trafficking", "sexual assault", "domestic violence",
    "intimate partner", "peace bond", "surety", "sureties",
    "police", "officer", "disclosure",
]

# Keywords that signal precedent-setting or significant legal analysis
PRECEDENT_KEYWORDS = [
    "overrul", "overturn", "depart from", "new test", "new approach",
    "clarify the law", "established that", "held that", "principle",
    "set out", "articulated", "framework", "standard of review",
    "landmark", "significant", "first time", "interprets",
    "s. 1", "s. 2", "s. 7", "s. 8", "s. 9", "s. 10", "s. 11", "s. 12", "s. 24",
    "charter", "constitutional", "new trial ordered", "appeal allowed",
]

# Law enforcement interest keywords — rulings that affect how police work
LE_KEYWORDS = [
    "disclosure", "search and seizure", "warrantless", "exclusion of evidence",
    "s. 24(2)", "breach", "statement to police", "custodial interrogation",
    "videotaped statement", "identification procedure", "lineup",
    "reasonable suspicion", "reasonable grounds", "articulable cause",
    "traffic stop", "check stop", "roadside screening", "ASD",
    "approved instrument", "breath demand", "blood sample",
    "search incident to arrest", "strip search", "body cavity",
    "digital device", "cell phone", "computer search",
    "sniff", "sniffer dog", "drug recognition",
    "bail hearing", "show cause", "reverse onus",
    "s. 524", "bail revocation", "surety",
    "peace bond", "s. 810", "firearm prohibition",
    "weapons prohibition", "mandatory minimum",
    "victim surcharge", "restitution",
]


def get_report_cutoff():
    """Same cutoff logic as analyze_rulings.py — rulings since last pipeline run."""
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE) as f:
            ts = f.read().strip()
            if ts:
                return ts[:10]  # YYYY-MM-DD
    # Fallback: most recent briefing
    briefings = sorted(glob.glob(os.path.join(REPORTS_DIR, "briefing_*.txt")), reverse=True)
    if briefings:
        m = re.search(r'briefing_(\d{4}-\d{2}-\d{2})\.txt', briefings[0])
        if m:
            return m.group(1)
    return (datetime.now(timezone.utc)).strftime('%Y-%m-%d')


def extract_descriptions(desc):
    """Extract individual subject-description pairs from the description field."""
    parts = []
    if not desc:
        return parts
    lines = desc.replace("<br/>", "\n").replace("<br />", "\n").split("\n")
    for line in lines:
        line = line.strip()
        if line and "—" in line:
            parts.append(line)
    return parts


def classify_ruling(item):
    """Classify a ruling — mirrors logic from analyze_rulings.py."""
    title = item.get("title", "").lower()
    desc = item.get("description", "").lower()
    subjects = [s.lower() for s in item.get("subjects", [])]
    combined = title + " " + desc + " " + " ".join(subjects)

    # Determine if criminal
    is_criminal = any(kw in combined for kw in CRIME_KEYWORDS)

    # Determine law enforcement relevance
    is_LE_relevant = any(kw in combined for kw in LE_KEYWORDS)

    # Determine subject areas
    subject_areas = set()
    if desc:
        for line in desc.replace("<br/>", "\n").replace("<br />", "\n").split("\n"):
            line_clean = line.strip()
            if line_clean and len(line_clean) < 200 and "—" in line_clean:
                subject = line_clean.split("—")[0].strip()
                if len(subject) < 100:
                    subject_areas.add(subject)
    if subjects:
        subject_areas.update(subjects)

    # Check for precedent-setting language
    precedent_score = 0
    precedent_indicators = []
    for kw in PRECEDENT_KEYWORDS:
        if kw in combined:
            precedent_score += 1
            precedent_indicators.append(kw)

    court = item.get("court", "")
    is_appellate = court in ("scc", "onca", "fca")

    # Extract descriptions in readable form
    descriptions = extract_descriptions(item.get("description", ""))

    return {
        "is_criminal": is_criminal,
        "is_LE_relevant": is_LE_relevant,
        "is_appellate": is_appellate,
        "subject_areas": list(subject_areas)[:5],
        "precedent_score": precedent_score,
        "precedent_indicators": precedent_indicators[:5],
        "court_weight": PRECEDENT_WEIGHT.get(court, "Information"),
        "court_label": COURT_LABELS.get(court, court.upper()),
        "descriptions": descriptions,
        "combined_relevance": (
            (precedent_score * 2) +
            (3 if is_criminal else 0) +
            (3 if is_LE_relevant else 0) +
            max(0, 5 - COURT_ORDER.get(court, 99))
        ),
    }


def extract_rulings():
    os.makedirs(PODCAST_DIR, exist_ok=True)

    cutoff_date = get_report_cutoff()
    print(f"Cutoff: {cutoff_date}")

    # Load rulings since cutoff
    rulings = []
    if not os.path.isdir(DATA_DIR):
        print(f"Data directory not found: {DATA_DIR}")
        return

    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.startswith("rulings_") or not fname.endswith(".jsonl"):
            continue
        m = re.search(r'(\d{4}-\d{2}-\d{2})', fname)
        if cutoff_date and m and m.group(1) < cutoff_date:
            continue
        path = os.path.join(DATA_DIR, fname)
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    rulings.append(entry)
                except json.JSONDecodeError:
                    pass

    print(f"Loaded {len(rulings)} rulings since cutoff")

    if not rulings:
        print("No new rulings. Writing empty output.")
        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cutoff": cutoff_date,
            "total_rulings": 0,
            "rulings": [],
        }
        with open(OUTPUT_FILE, "w") as f:
            json.dump(output, f, indent=2)
        print(f"No rulings. Output: {OUTPUT_FILE}")
        return

    # Classify and score all rulings
    classified = []
    for r in rulings:
        info = classify_ruling(r)
        info["item"] = r
        classified.append(info)

    # Sort: combined_relevance desc, precedent_score desc, court hierarchy asc
    classified.sort(key=lambda x: (-x["combined_relevance"], -x["precedent_score"], COURT_ORDER.get(x["item"].get("court", ""), 99)))

    # Stats
    crime_count = sum(1 for c in classified if c["is_criminal"])
    LE_count = sum(1 for c in classified if c["is_LE_relevant"])
    court_counts = Counter(c["item"].get("court", "?") for c in classified)

    # Take top 5 for podcast
    top5 = classified[:5]

    # Build clean output
    papers = []
    for c in top5:
        item = c["item"]
        papers.append({
            "title": item.get("title", "Untitled"),
            "url": item.get("url", ""),
            "date_published": item.get("date_published", ""),
            "court_label": c["court_label"],
            "court_weight": c["court_weight"],
            "is_criminal": c["is_criminal"],
            "is_law_enforcement_relevant": c["is_LE_relevant"],
            "subject_areas": c["subject_areas"],
            "precedent_score": c["precedent_score"],
            "precedent_indicators": c["precedent_indicators"],
            "descriptions": c["descriptions"],
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": cutoff_date,
        "total_rulings": len(rulings),
        "criminal_rulings": crime_count,
        "law_enforcement_relevant": LE_count,
        "court_breakdown": {COURT_LABELS.get(k, k.upper()): v for k, v in court_counts.items()},
        "rulings": papers,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Extracted {len(papers)} rulings for podcast.")
    print(f"Criminal-law rulings: {crime_count}/{len(rulings)}")
    print(f"LE-relevant rulings: {LE_count}/{len(rulings)}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Output JSON:\n{json.dumps(output, indent=2)}")


if __name__ == "__main__":
    extract_rulings()
