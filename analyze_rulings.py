#!/usr/bin/env python3
"""
Analyze recent court rulings and identify precedent-setting decisions.
Incorporates Hansard legislative debate data for context.
Generates a summary briefing for the user.
"""
import json
import os
import sys
import glob
import re
from datetime import datetime, timezone
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
LAST_RUN_FILE = os.path.join(REPORTS_DIR, ".last_report")
os.makedirs(REPORTS_DIR, exist_ok=True)

# Courts that set binding precedent in Ontario (hierarchy)
PRECEDENT_WEIGHT = {
    "scc": "BINDING - Supreme Court of Canada",
    "onca": "BINDING - Ontario Court of Appeal",
    "fca": "PERSUASIVE - Federal Court of Appeal",
    "fct": "PERSUASIVE - Federal Court",
    "onsc": "PERSUASIVE - Ontario Superior Court",
    "onscdc": "PERSUASIVE - Ontario Divisional Court",
    "oncj": "PERSUASIVE - Ontario Court of Justice",
}

# Keywords that signal precedent-setting or significant legal analysis
PRECEDENT_KEYWORDS = [
    "overrul", "overturn", "depart from", "new test", "new approach",
    "clarify the law", "established that", "held that", "principle",
    "set out", "articulated", "framework", "standard of review",
    "landmark", "significant", "first time", "interprets",
    "s. 1", "s. 2", "s. 7", "s. 8", "s. 9", "s. 10", "s. 11", "s. 12", "s. 24",
    "charter", "constitutional", "new trial ordered", "appeal allowed",
]

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
]

os.makedirs(REPORTS_DIR, exist_ok=True)


def get_last_report_cutoff():
    """Determine cutoff date — only report on data files from after the last briefing."""
    marker = LAST_RUN_FILE
    if os.path.exists(marker):
        with open(marker) as f:
            ts = f.read().strip()
            if ts:
                return ts[:10]  # YYYY-MM-DD
    
    # Fallback: find most recent briefing file
    briefings = sorted(glob.glob(os.path.join(REPORTS_DIR, "briefing_*.txt")), reverse=True)
    if briefings:
        m = re.search(r'briefing_(\d{4}-\d{2}-\d{2})\.txt', briefings[0])
        if m:
            return m.group(1)
    
    # First run ever — last 7 days
    return (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=7)).strftime('%Y-%m-%d')


def load_recent_data(prefix, cutoff_date=None):
    """Load data files by prefix from the data directory.
    
    If cutoff_date is provided, only loads files dated on or after that date.
    """
    entries = []
    if not os.path.isdir(DATA_DIR):
        return entries
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.startswith(prefix) or not fname.endswith(".jsonl"):
            continue
        # Extract date from filename: prefix_YYYY-MM-DD.jsonl
        m = re.search(r'(\d{4}-\d{2}-\d{2})', fname)
        if cutoff_date and m and m.group(1) < cutoff_date:
            continue
        path = os.path.join(DATA_DIR, fname)
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    pass
    return entries


def classify_ruling(item):
    """Classify a ruling by subject matter and identify precedent potential."""
    title = item.get("title", "").lower()
    desc = item.get("description", "").lower()
    subjects = [s.lower() for s in item.get("subjects", [])]
    combined = title + " " + desc + " " + " ".join(subjects)

    # Determine if criminal
    is_criminal = any(kw in combined for kw in CRIME_KEYWORDS)

    # Determine subject areas
    subject_areas = set()
    if desc:
        lines = desc.split("<br/>")
        for line in lines:
            line_clean = line.strip()
            if line_clean and len(line_clean) < 200:
                if "\u2014" in line_clean:
                    subject = line_clean.split("\u2014")[0].strip()
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

    return {
        "is_criminal": is_criminal,
        "is_appellate": is_appellate,
        "subject_areas": subject_areas,
        "precedent_score": precedent_score,
        "precedent_indicators": precedent_indicators,
        "court_weight": PRECEDENT_WEIGHT.get(court, "Information"),
    }


