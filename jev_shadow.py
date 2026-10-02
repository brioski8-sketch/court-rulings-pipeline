#!/usr/bin/env python3
"""
Jev shadow-mode hook for the court-rulings pipeline.

WHAT THIS DOES
  Runs TypeSafe Jev (a decision model: state in -> typed questions out) alongside the
  existing keyword classifier in classify.py, and writes Jev's verdicts to a SIDE FILE.
  It does NOT change the briefing. Nothing Jev says reaches the user-facing output.

  After a few Mondays we diff the two lists and decide - with real data - whether Jev is
  worth promoting into the live path. Shadow mode is the low-risk first step.

DECISION POINTS (see the llm-model-evaluation skill)
  - Put the model behind a swappable backend function (decide() below).
  - Ask the frame question ("is this even the right lens?") in the SAME call as the pick.
    Jev's `confidence` scores the PICK among the options supplied, never the FRAME, so a
    confident wrong pick is the dangerous case. We gate on BOTH.
  - Feed only the state the question needs (title + court + synopsis, capped).
  - Never write a model answer straight into an append-only store - side file, always.

USAGE
  python3 jev_shadow.py --limit 12                 # shadow-score 12 rulings (spread sample)
  python3 jev_shadow.py --limit 12 --date 2026-09-17
  python3 jev_shadow.py --limit 12 --dry-run       # build states, no API calls
  python3 jev_shadow.py --limit 12 --model typesafe/jev-latest

Exit 0 = ran, 1 = no rulings / setup problem. A single failed call never aborts the run.
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from classify import classify_ruling, rank_key, resolve_fingerprint  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
SHADOW_DIR = os.path.join(BASE_DIR, "shadow")

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

# --- thresholds (verify against real output before trusting) -------------------
NOUL_THRESHOLD = 0.50        # noul >= this reads as "yes"
CHOICE_CONF_FLOOR = 0.50     # choice confidence below this -> review, never auto-accept
# MEASURED 2026-09-20 on 12 real rulings: a GOOD state scores ~0.52-0.73 on the
# judgeability frame; a bad/substituted frame scored 0.05-0.20. Gate at 0.35 so the
# safety net stays silent on good input and only fires on genuinely unusable text.
FRAME_THRESHOLD = 0.35

# How much of a judgment to hand over. Context is 32k on OpenRouter; per the skill we
# feed only what the question needs. ~6k chars ~= 1.5k tokens.
MAX_FULLTEXT_CHARS = 6000
MAX_SYNOPSIS_CHARS = 3000

TIER_ORDER = ("federal_", "scc_", "canlii_", "rulings_")   # richest first


# --- swappable backend seam ----------------------------------------------------

def _load_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def decide(state, questions, model=DEFAULT_MODEL, timeout=90):
    """POST one call to OpenRouter's decisions endpoint. Returns (parsed_json, meta).

    meta = {"ok": bool, "http": int|None, "error": str|None, "cost": float}.
    The body is a VERBATIM passthrough of TypeSafe's native schema - the only thing the
    aggregator changes is the URL and the model id.
    """
    key = _load_key()
    if not key:
        return None, {"ok": False, "http": None, "error": "OPENROUTER_API_KEY not found", "cost": 0.0}

    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        cost = float((data.get("usage") or {}).get("cost") or 0.0)
        return data, {"ok": True, "http": resp.status, "error": None, "cost": cost}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return None, {"ok": False, "http": e.code, "error": detail, "cost": 0.0}
    except Exception as e:  # noqa: BLE001 - a dead call must not abort the batch
        return None, {"ok": False, "http": None, "error": repr(e)[:300], "cost": 0.0}


# --- questions -----------------------------------------------------------------

# One pick + one frame, same call, parallel evaluation against shared state.
def question_set():
    return {
        "criminal_matter": {
            "type": "noul",
            "instructions": (
                "Is the subject matter of this case criminal law - Criminal Code offences, "
                "sentencing, bail, Charter ss. 7-10 or s. 24, police powers, or evidence in a "
                "criminal prosecution? Answer false for purely civil, family, immigration, tax, "
                "commercial or administrative matters."
            ),
        },
        # FRAME question: verifies the PRECONDITION the relevance pick rests on - that the
        # state is actually judgeable. It must NOT restate the criminal_matter question:
        # a first version ("is criminal-law the right lens?") tracked criminal_matter 1:1,
        # added no independent signal, and false-flagged a criminal ONCA case (crim p=0.99,
        # frame 0.48). This version is independent of the subject matter.
        "frame_ok": {
            "type": "noul",
            "instructions": (
                "Was there enough information in the text above to judge this item's relevance "
                "to a police analyst? Answer false ONLY if the text is empty, truncated, or does "
                "not describe an actual court ruling."
            ),
        },
        "relevance": {
            "type": "choice",
            "instructions": (
                "How relevant is this ruling to an Ontario Justice of the Peace - someone who "
                "presides over bail, provincial-offence trials and Charter applications? "
                "Weight Charter breaches and bail law heavily."
            ),
            "criteria": {
                "High": ("Directly affects how Ontario police investigate, charge or testify: "
                         "Charter/search, bail, sentencing, evidence, identification, police powers."),
                "Medium": ("Criminal or justice-related but indirect or narrow - procedural, a "
                           "peripheral offence, or confined to another province's facts."),
                "Low": ("Mentions criminal law only in passing; the outcome does not affect "
                        "police practice."),
                "None": "Not a criminal-law matter at all.",
            },
        },
        # JP-DOCKET AXES. A Justice of the Peace presides over far more than the Criminal
        # Code: provincial offences (HTA, municipal by-laws, liquor), bail, and Charter
        # applications. A single "is it criminal?" flag cannot separate those, and the
        # pipeline's keyword classifier labels EVERY "R. v." matter criminal — including
        # municipal prosecutions (Toronto (City) v. Greene, R. v. McSevney). Ask directly.
        "matter_type": {
            "type": "choice",
            "instructions": "What kind of proceeding is this?",
            "criteria": {
                "Criminal Code offence": (
                    "A charge, conviction, sentence or appeal under the federal Criminal Code."),
                "Provincial or regulatory offence": (
                    "A prosecution under a provincial statute or municipal by-law - Highway "
                    "Traffic Act, Liquor Licence Act, municipal by-laws, fish and wildlife, "
                    "compulsory automobile insurance, etc."),
                "Civil or administrative": (
                    "A private dispute or a tribunal/administrative matter - contract, tort, "
                    "family, real property, immigration, labour."),
                "Other": "None of the above, or unclear from the text.",
            },
        },
        "charter_issue": {
            "type": "noul",
            "instructions": (
                "Does this case turn on a Canadian Charter of Rights and Freedoms issue - an "
                "alleged breach of ss. 7-10, or exclusion of evidence under s. 24(2), or a "
                "search and seizure, detention, or right to counsel question?"
            ),
        },
        "bail_related": {
            "type": "noul",
            "instructions": (
                "Is this case about bail or judicial interim release - a bail hearing, a show "
                "cause, a detention order, a surety, or a review of a release decision?"
            ),
        },
        # IMPORTANCE - distinct from relevance. Relevance = does this change how Ontario
        # police work. Importance = how significant is it as a development in the law.
        # classify.py folds both into one integer; a lawyer cares about the second one.
        # The `score` primitive returns a position on an ordered legend + per-level probs.
        "importance": {
            "type": "score",
            "instructions": (
                "How significant is this ruling as a development in Canadian criminal law? "
                "Judge the legal weight of the ruling itself, not how interesting the facts are."
            ),
            "criteria": ["Landmark", "Significant", "Routine", "Minimal"],
        },
    }


# --- state builder -------------------------------------------------------------

def build_state(rec):
    """Compact state: only what these questions need."""
    parts = [
        f"Title: {rec.get('title', '')}",
        f"Court: {(rec.get('court') or 'unknown').upper()}",
        f"Citation: {rec.get('neutral_citation') or '(none)'}",
        f"Decision date: {rec.get('decision_date') or rec.get('date_published') or '(unknown)'}",
    ]
    synopsis = (rec.get("keywords") or rec.get("description") or "").strip()
    if synopsis.lower().startswith("new document published"):
        synopsis = ""
    if synopsis:
        parts.append("Synopsis: " + synopsis[:MAX_SYNOPSIS_CHARS])

    ft_path = rec.get("full_text_path")
    full = ""
    if ft_path and os.path.exists(ft_path):
        try:
            full = open(ft_path, encoding="utf-8", errors="replace").read()
        except OSError:
            full = ""
    if full:
        parts.append("Judgment text (excerpt):\n" + full[:MAX_FULLTEXT_CHARS])

    subjects = rec.get("subjects") or []
    if subjects:
        parts.append("Subjects: " + "; ".join(str(s) for s in subjects)[:500])
    return "\n".join(parts)


# --- load + merge (mirrors analyze_rulings.py so the shadow sees the same cases) --

def load_merged(cutoff_date=None, prefixes=TIER_ORDER):
    raw = []
    for prefix in prefixes:
        for fname in sorted(os.listdir(DATA_DIR)):
            if not fname.startswith(prefix) or not fname.endswith(".jsonl"):
                continue
            if cutoff_date:
                import re as _re
                m = _re.search(r"(\d{4}-\d{2}-\d{2})", fname)
                if m and m.group(1) < cutoff_date:
                    continue
            with open(os.path.join(DATA_DIR, fname)) as f:
                for line in f:
                    try:
                        raw.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    seen, out = set(), []
    for rec in raw:
        fp = resolve_fingerprint(rec)
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        out.append(rec)
    return out


def spread_sample(items, n):
    """Take an evenly-spread sample so the test set is not all one court/tier."""
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


# --- shadow scoring ------------------------------------------------------------

def _score_label(ans):
    """Map the `score` primitive's numeric position onto its legend label.
    Arithmetic stays in code - the model returns a position, we round it."""
    legend = ans.get("legend") or {}
    pos = ans.get("score")
    if pos is None or not legend:
        return None
    idx = str(int(round(float(pos))))
    return legend.get(idx) or legend.get(int(idx))


def score_one(rec, model=DEFAULT_MODEL, dry_run=False):
    state = build_state(rec)
    kw = classify_ruling(rec)
    row = {
        "fingerprint": resolve_fingerprint(rec),
        "title": rec.get("title", ""),
        "court": kw["court"],
        "citation": rec.get("neutral_citation") or "",
        "coverage_tier": kw["coverage_tier"],
        "keyword": {
            "is_criminal": kw["is_criminal"],
            "is_LE_relevant": kw["is_LE_relevant"],
            "relevance_score": kw["combined_relevance"],
            "rank_tier": rank_key(kw)[0],
            "jp_bucket": kw.get("jp_bucket"),
        },
        "state_chars": len(state),
        "jev": None,
        "cost": 0.0,
        "error": None,
    }
    if dry_run:
        return row

    data, meta = decide(state, question_set(), model=model)
    row["cost"] = meta["cost"]
    if not meta["ok"]:
        row["error"] = meta["error"]
        return row

    ans = data.get("answers") or {}
    crim = ans.get("criminal_matter", {}).get("noul")
    frame = ans.get("frame_ok", {}).get("noul")
    rel = ans.get("relevance", {}) or {}
    choice = rel.get("choice")
    conf = rel.get("confidence")
    imp = ans.get("importance", {}) or {}
    mt = ans.get("matter_type", {}) or {}
    charter = ans.get("charter_issue", {}).get("noul")
    bail = ans.get("bail_related", {}).get("noul")

    # Gate: BOTH the pick and the frame have to be confident, AND the two criminal
    # judgments have to agree. Anything else is flagged for review, never auto-accepted.
    frame_valid = frame is not None and frame >= FRAME_THRESHOLD
    crim_agree = crim is not None and (crim >= NOUL_THRESHOLD) == bool(kw["is_criminal"])
    conf_ok = conf is not None and conf >= CHOICE_CONF_FLOOR

    if not frame_valid:
        flag = "STATE_UNJUDGEABLE"  # empty/truncated state - fix upstream, not the option list
    elif not conf_ok:
        flag = "LOW_CONF"           # pick not confident enough to trust
    elif not crim_agree:
        flag = "CRIM_DISAGREE"      # keyword and Jev disagree on criminal/not
    else:
        flag = "OK"

    row["jev"] = {
        "criminal_matter_noul": crim,
        "frame_ok_noul": frame,
        "relevance_choice": choice,
        "relevance_confidence": conf,
        "relevance_probabilities": rel.get("probabilities"),
        "importance_score": imp.get("score"),
        "importance_label": _score_label(imp),
        "importance_confidence": imp.get("confidence"),
        "importance_probabilities": imp.get("probabilities"),
        "matter_type": mt.get("choice"),
        "matter_type_confidence": mt.get("confidence"),
        "charter_issue_noul": charter,
        "bail_related_noul": bail,
        "criminal_agree": crim_agree,
        "auto_accept": flag == "OK",
        "flag": flag,
        "model": data.get("model"),
    }
    return row


def _marker_path():
    return os.path.join(SHADOW_DIR, ".last_shadow")


def _read_marker():
    p = _marker_path()
    if os.path.exists(p):
        v = open(p).read().strip()
        if v:
            return v[:10]
    return None


def _write_marker():
    os.makedirs(SHADOW_DIR, exist_ok=True)
    with open(_marker_path(), "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def run(limit=12, cutoff_date=None, model=DEFAULT_MODEL, dry_run=False,
        since_last=False, max_items=200):
    if since_last:
        # Own watermark, NOT the briefing's .last_report marker: analyze_rulings.py
        # advances that one at the end of its run, so reusing it would score nothing.
        m = _read_marker()
        cutoff_date = m or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"Watermark: {cutoff_date} (scoring rulings pulled since then)")

    items = load_merged(cutoff_date=cutoff_date)
    if not items:
        print("No new rulings since the last shadow run.")
        if since_last and not dry_run:
            _write_marker()
        return None
    sample = spread_sample(items, limit) if limit else items
    # Cap the SAMPLE, never the pool: truncating `items` before sampling biases the run
    # toward whatever sorts first in the data dir (federal records), silently.
    if len(sample) > max_items:
        print(f"  capping at {max_items} of {len(sample)} selected rulings")
        sample = sample[:max_items]
    print(f"Loaded {len(items)} merged rulings; shadow-scoring {len(sample)} (model={model}"
          f"{', DRY RUN' if dry_run else ''})")

    rows, total_cost = [], 0.0
    for i, rec in enumerate(sample, 1):
        r = score_one(rec, model=model, dry_run=dry_run)
        rows.append(r)
        total_cost += r["cost"]
        tag = "ERR" if r["error"] else ("SKIP" if dry_run else (r["jev"]["flag"] if r["jev"] else "?"))
        print(f"  [{i:2}/{len(sample)}] {tag:13} {(r['citation'] or r['title'])[:55]}")
        if not dry_run:
            time.sleep(0.3)

    os.makedirs(SHADOW_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    jsonl_path = os.path.join(SHADOW_DIR, f"jev_shadow_{stamp}.jsonl")
    with open(jsonl_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    summary = summarize(rows, sample, total_cost, model, dry_run, stamp)
    md_path = os.path.join(SHADOW_DIR, f"jev_shadow_{stamp}.md")
    with open(md_path, "w") as f:
        f.write(summary)

    # The EMAILED artefacts: a rendered HTML table (markdown pipes don't line up in a mail
    # client) and a CSV for sorting/filtering in a spreadsheet.
    html_path = os.path.join(SHADOW_DIR, f"jev_shadow_{stamp}.html")
    with open(html_path, "w") as f:
        f.write("<html><head><meta charset='utf-8'></head><body>"
                + render_html(rows, sample, total_cost, model, dry_run, stamp) + "</body></html>")
    csv_path = os.path.join(SHADOW_DIR, f"jev_shadow_{stamp}.csv")
    with open(csv_path, "w", newline="") as f:
        f.write(render_csv(rows))

    print("\n" + summary)
    print(f"\nSide files: {jsonl_path}\n            {md_path}\n            {html_path}\n            {csv_path}")
    if since_last and not dry_run:
        _write_marker()
    return {"rows": rows, "jsonl": jsonl_path, "md": md_path, "html": html_path,
            "csv": csv_path, "cost": total_cost}


def _f(v, nd=2):
    """Format a nullable float for a table cell."""
    return "-" if v is None else f"{float(v):.{nd}f}"


def _esc(s):
    """Minimal HTML escape for table cells."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_HTML_CSS = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         color: #1f2328; font-size: 13px; }
  h2 { font-size: 15px; margin: 22px 0 8px; }
  table { border-collapse: collapse; margin: 8px 0 20px; }
  th { background: #24292f; color: #fff; text-align: left; padding: 6px 9px;
       font-size: 12px; white-space: nowrap; }
  td { border-bottom: 1px solid #e1e4e8; padding: 5px 9px; vertical-align: top; }
  tr:nth-child(even) td { background: #f6f8fa; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .flag-OK { color: #1a7f37; font-weight: 600; }
  .flag-LOW_CONF { color: #9a6700; font-weight: 600; }
  .flag-CRIM_DISAGREE { color: #cf222e; font-weight: 600; }
  .flag-STATE_UNJUDGEABLE { color: #cf222e; font-weight: 600; }
  .hit { font-weight: 700; color: #1a7f37; }
  .sum td { border-bottom: 1px solid #e1e4e8; }
  .note { color: #57606a; }
</style>
"""

FLAG_MEANING = {
    "OK": "pick + frame confident and both criminal judgments agree",
    "STATE_UNJUDGEABLE": "not enough text to judge - fix the state upstream",
    "LOW_CONF": "relevance pick below the confidence floor - not trusted",
    "CRIM_DISAGREE": "keyword classifier and Jev disagree on criminal/not",
}


def render_html(rows, sample, total_cost, model, dry_run, stamp):
    """The emailed report: a RENDERED table. Raw markdown has aligned pipes in source but
    reads as unlined plain text in a mail client, which is what the user rejected."""
    ok = [r for r in rows if r.get("jev")]
    errs = [r for r in rows if r.get("error")]
    flags = Counter(r["jev"]["flag"] for r in ok)
    agree = [r for r in ok if r["jev"]["criminal_agree"]]

    out = [_HTML_CSS, f"<h2>Jev shadow report &mdash; {_esc(stamp)}</h2>"]
    out.append('<p class="note"><b>Shadow mode.</b> These verdicts are NOT used by the '
               'briefing. They exist only so the keyword classifier and Jev can be compared '
               'on real data before anything is promoted.</p>')

    out.append("<h2>Summary</h2><table class='sum'>")
    for k, v in (("Model", f"<code>{_esc(model)}</code>"),
                 ("Rulings scored", str(len(rows)) + (" (dry run)" if dry_run else "")),
                 ("API errors", str(len(errs))),
                 ("Total cost", f"${total_cost:.6f}"),
                 ("Criminal / not agreement",
                  f"{len(agree)}/{len(ok)} ({100 * len(agree) // len(ok)}%)" if ok else "-")):
        out.append(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>")
    out.append("</table>")

    if not ok:
        out.append("<p>No successful Jev calls to compare.</p>")
        return "\n".join(out) + "</body>"

    out.append("<h2>Gate outcomes</h2><table>")
    out.append("<tr><th>Flag</th><th>Count</th><th>Meaning</th></tr>")
    for flag, n in flags.most_common():
        out.append(f"<tr><td class='flag-{_esc(flag)}'>{_esc(flag)}</td>"
                   f"<td class='num'>{n}</td><td>{_esc(FLAG_MEANING.get(flag, ''))}</td></tr>")
    out.append("</table>")

    out.append("<h2>Per-ruling comparison</h2><table>")
    out.append("<tr><th>#</th><th>Ruling</th><th>Court</th><th>Jev matter type</th>"
               "<th>Relevance</th><th>conf</th><th>Importance</th><th>pos</th>"
               "<th>Charter</th><th>Bail</th><th>KW crim</th><th>Jev crim</th><th>Flag</th></tr>")
    order = {b: i for i, b in enumerate(("charter", "bail", "provincial", "criminal", "other"))}

    def cell(v, bold=False):
        s = _f(v)
        return f"<td class='num'>{'<span class=hit>' + s + '</span>' if bold else s}</td>"

    for n, r in enumerate(sorted(ok, key=lambda x: (
            order.get(x.get("jp_bucket"), 9),
            x["jev"].get("importance_score")
            if x["jev"].get("importance_score") is not None else 99)), 1):
        j = r["jev"]
        out.append(
            f"<tr><td class='num'>{n}</td>"
            f"<td>{_esc((r['citation'] or r['title'])[:52])}</td>"
            f"<td>{_esc(r['court'])}</td>"
            f"<td>{_esc(j.get('matter_type') or '-')}</td>"
            f"<td>{_esc(j.get('relevance_choice') or '-')}</td>"
            + cell(j.get("relevance_confidence"))
            + f"<td>{_esc(j.get('importance_label') or '-')}</td>"
            + cell(j.get("importance_score"))
            + cell(j.get("charter_issue_noul"), (j.get("charter_issue_noul") or 0) >= 0.5)
            + cell(j.get("bail_related_noul"), (j.get("bail_related_noul") or 0) >= 0.5)
            + f"<td>{'yes' if r['keyword']['is_criminal'] else 'no'}</td>"
            + f"<td>{'yes' if (j.get('criminal_matter_noul') or 0) >= 0.5 else 'no'}</td>"
            + f"<td class='flag-{_esc(j.get('flag'))}'>{_esc(j.get('flag'))}</td></tr>")
    out.append("</table>")

    disagree = [r for r in ok if not r["jev"]["criminal_agree"]]
    if disagree:
        out.append("<h2>Disagreements to review</h2><table>")
        out.append("<tr><th>Ruling</th><th>Court</th><th>Keyword says</th><th>Jev criminal</th>"
                   "<th>Jev matter type</th><th>Relevance</th></tr>")
        for r in disagree:
            j = r["jev"]
            out.append(f"<tr><td>{_esc((r['citation'] or r['title'])[:48])}</td>"
                       f"<td>{_esc(r['court'])}</td>"
                       f"<td>criminal={r['keyword']['is_criminal']}</td>"
                       f"<td class='num'>{_f(j.get('criminal_matter_noul'))}</td>"
                       f"<td>{_esc(j.get('matter_type') or '-')}</td>"
                       f"<td>{_esc(j.get('relevance_choice') or '-')} @ "
                       f"{_f(j.get('relevance_confidence'))}</td></tr>")
        out.append("</table>")

    ch = [r for r in ok if (r["jev"].get("charter_issue_noul") or 0) >= 0.5]
    ba = [r for r in ok if (r["jev"].get("bail_related_noul") or 0) >= 0.5]
    out.append(f"<h2>JP docket flags</h2><p><b>Charter issues:</b> {len(ch)}"
               + (" &mdash; " + ", ".join(_esc((r['citation'] or r['title'])[:40]) for r in ch[:8])
                  if ch else "") + "<br>"
               + f"<b>Bail / interim release:</b> {len(ba)}"
               + (" &mdash; " + ", ".join(_esc((r['citation'] or r['title'])[:40]) for r in ba[:8])
                  if ba else "") + "</p>")
    return "\n".join(out) + "</body>"


CSV_COLUMNS = ("ruling", "citation", "court", "coverage_tier", "kw_bucket", "kw_criminal",
               "jev_matter_type", "jev_matter_conf", "relevance", "relevance_conf",
               "importance", "importance_pos", "importance_conf", "charter_p", "bail_p",
               "jev_criminal_p", "flag")


def render_csv(rows):
    """Same data as CSV so it can be sorted and filtered in a spreadsheet."""
    import csv as _csv
    import io
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        j = r.get("jev") or {}
        w.writerow({
            "ruling": r.get("title", ""),
            "citation": r.get("citation", ""),
            "court": r.get("court", ""),
            "coverage_tier": r.get("coverage_tier", ""),
            "kw_bucket": (r.get("keyword") or {}).get("jp_bucket", ""),
            "kw_criminal": (r.get("keyword") or {}).get("is_criminal", ""),
            "jev_matter_type": j.get("matter_type", ""),
            "jev_matter_conf": j.get("matter_type_confidence", ""),
            "relevance": j.get("relevance_choice", ""),
            "relevance_conf": j.get("relevance_confidence", ""),
            "importance": j.get("importance_label", ""),
            "importance_pos": j.get("importance_score", ""),
            "importance_conf": j.get("importance_confidence", ""),
            "charter_p": j.get("charter_issue_noul", ""),
            "bail_p": j.get("bail_related_noul", ""),
            "jev_criminal_p": j.get("criminal_matter_noul", ""),
            "flag": j.get("flag", ""),
        })
    return buf.getvalue()


def summarize(rows, sample, total_cost, model, dry_run, stamp):
    """Build the readable report. Markdown TABLES, not bullet soup - this is delivered as a
    FILE (email attachment), so a table is readable here in a way it never was in chat."""
    ok = [r for r in rows if r.get("jev")]
    errs = [r for r in rows if r.get("error")]
    flags = Counter(r["jev"]["flag"] for r in ok)
    agree = [r for r in ok if r["jev"]["criminal_agree"]]

    FLAG_MEANING = {
        "OK": "pick + frame confident and both criminal judgments agree",
        "STATE_UNJUDGEABLE": "not enough text to judge - fix the state upstream",
        "LOW_CONF": "relevance pick below the confidence floor - not trusted",
        "CRIM_DISAGREE": "keyword classifier and Jev disagree on criminal/not",
    }

    L = [f"# Jev shadow report - {stamp}", ""]
    L.append("**Shadow mode.** These verdicts are NOT used by the briefing. They exist only so the")
    L.append("keyword classifier and Jev can be compared on real data before anything is promoted.")
    L.append("")

    L += ["## Summary", "", "| Metric | Value |", "|---|---|",
          f"| Model | `{model}` |",
          f"| Rulings scored | {len(rows)}{' (DRY RUN)' if dry_run else ''} |",
          f"| API errors | {len(errs)} |",
          f"| Total cost | ${total_cost:.6f} |"]
    if ok:
        L.append(f"| Criminal / not agreement | {len(agree)}/{len(ok)} "
                 f"({100 * len(agree) // len(ok)}%) |")
    L.append("")

    if not ok:
        L.append("No successful Jev calls to compare.")
        if errs:
            L += ["", "### Errors", ""]
            L += [f"- `{(e.get('citation') or e.get('title') or '')[:60]}` - {e['error']}" for e in errs]
        return "\n".join(L)

    mid = [r for r in ok if r.get("matter_type") not in ("Criminal Code offence", None)
           and r["keyword"]["is_criminal"]]
    L += ["## Gate outcomes", "", "| Flag | Count | Meaning |", "|---|---|---|"]
    for flag, n in flags.most_common():
        L.append(f"| {flag} | {n} | {FLAG_MEANING.get(flag, '')} |")
    L.append("")
    L.append(f"Labelled criminal by the keyword classifier but read otherwise by Jev: **{len(mid)}**")
    L.append("")

    L += ["## Per-ruling comparison", "",
          "| # | Ruling | Court | Jev matter type | Relevance | conf | Importance | pos | "
          "Charter | Bail | KW crim | Jev crim | Flag |",
          "|---:|---|---|---|---|---:|---|---:|---:|---:|---|---|---|"]
    order = {b: i for i, b in enumerate(("charter", "bail", "provincial", "criminal", "other"))}
    for n, r in enumerate(sorted(ok, key=lambda x: (order.get(x.get("jp_bucket"), 9),
                                                    x["jev"].get("importance_score")
                                                    if x["jev"].get("importance_score") is not None else 99)), 1):
        j = r["jev"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            n,
            (r["citation"] or r["title"])[:46].replace("|", "/"),
            r["court"],
            j.get("matter_type") or "-",
            j.get("relevance_choice") or "-",
            _f(j.get("relevance_confidence")),
            j.get("importance_label") or "-",
            _f(j.get("importance_score")),
            _f(j.get("charter_issue_noul")),
            _f(j.get("bail_related_noul")),
            "yes" if r["keyword"]["is_criminal"] else "no",
            "yes" if (j.get("criminal_matter_noul") or 0) >= 0.5 else "no",
            j.get("flag"),
        ))
    L.append("")

    disagree = [r for r in ok if not r["jev"]["criminal_agree"]]
    if disagree:
        L += ["## Disagreements to review", "",
              "| Ruling | Court | Keyword says | Jev criminal | Jev matter type | Relevance |",
              "|---|---|---|---|---|---|"]
        for r in disagree:
            j = r["jev"]
            L.append("| {} | {} | criminal={} | p={} | {} | {} @ {} |".format(
                (r["citation"] or r["title"])[:44].replace("|", "/"), r["court"],
                r["keyword"]["is_criminal"], _f(j.get("criminal_matter_noul")),
                j.get("matter_type") or "-", j.get("relevance_choice") or "-",
                _f(j.get("relevance_confidence"))))
        L.append("")

    ch = [r for r in ok if (r["jev"].get("charter_issue_noul") or 0) >= 0.5]
    ba = [r for r in ok if (r["jev"].get("bail_related_noul") or 0) >= 0.5]
    L += ["## JP docket flags", "",
          f"- **Charter issues:** {len(ch)}" + (" - " + ", ".join(
              (r["citation"] or r["title"])[:40] for r in ch[:8]) if ch else ""),
          f"- **Bail / interim release:** {len(ba)}" + (" - " + ", ".join(
              (r["citation"] or r["title"])[:40] for r in ba[:8]) if ba else ""),
          ""]

    if errs:
        L += ["## Errors", ""]
        L += [f"- `{(e.get('citation') or e.get('title') or '')[:60]}` - {e['error']}" for e in errs]
        L.append("")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Jev shadow-mode hook for court rulings")
    ap.add_argument("--limit", type=int, default=12,
                    help="max items to score per run (0 = no cap)")
    ap.add_argument("--date", default=None, help="cutoff YYYY-MM-DD (only files on/after)")
    ap.add_argument("--since-last", action="store_true",
                    help="score only rulings pulled since the last shadow run (cron mode)")
    ap.add_argument("--max-items", type=int, default=200,
                    help="hard safety cap on items scored in one run")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run(limit=args.limit, cutoff_date=args.date, model=args.model,
              dry_run=args.dry_run, since_last=args.since_last, max_items=args.max_items)
    return 0 if out else 0   # nothing new is not an error for a weekly job


if __name__ == "__main__":
    sys.exit(main())
