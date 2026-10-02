#!/usr/bin/env python3
"""
Shared classification + ranking for court rulings.

Extracted so analyze_rulings.py (briefing) and generate_court_podcast_data.py
(podcast) cannot drift apart — previously each carried its own copy of the keyword
lists and they had to be kept in sync by hand.

Two jobs:
  1. classify_ruling() — how relevant is this case to a crime analyst / LE audience.
  2. rank_key() / select_top() — Ontario FIRST, without letting that become Ontario ONLY.
"""

import os
import re
from collections import OrderedDict

# --- Court hierarchy ---------------------------------------------------------

COURT_ORDER = {"onca": 0, "scc": 1, "onsc": 2, "onscdc": 3, "fca": 4, "fct": 5, "oncj": 6}

COURT_LABELS = {
    "scc": "Supreme Court of Canada",
    "onca": "Ontario Court of Appeal",
    "onsc": "Ontario Superior Court",
    "onscdc": "ONSC Divisional Court",
    "oncj": "Ontario Court of Justice",
    "fca": "Federal Court of Appeal",
    "fct": "Federal Court",
}

PRECEDENT_WEIGHT = OrderedDict([
    ("onca", "BINDING - Ontario Court of Appeal"),
    ("scc", "BINDING - Supreme Court of Canada"),
    ("onsc", "PERSUASIVE - Ontario Superior Court"),
    ("onscdc", "PERSUASIVE - Ontario Divisional Court"),
    ("oncj", "PERSUASIVE - Ontario Court of Justice"),
    ("fca", "PERSUASIVE - Federal Court of Appeal"),
    ("fct", "PERSUASIVE - Federal Court"),
])

# Ontario courts are the focus: this is the jurisdiction HPS actually works in.
ONTARIO_COURTS = frozenset({"onca", "onsc", "onscdc", "oncj"})
APPELLATE_COURTS = frozenset({"scc", "onca", "fca"})

# --- Signals -----------------------------------------------------------------

CRIME_KEYWORDS = [
    "criminal", "sentencing", "evidence", "search", "seizure", "charter", "murder",
    "assault", "robbery", "drug", "weapon", "firearm", "impaired", "driving",
    "dangerous", "offender", "bail", "remand", "custody", "probation",
    "conditional sentence", "reasonable doubt", "identification", "confession",
    "statement", "right to counsel", "detention", "arrest", "warrant", "wiretap",
    "DNA", "forensic", "expert evidence", "accomplice", "kienapple", "ywca",
    "young offender", "youth", "gang", "organized crime", "human trafficking",
    "sexual assault", "domestic violence", "intimate partner", "peace bond",
    "surety", "sureties", "police", "officer", "disclosure", "verdict",
    "controlled drugs and substances act", "criminal code", "guilty", "acquittal",
    "proceeds of crime", "forfeiture", "conspiracy", "trafficking",
]

PRECEDENT_KEYWORDS = [
    "overrul", "overturn", "depart from", "new test", "new approach",
    "clarify the law", "established that", "held that", "principle", "set out",
    "articulated", "framework", "standard of review", "landmark", "significant",
    "first time", "interprets", "charter", "constitutional", "new trial ordered",
    "appeal allowed", "appeal dismissed",
]

# Charter section references are matched by THIS regex, never as bare substrings.
# Why: "s. 1" is a substring of "s. 12"/"s. 10"/"s. 11", and every statute has section
# numbers, so a naked "s. 12" hit fired on a Family Law Act preservation order and scored
# a CIVIL case (Johnson v. Bobanovic) as precedent-setting — it became the week's highest
# "importance" ruling. Measured 2026-09-20: 610 of 1,622 archived rulings carried a bare
# section-ref hit, 147 of them non-criminal matters (family, immigration, commercial,
# condo, insurance) being inflated.
#
# The gate is "the document mentions the Charter at all" + "a section ref is present".
# NOT proximity: a first attempt required "Charter" within 60 characters of the ref and
# that penalised REAL criminal judgments (mean -2.45 vs -0.97 for other cases), dropping
# the criminal share of the top 10 from 7/10 to 4/10 — criminal reasons say "the s. 8
# breach" long after establishing Charter context once. Document-level gating is neutral
# on that ranking while still rejecting the Family Law Act / CLRA / Arbitration Act hits.
# `(?![0-9])` stops "s. 1" matching inside "s. 12"/"s. 10"/"s. 11".
CHARTER_SECTION_RE = re.compile(
    r"(?i)\bs{1,2}\.\s*(?:1|2|7|8|9|10|11|12|24)(?![0-9])")

