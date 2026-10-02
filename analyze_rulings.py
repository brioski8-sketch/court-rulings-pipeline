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
sys.path.insert(0, BASE_DIR)
# classify.py is the single source of truth for classification + Ontario-first ranking.
# Do not reintroduce a local copy of the keyword lists here — the podcast had one too
# and the two drifted apart.
from classify import (classify_ruling, rank_key, resolve_fingerprint,  # noqa: E402
                      JP_BUCKET_ORDER, JP_BUCKET_LABELS,
                      decision_date_of, window_start_iso)

# Rolling window, in days, on the DECISION date. The briefing runs weekly on Monday, so
# 7 days back is the previous Monday, inclusive. Matches the podcast's WINDOW_DAYS and the
# dashboard's date column — all three use classify.decision_date_of.
WINDOW_DAYS = 7

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
    """Inclusive start of the rolling DECISION-date window (previous Monday).

    Replaces the old `.last_report` marker, which made the window depend on when the last
    run happened to fire (and coupled the briefing to run order). Fixed at WINDOW_DAYS so
    the briefing, the podcast and the dashboard all cover the same week.
    """
    return window_start_iso(WINDOW_DAYS)


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


def merge_records(records):
    """Collapse the same case arriving from several sources into one record.

    Keeps the FIRST record seen for each fingerprint, which is why the loader feeds
    the richest tier first (federal_ -> scc_ -> canlii_ -> rulings_). A full-text
    record must never be replaced by the bare SCC feed record for the same case.
    """
    seen, out = set(), []
    for rec in records:
        fp = resolve_fingerprint(rec)
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        out.append(rec)
    return out


def _legacy_classify_ruling(item):
    """DEPRECATED — superseded by classify.classify_ruling (imported above).

    Kept briefly so this change stays reviewable; delete once the shared classifier
    has run clean through a full pipeline cycle. Do not call this.
    """
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

    # Ontario-first ordering (classify.rank_key): Ontario criminal/LE matters lead, then
    # SCC criminal/LE, then everything else, then historical backfill. Replaces the old
    # SCC-first precedent sort, which buried the local courts HPS actually works in.
    classified.sort(key=rank_key)

    # Count only decisions that are actually in-window. The SCC feed snapshot contains
    # the entire year's corpus; tallying all of it made the briefing claim 32 fresh SCC
    # rulings in a week where there was one.
    court_counts = Counter()
    for c in classified:
        if c.get("is_recent", True):
            court_counts[c["item"].get("court", "?")] += 1

    all_subjects = Counter()
    for c in classified:
        for s in c["subject_areas"]:
            all_subjects[s] += 1

    crime_rulings = [c for c in classified if c["is_criminal"]]
    tier_counts = Counter(c.get("coverage_tier", "metadata_only") for c in classified)

    # JP docket sections. Each takes Ontario matters first, then non-Ontario, so a busy
    # Ontario week cannot crowd out a full-text SCC/Federal judgment — the same failure
    # that once made the briefing retrieve 25 real judgments and display none.
    def _pick(name, n_ontario, n_other):
        b = [c for c in classified if c.get("jp_bucket") == name]
        return ([c for c in b if c.get("is_ontario")][:n_ontario]
                + [c for c in b if not c.get("is_ontario")][:n_other])

    jp_sections = {
        "charter":    _pick("charter", 4, 2),
        "bail":       _pick("bail", 3, 1),
        "provincial": _pick("provincial", 4, 1),
        "criminal":   _pick("criminal", 4, 3),
        "other":      _pick("other", 3, 2),
    }
    _shown = {id(c) for picks in jp_sections.values() for c in picks}
    # Only JP-relevant full judgments. Without the bucket filter this section filled up
    # with Federal Court immigration decisions - retrieved in full, but not a JP's docket.
    fulltext_leftover = [c for c in classified
                         if c.get("coverage_tier") == "full_text"
                         and id(c) not in _shown
                         and c.get("jp_bucket") != "other"][:5]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_rulings": len(rulings),
        "tier_counts": dict(tier_counts),
        "ontario_rulings": sum(1 for c in classified if c.get("is_ontario")),
        "court_counts": dict(court_counts),
        "top_subjects": all_subjects.most_common(15),
        "criminal_rulings": len(crime_rulings),
        # JP docket: what the brief actually leads with. Counts are in-window only.
        "jp_counts": dict(Counter(c.get("jp_bucket") for c in classified
                                  if c.get("is_recent", True))),
        "jp_sections": jp_sections,
        "fulltext_leftover": fulltext_leftover,
        "highest_precedent": classified[:10],
        # Curated the way the briefing renders: what the local courts decided, and the
        # judgments held in full. Ontario-first ordering alone buried every full-text
        # judgment beneath 33 Ontario synopses.
        "ontario_top": [c for c in classified if c.get("is_ontario")][:6],
        "fulltext_top": [c for c in classified
                         if c.get("coverage_tier") == "full_text"
                         and not c.get("is_ontario")][:5],
        "all": classified,
    }