def analyze_hansard(entries):
    """Analyze Hansard entries and extract key legislative debates."""
    transcripts = [e for e in entries if e.get("source") == "hansard_transcript"]
    api_matches = [e for e in entries if e.get("source") == "hansard_api"]

    analysis = {
        "transcript_count": len(transcripts),
        "api_match_count": len(api_matches),
        "total_crime_paras": sum(e.get("crime_para_count", 0) for e in transcripts),
        "bills_under_debate": [],
        "key_topics": Counter(),
        "relevant_dates": sorted(set(e.get("date", "") for e in entries if e.get("date")), reverse=True),
    }

    # Extract bill names from transcript snippets
    bill_patterns = [
        "Keeping Criminals Behind Bars Act",
        "Lydia's Law",
        "Safety and Accountability in Ontario Corrections Act",
        "Cash Bail",
        "Bail Reform",
    ]
    seen_bills = set()
    for e in transcripts:
        snippets = e.get("crime_snippets", [])
        for s in snippets:
            for bp in bill_patterns:
                if bp.lower() in s.lower() and bp not in seen_bills:
                    seen_bills.add(bp)
                    analysis["bills_under_debate"].append(bp)
                    break

    # Extract topics from API matches
    for e in api_matches:
        topic = e.get("topic", "")
        if topic and len(topic) > 5:
            analysis["key_topics"][topic] += 1

    return analysis


def generate_briefing(rulings):
    """Generate a summary briefing of recent rulings."""
    classified = []
    for r in rulings:
        info = classify_ruling(r)
        info["item"] = r
        classified.append(info)

    court_order = {"scc": 0, "onca": 1, "fca": 2, "fct": 3, "onsc": 4, "onscdc": 5, "oncj": 6}
    classified.sort(key=lambda x: (-x["precedent_score"], court_order.get(x["item"].get("court", ""), 99)))

    court_counts = Counter()
    for c in classified:
        court_counts[c["item"].get("court", "?")] += 1

    all_subjects = Counter()
    for c in classified:
        for s in c["subject_areas"]:
            all_subjects[s] += 1

    crime_rulings = [c for c in classified if c["is_criminal"]]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_rulings": len(rulings),
        "court_counts": dict(court_counts),
        "top_subjects": all_subjects.most_common(15),
        "criminal_rulings": len(crime_rulings),
        "highest_precedent": classified[:10],
        "all": classified,
    }