LE_KEYWORDS = [
    "disclosure", "search and seizure", "warrantless", "exclusion of evidence",
    "s. 24(2)", "breach", "statement to police", "custodial interrogation",
    "videotaped statement", "identification procedure", "lineup",
    "reasonable suspicion", "reasonable grounds", "articulable cause",
    "traffic stop", "check stop", "roadside screening", "ASD",
    "approved instrument", "breath demand", "blood sample",
    "search incident to arrest", "strip search", "body cavity", "digital device",
    "cell phone", "computer search", "sniff", "sniffer dog", "drug recognition",
    "bail hearing", "show cause", "reverse onus", "s. 524", "bail revocation",
    "surety", "peace bond", "s. 810", "firearm prohibition",
    "weapons prohibition", "mandatory minimum", "victim surcharge", "restitution",
]

CITATION_RE = re.compile(
    r"20\d\d\s+(?:SCC|ONCA|ONSC|ONSCDC|ONCJ|FCA|FCT)\s+\d+", re.IGNORECASE)

# --- JP docket axes ----------------------------------------------------------
# A Justice of the Peace presides over bail, provincial-offence trials and Charter
# applications - not only the Criminal Code. A title cannot tell these apart: Ontario
# POA prosecutions are prosecuted by the Crown and styled "R. v." too, so "R. v." is
# NOT a criminal-law signal on its own.
# Every pattern here uses WORD BOUNDARIES: "bail" must not match "bailiff", "s. 8" must
# not match "s. 80" - the same substring trap already documented for the section refs.
CHARTER_RE = re.compile(
    r"(?i)\bcharter\b|\bunreasonable search\b"
    r"|\barbitrary detention\b|\bright to counsel\b|\bexclusion of evidence\b"
    r"|\bcruel and unusual\b|\bpresumption of innocence\b|\bself-incrimination\b"
    r"|\bs\.\s*24\s*\(\s*2\s*\)")
BAIL_RE = re.compile(
    r"(?i)\bbail\b|\bjudicial interim release\b|\bshow cause\b|\bsurety\b|\bsureties\b"
    r"|\bs\.\s*515\b|\bs\.\s*524\b|\brelease order\b|\bdetention order\b"
    r"|\breverse onus\b|\bbail review\b")
PROVINCIAL_OFFENCE_RE = re.compile(
    r"(?i)\bprovincial offences act\b|\bhighway traffic act\b|\bliquor licence act\b"
    r"|\bcompulsory automobile insurance\b|\bfish and wildlife conservation act\b"
    r"|\btrespass to property act\b|\bsafe streets act\b|\bsmoke-free ontario\b"
    r"|\bdog owners. liability\b|\bliquor control act\b|\bmotorized snow vehicles act\b"
    r"|\boff-road vehicles act\b|\bbuilding code act\b|\bfire protection and prevention act\b"
    r"|\benvironmental protection act\b|\bpesticides act\b|\bontario heritage act\b")

# Section order in the briefing = JP priority. First match wins.
JP_BUCKET_ORDER = ("charter", "bail", "provincial", "criminal", "other")
JP_BUCKET_LABELS = {
    "charter": "CHARTER APPLICATIONS",
    "bail": "BAIL / JUDICIAL INTERIM RELEASE",
    "provincial": "PROVINCIAL OFFENCES (POA / regulatory)",
    "criminal": "CRIMINAL CODE (no Charter or bail issue flagged)",
    "other": "OTHER RULINGS",
}

# How old a *decision* may be and still count as "new this week". CanLII backfills
# constantly, so posting date alone is not enough.
RECENT_DECISION_DAYS = 45