def format_briefing_text(briefing, hansard_analysis):
    """Format the briefing as readable text."""
    lines = []
    lines.append("\u2550" * 60)
    lines.append("  COURT RULINGS & LEGISLATIVE BRIEFING \u2014 JP EDITION")
    lines.append(f"  {briefing['timestamp']}")
    lines.append("\u2550" * 60)
    lines.append("")

    # ── Rulings Summary ──
    lines.append(f"Total rulings found: {briefing['total_rulings']}")
    lines.append(f"  of which Ontario: {briefing.get('ontario_rulings', 0)}")
    lines.append("")

    # ── Coverage statement ──
    # Required by our own analytical discipline: state what could and could not be
    # seen. A synopsis is not a judgment and this briefing must never imply otherwise.
    tiers = briefing.get("tier_counts", {})
    lines.append("COVERAGE — what this briefing is based on:")
    lines.append(f"  Full judgment text retrieved: {tiers.get('full_text', 0)}")
    lines.append(f"  Court synopsis only (not the judgment): {tiers.get('metadata_only', 0)}")
    lines.append("  Ontario rulings carry CanLII's structured synopsis — charge, statute and")
    lines.append("  section, legal issue, authorities applied, disposition — but NOT the")
    lines.append("  judgment text. canlii.org and ontariocourts.ca both forbid automated")
    lines.append("  retrieval, so Ontario full text is not collected. Do not read a synopsis")
    lines.append("  as the court's reasoning.")
    lines.append("")

    # JP DOCKET AT A GLANCE - the reading order below is Charter, bail, provincial
    # offences, criminal, other. A JP presides over far more than the Criminal Code,
    # so the brief no longer leads with "criminal law rulings: N".
    jp = briefing.get("jp_counts", {})
    if jp:
        lines.append("JP DOCKET AT A GLANCE (decisions in window):")
        for _name in JP_BUCKET_ORDER:
            if jp.get(_name):
                lines.append(f"  {JP_BUCKET_LABELS[_name]}: {jp[_name]}")
        lines.append("")

    lines.append("BY COURT (decisions in window; backfilled older decisions excluded):")
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

    else:
        # Say WHY the section is absent. Silence here is indistinguishable from a failed
        # pull, and "the House was not sitting" is a different statement from "no
        # relevant debate occurred". The Ontario Legislature rises in late June and
        # does not sit over the summer, so this branch is the normal state for months.
        lines.append("─" * 60)
        lines.append("  ONTARIO LEGISLATIVE DEBATES (Current Session 44-1)")
        lines.append("─" * 60)
        lines.append("  NO SITTING-WEEK TRANSCRIPTS in this window - the Legislature is not")
        lines.append("  sitting. This is NOT a finding that no relevant debate occurred; it")
        lines.append("  means the House was not in session for the period covered.")
        _hist = (hansard_analysis or {}).get("api_match_count", 0)
        if _hist:
            lines.append(f"  Historical corpus matches: {_hist} (not current session)")
        lines.append("")

    def _render_entries(entries, limit):
        """Render up to `limit` classified entries, each labelled with its coverage tier."""
        shown = 0
        for c in entries:
            if shown >= limit:
                break
            item = c["item"]
            court = item.get("court", "").upper()
            title = item.get("title", "Untitled")
            nc = item.get("neutral_citation", "")
            date = item.get("decision_date") or item.get("date_published", "")
            tier = c.get("coverage_tier", "metadata_only")
            keywords = (item.get("keywords") or "").strip()
            desc = (item.get("description") or "").strip()
            # The SCC feed's description is "<subjects> - New document published on <date>".
            # Strip the date clause wherever it appears and keep the subject.
            if "new document published" in desc.lower():
                desc = re.sub(r"\s*[-–—]?\s*new document published on [\d\-]+\.?",
                              "", desc, flags=re.IGNORECASE).strip(" -–—")

            # Label every entry with what was actually seen. A CanLII brief is the
            # court's own structured synopsis, not the judgment, and the reader must
            # never have to guess which one they are looking at.
            tier_label = {
                "full_text": "FULL JUDGMENT TEXT",
                "metadata_only": "SYNOPSIS ONLY \u2014 judgment not read",
            }.get(tier, tier.upper())

            tag = f" [{nc}]" if nc else ""
            weight = PRECEDENT_WEIGHT.get(item.get("court", ""), "")

            lines.append("")
            lines.append(f"\u25b6 {court}{tag}  \u2014  {tier_label}")
            lines.append(f"  {title}")
            lines.append(f"  {date} | {weight}")

            if keywords:
                # Ontario: blocks separated by " | ", each block itself em-dash separated
                # ("Subject — Issue — Holding — Disposition"). Split on both, or the whole
                # 1,800-character blob prints as a single unreadable line.
                blocks = [b.strip() for b in re.split(r"\s*\|\s*|\n", keywords) if b.strip()]
                for blk in blocks[:3]:
                    if len(blk) > 320:
                        blk = blk[:320].rsplit(" ", 1)[0] + "\u2026"
                    lines.append(f"    {blk}")
                if len(blocks) > 3:
                    lines.append(f"    (+{len(blocks) - 3} further issue(s) on CanLII)")
            elif desc:
                clean_desc = desc.replace("<br/>", "\n").replace("<br />", "\n")
                clean_desc = clean_desc.replace("&lt;", "<").replace("&gt;", ">")
                for pl in [l.strip() for l in clean_desc.split("\n") if l.strip()][:3]:
                    lines.append(f"    {pl}")

            if c["precedent_indicators"]:
                flags = ", ".join(c["precedent_indicators"][:5])
                lines.append(f"    \u2691 Precedent signals: {flags}")

            url = item.get("url", "")
            if url:
                lines.append(f"    {url}")
            if item.get("full_text_path"):
                lines.append(f"    Text on disk: {item['full_text_path']}")

            shown += 1

    # ── JP sections, in the JP's reading order ──
    # Charter first, then bail, then provincial offences: a JP presides over those far
    # more often than a Criminal Code trial, and the brief used to bury all of it under
    # one "criminal law" heading. Order comes from classify.JP_BUCKET_ORDER.
    for _name in JP_BUCKET_ORDER:
        _entries = (briefing.get("jp_sections") or {}).get(_name) or []
        if not _entries:
            continue
        lines.append("")
        lines.append("─" * 60)
        lines.append(f"  {JP_BUCKET_LABELS[_name]} ({len(_entries)})")
        lines.append("─" * 60)
        _render_entries(_entries, len(_entries))

    # Any full-text judgment not shown above is still listed, so retrieving a real
    # judgment never results in displaying none of them.
    _left = briefing.get("fulltext_leftover") or []
    if _left:
        lines.append("")
        lines.append("─" * 60)
        lines.append("  FULL JUDGMENT TEXT RETRIEVED (not shown above)")
        lines.append("─" * 60)
        _render_entries(_left, 5)

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

    # Load every source tier, richest first (merge_records keeps the first seen).
    raw = []
    for prefix in ("federal_", "scc_", "canlii_", "rulings_"):
        got = load_recent_data(prefix, cutoff_date=cutoff_date)
        if got:
            print(f"  loaded {len(got):4} from {prefix}*")
        raw.extend(got)
    rulings = merge_records(raw)
    # HARD FILTER on decision date. Undated records cannot be shown to fall inside the
    # window, so they are excluded — but counted, never dropped silently.
    _before = len(rulings)
    _undated = sum(1 for r in rulings if not decision_date_of(r))
    rulings = [r for r in rulings if decision_date_of(r) >= cutoff_date]
    print(f"  merged to {_before} unique rulings; {len(rulings)} decided {cutoff_date} onward "
          f"(dropped {_undated} undated, {_before - _undated - len(rulings)} decided before cutoff)")
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