def format_briefing_text(briefing, hansard_analysis):
    """Format the briefing as readable text."""
    lines = []
    lines.append("\u2550" * 60)
    lines.append("  COURT RULINGS & LEGISLATIVE BRIEFING")
    lines.append(f"  {briefing['timestamp']}")
    lines.append("\u2550" * 60)
    lines.append("")

    # ── Rulings Summary ──
    lines.append(f"Total rulings found: {briefing['total_rulings']}")
    lines.append(f"Criminal law rulings: {briefing['criminal_rulings']}")
    lines.append("")

    lines.append("BY COURT:")
    court_labels = {
        "scc": "  Supreme Court of Canada",
        "onca": "  Ontario Court of Appeal",
        "fca": "  Federal Court of Appeal",
        "fct": "  Federal Court",
        "onsc": "  Ontario Superior Court",
        "onscdc": "  ONSC Divisional Court",
        "oncj": "  Ontario Court of Justice",
    }
    for court_key in ["scc", "onca", "fca", "fct", "onsc", "onscdc", "oncj"]:
        count = briefing["court_counts"].get(court_key, 0)
        if count:
            lines.append(f"  {court_labels.get(court_key, court_key)}: {count}")
    lines.append("")

    # Top subject areas
    if briefing["top_subjects"]:
        lines.append("TOP SUBJECT AREAS:")
        for subject, count in briefing["top_subjects"][:10]:
            lines.append(f"  \u2022 {subject}: {count}")
        lines.append("")

    # ── Legislative Debates Section ──
    if hansard_analysis and hansard_analysis["transcript_count"] > 0:
        lines.append("\u2500" * 60)
        lines.append("  ONTARIO LEGISLATIVE DEBATES (Current Session 44-1)")
        lines.append("\u2500" * 60)
        lines.append("")

        lines.append(f"Transcripts scanned: {hansard_analysis['transcript_count']}")
        lines.append(f"Crime-relevant paragraphs: {hansard_analysis['total_crime_paras']}")
        lines.append(f"Date range: {hansard_analysis['relevant_dates'][-1] if hansard_analysis['relevant_dates'] else 'N/A'} through {hansard_analysis['relevant_dates'][0] if hansard_analysis['relevant_dates'] else 'N/A'}")
        lines.append("")

        if hansard_analysis["bills_under_debate"]:
            lines.append("BILLS / LEGISLATION IN DEBATE:")
            for bill in hansard_analysis["bills_under_debate"]:
                lines.append(f"  \u25b6 {bill}")

        if hansard_analysis["key_topics"]:
            lines.append("")
            lines.append("KEY DEBATE TOPICS (from historical search):")
            for topic, count in hansard_analysis["key_topics"].most_common(10):
                lines.append(f"  \u2022 {topic}: {count} mentions")

        lines.append("")

    # ── Precedent-Setting Rulings ──
    lines.append("\u2500" * 60)
    lines.append("  PRECEDENT-SETTING RULINGS (Highest Potential)")
    lines.append("\u2500" * 60)

    shown = 0
    for c in briefing["highest_precedent"]:
        if c["precedent_score"] == 0 and shown >= 5:
            continue
        item = c["item"]
        court = item.get("court", "").upper()
        title = item.get("title", "Untitled")
        nc = item.get("neutral_citation", "")
        date = item.get("date_published", "")
        desc = item.get("description", "")
        subjects_str = ", ".join(item.get("subjects", []))

        tag = f" [{nc}]" if nc else ""
        weight = PRECEDENT_WEIGHT.get(item.get("court", ""), "")

        lines.append("")
        lines.append(f"\u25b6 {court}{tag}")
        lines.append(f"  {title}")
        lines.append(f"  {date} | {weight}")

        if subjects_str:
            lines.append(f"  Subjects: {subjects_str}")

        if desc:
            clean_desc = desc.replace("<br/>", "\n").replace("<br />", "\n")
            clean_desc = clean_desc.replace("&lt;", "<").replace("&gt;", ">")
            para_lines = [l.strip() for l in clean_desc.split("\n") if l.strip() and "\u2014" in l][:3]
            for pl in para_lines[:3]:
                lines.append(f"    {pl}")

        if c["precedent_indicators"]:
            flags = ", ".join(c["precedent_indicators"][:5])
            lines.append(f"    \u2691 Precedent signals: {flags}")

        url = item.get("url", "")
        if url:
            lines.append(f"    {url}")

        shown += 1
        if shown >= 8:
            break

    lines.append("")
    lines.append("\u2500" * 60)
    lines.append("End of briefing")

    return "\n".join(lines)


def save_briefing(briefing_text):
    """Save briefing text to reports/."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(REPORTS_DIR, f"briefing_{today}.txt")
    with open(path, "w") as f:
        f.write(briefing_text)
    return path


def main():
    cutoff_date = get_last_report_cutoff()
    print(f"Reporting cutoff: {cutoff_date} (only rulings from {cutoff_date} onward)")

    rulings = load_recent_data("rulings_", cutoff_date=cutoff_date)
    hansard = load_recent_data("hansard_", cutoff_date=cutoff_date)

    print(f"Loaded {len(rulings)} rulings, {len(hansard)} Hansard entries")
    print(f"  Hansard breakdown: {len([e for e in hansard if e.get('source') == 'hansard_transcript'])} transcripts, {len([e for e in hansard if e.get('source') == 'hansard_api'])} API matches")

    if not rulings:
        print("No new rulings since last report. Moving marker forward.")
        # Save marker so cutoff advances
        with open(LAST_RUN_FILE, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print("Briefing skipped — nothing new to report.")
        return

    briefing = generate_briefing(rulings)
    hansard_analysis = analyze_hansard(hansard) if hansard else None

    briefing_text = format_briefing_text(briefing, hansard_analysis)
    print("\n" + briefing_text)

    path = save_briefing(briefing_text)
    print(f"\nBriefing saved to: {path}")
    
    # Save marker for next run's cutoff
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()