def _within_days(date_str, days):
    """True if YYYY-MM-DD is within `days` of today. Unparseable => True (never drop
    silently on a parsing failure)."""
    if not date_str:
        return True
    from datetime import datetime
    s = str(date_str).strip()
    d = None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
            break
        except Exception:
            continue
    if d is None:
        try:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(s).date()
        except Exception:
            return True   # genuinely unparseable: never drop silently
    from datetime import date as _d
    return (_d.today() - d).days <= days


def decision_date_of(item):
    """Normalized YYYY-MM-DD decision date for a record. '' when unknown.

    THE date accessor for this pipeline. The podcast, the briefing and the dashboard all
    window and display on the DECISION date (user direction, 2026-09-21) so the three
    agree by construction — it lives here rather than being re-implemented per consumer.
    Note this is not the release date: CanLII adds judgments long after they are decided.
    """
    if not isinstance(item, dict):
        return ""
    raw = str(item.get("decision_date") or item.get("date_published") or "").strip()
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except Exception:
            continue
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        return ""


def window_start_iso(days):
    """Inclusive start of a rolling window N days back, as YYYY-MM-DD."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


def normalize_citation(cit):
    """'2026 SCC 25' -> '2026scc25' (stable dedup key)."""
    if not cit:
        return None
    return re.sub(r"[^0-9a-z]", "", cit.lower())


def resolve_fingerprint(item):
    """Stable key so one ruling is never covered twice via different URLs."""
    key = normalize_citation(item.get("neutral_citation", "") or "")
    if not key:
        m = CITATION_RE.search(item.get("title", "") or "")
        if m:
            key = normalize_citation(m.group(0))
    if not key:
        key = normalize_citation((item.get("url") or "").strip().lower())
    return key


def court_from_citation(item):
    """Trust the citation over a title substring when they disagree."""
    cit = (item.get("neutral_citation") or "") + " " + (item.get("title") or "")
    m = re.search(r"\b(20\d\d)\s+(SCC|ONCA|ONSCDC|ONSC|ONCJ|FCA|FCT)\b", cit, re.I)
    return m.group(2).lower() if m else None


def looks_criminal_title(title):
    """Cheap, high-precision pre-filter. Measured on Ontario (Sept 2026):
    8/13 ONCJ, 17/72 ONSC, 8/23 ONCA."""
    t = (title or "").strip().lower()
    return t.startswith("r. v.") or t.startswith("r v ") or " v. her majesty" in t


# --- Judgment body: read it off disk -----------------------------------------
# `pull_scc_text.py` and `pull_federal.py` write the judgment to a file and put the
# PATH in `full_text_path`; nothing inlines `full_text`. Verified 2026-09-28 over
# 1,694 archived records: 0 carried the inline key, while 71/79 federal and 3/3 SCC
# records carried the path. classify_ruling read only the inline key, so it read "".
# The classifier had NEVER seen a judgment — it ranked on the headline and a one-line
# synopsis, which is how "Chief Electoral Officer" (party name) and the subject tag
# "Evidence; Contracts" got two civil appeals classified as criminal.
MAX_JUDGMENT_CHARS = 2_000_000
_JUDGMENT_CACHE = {}


def load_judgment_text(item):
    """Judgment body for a record, or "" when none was collected.

    Cached on (path, mtime, size) so the dashboard's API path does not re-read
    100 KB files on every request. Unreadable or missing file => "" — a body is
    optional signal, never a reason to fail a record.
    """
    path = item.get("full_text_path")
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    cached = _JUDGMENT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_JUDGMENT_CHARS)
    except OSError:
        text = ""
    _JUDGMENT_CACHE[key] = text
    return text


# --- The body must clear a criminal-proceeding gate ---------------------------
# A judgment body is 100 KB of ordinary English. OR-ing it into the same substring
# test is a MEASURED REGRESSION, not a fix: wiring it in raw flagged 47 of 52
# full-text records as criminal and 0 of those 47 were criminal — Federal Court
# immigration and judicial-review reasons discuss evidence, warrants, arrest,
# disclosure, police and "sentence" as a matter of course. It also made
# Sinclair-Desgagné MORE confidently criminal (its body hits charter, evidence,
# statement and officer).
#
# So a body may only ADD crime signal when the body is a criminal proceeding, read
# off >=2 independent proceeding markers. Calibration set 2026-09-28: 1/1 criminal
# judgment admitted (R. v. R.B.-C., 2026 SCC 30 — hits all five), 0/51 non-criminal
# admitted. The gate can only ADD signal for a document that is plainly a criminal
# proceeding, so it can never remove what the headline and synopsis already produced.
#
# One positive control is thin. Re-measure before loosening the threshold, and never
# swap this for a raw substring match.
CRIMINAL_PROCEEDING_MARKERS = (
    re.compile(r"\bthe accused\b", re.I),
    re.compile(r"\bCrown counsel\b", re.I),
    re.compile(r"\bindictment\b", re.I),
    re.compile(r"\bbeyond a reasonable doubt\b", re.I),
    re.compile(r"\bguilty plea\b", re.I),
)
CRIMINAL_PROCEEDING_MIN_MARKERS = 2


def is_criminal_proceeding(text):
    """True when the text reads as a criminal prosecution, not a civil or
    immigration proceeding that merely mentions criminal law."""
    if not text:
        return False
    return sum(1 for m in CRIMINAL_PROCEEDING_MARKERS if m.search(text)) \
        >= CRIMINAL_PROCEEDING_MIN_MARKERS


def _split_issue_blocks(text):
    """CanLII `keywords` blocks are separated by ' | ' (normalised to newlines on
    ingest). Older CanLII RSS descriptions used '<subject> — <detail>' lines.
    Handle both so this works regardless of tier."""
    out = []
    if not text:
        return out
    for line in text.replace("<br/>", "\n").replace("<br />", "\n").split("\n"):
        line = line.strip()
        if line:
            out.append(line)
    return out


def classify_ruling(item):
    court = (item.get("court") or court_from_citation(item) or "").lower()

    title = (item.get("title") or "")
    keywords = (item.get("keywords") or "")
    # For metadata-tier records, `description` is the normalised keywords. For
    # full-text-tier records it may be the SCC boilerplate ("New document published
    # on ...") — that boilerplate is deliberately NOT treated as signal.
    desc = (item.get("description") or "")
    if desc.lower().startswith("new document published"):
        desc = ""
    subjects = " ".join(item.get("subjects") or [])
    full_text = item.get("full_text") or ""
    # The judgment body is on disk, not inline — see load_judgment_text.
    judgment_text = load_judgment_text(item)

    # Signal strength depends on tier: full text counts, boilerplate does not.
    body = " ".join(x for x in (keywords, desc, subjects, full_text) if x)
    combined = (title + " " + body).lower()

    # The body is admitted ONLY for a criminal proceeding (gate documented above).
    # Appended to `combined`, so the JP axes and the precedent score see it too.
    if is_criminal_proceeding(judgment_text):
        combined += " " + judgment_text.lower()

    is_criminal = any(kw in combined for kw in CRIME_KEYWORDS) or looks_criminal_title(title)
    is_LE_relevant = any(kw in combined for kw in LE_KEYWORDS) or is_criminal
    is_ontario = court in ONTARIO_COURTS

    # JP docket axes. Precedence decides the briefing section: a criminal case that turns
    # on a Charter breach is a CHARTER matter first, which is the JP's reading order.
    is_charter = bool(CHARTER_RE.search(combined))
    is_bail = bool(BAIL_RE.search(combined))
    is_provincial = bool(PROVINCIAL_OFFENCE_RE.search(combined))
    if is_charter:
        jp_bucket = "charter"
    elif is_bail:
        jp_bucket = "bail"
    elif is_provincial:
        jp_bucket = "provincial"
    elif is_criminal:
        jp_bucket = "criminal"
    else:
        jp_bucket = "other"

    precedent_score, precedent_indicators = 0, []
    for kw in PRECEDENT_KEYWORDS:
        if kw in combined:
            precedent_score += 1
            precedent_indicators.append(kw)
    # Charter section refs: counted only when the document actually invokes the Charter
    # (otherwise it is some other statute's section numbers). See CHARTER_SECTION_RE.
    if "charter" in combined and CHARTER_SECTION_RE.search(combined):
        precedent_score += 1
        precedent_indicators.append("charter s. ref")

    subject_areas = []
    for block in _split_issue_blocks(keywords or desc):
        head = block.split("—")[0].strip()
        if 0 < len(head) < 100 and head not in subject_areas:
            subject_areas.append(head)
    if item.get("topics"):
        subject_areas.insert(0, item["topics"])

    relevance = (
        (precedent_score * 2)
        + (3 if is_criminal else 0)
        + (3 if is_LE_relevant else 0)
        + max(0, 5 - COURT_ORDER.get(court, 99))
        + (2 if is_ontario else 0)   # Ontario weighting
    )

    # `publishedAfter` filters on when CanLII POSTED a decision, not when it was
    # decided — courts and CanLII backfill old judgments constantly. Without this,
    # a 2020 sentencing appears in "this week's rulings". Historical additions are
    # demoted, not deleted, so they still show up on a quiet week.
    decision_date = item.get("decision_date") or item.get("date_published") or ""
    if item.get("date_published_approx") and not item.get("decision_date"):
        is_recent = True   # no date to judge by: do not silently drop it
    else:
        is_recent = _within_days(decision_date, RECENT_DECISION_DAYS)

    return {
        "court": court,
        "decision_date": decision_date,
        "is_recent": is_recent,
        "is_criminal": is_criminal,
        "is_LE_relevant": is_LE_relevant,
        "is_charter": is_charter,
        "is_bail": is_bail,
        "is_provincial_offence": is_provincial,
        "jp_bucket": jp_bucket,
        "is_appellate": court in APPELLATE_COURTS,
        "is_ontario": is_ontario,
        "subject_areas": subject_areas[:6],
        "precedent_score": precedent_score,
        "precedent_indicators": precedent_indicators[:5],
        "court_weight": PRECEDENT_WEIGHT.get(court, "Information"),
        "court_label": COURT_LABELS.get(court, court.upper()),
        "descriptions": _split_issue_blocks(keywords or desc),
        "coverage_tier": item.get("coverage_tier")
                         or ("full_text" if (full_text or judgment_text) else "metadata_only"),
        "combined_relevance": relevance,
    }


# --- Ontario-first ranking ---------------------------------------------------

def rank_key(classified):
    """Sort key implementing the Ontario-first policy.

    Tier 0: Ontario criminal/LE matters   — the focus
    Tier 1: SCC criminal/LE               — apex court, must not be crowded out
    Tier 2: everything else
    Within a tier: relevance desc, then court hierarchy.
    """
    if not classified.get("is_recent", True):
        # Historical backfill: real case, wrong week. Only surfaces if nothing
        # current qualifies, and it is labelled when it does.
        return (3, -classified["combined_relevance"],
                COURT_ORDER.get(classified["court"], 99))
    if classified["is_ontario"] and (classified["is_criminal"] or classified["is_LE_relevant"]):
        tier = 0
    elif classified["court"] == "scc" and (classified["is_criminal"] or classified["is_LE_relevant"]):
        tier = 1
    else:
        tier = 2
    return (tier, -classified["combined_relevance"],
            COURT_ORDER.get(classified["court"], 99))


def select_top(items, n=5, reserve_non_ontario=False):
    """Pick the top `n` under the Ontario-first policy.

    Guardrail: if `reserve_non_ontario`, hold one slot for the best SCC/FCA
    criminal/LE case when one exists — a landmark ruling must not be buried
    because Ontario had a busy week. Never pad: if fewer Ontario matters clear
    the bar than there are slots, the rest go to the next-best overall.
    """
    ranked = sorted(items, key=rank_key)
    if not reserve_non_ontario or n < 2:
        return ranked[:n]

    reserved = next((c for c in ranked
                     if not c["is_ontario"]
                     and c["court"] in ("scc", "fca")
                     and (c["is_criminal"] or c["is_LE_relevant"])), None)
    if reserved is None or reserved in ranked[:n]:
        return ranked[:n]

    top = [c for c in ranked[:n] if c is not reserved]
    if len(top) >= n:
        top = top[:n - 1]
    top.append(reserved)
    return sorted(top, key=rank_key)
