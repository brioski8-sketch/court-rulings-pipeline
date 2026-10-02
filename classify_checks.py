#!/usr/bin/env python3
"""
Regression checks for the classification keyword logic — NO network, runs offline.

Guards the Charter-section fix: section refs MUST be matched Charter-qualified, never as
bare substrings ("s. 1" is inside "s. 12"/"s. 10"/"s. 11", and every statute has section
numbers — a naked "s. 12" hit scored a FAMILY LAW case as precedent-setting).

Named *_checks.py, not test_*.py: the disk-cleanup plugin deletes test_*/tmp_* files under
HERMES_HOME at session end with no age threshold.

Run: python3 classify_checks.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import classify_ruling, CHARTER_SECTION_RE, PRECEDENT_KEYWORDS  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


def section_hit(text):
    """The exact gate classify_ruling applies."""
    t = text.lower()
    return "charter" in t and CHARTER_SECTION_RE.search(t) is not None


print("no bare section refs remain in the keyword list")
for bad in ("s. 1", "s. 2", "s. 7", "s. 8", "s. 9", "s. 10", "s. 11", "s. 12", "s. 24"):
    check(f"{bad!r} removed", bad in PRECEDENT_KEYWORDS, False)

print("\nCharter-qualified section matching")
check("Charter s. 8", section_hit("a breach of Charter s. 8"), True)
check("s. 24(2) of the Charter", section_hit("excluded under s. 24(2) of the Charter"), True)
check("Charter ss. 7 and 8", section_hit("Charter ss. 7 and 8 were engaged"), True)
check("Family Law Act s. 12 -> rejected", section_hit("Family Law Act, s. 12 preservation order"), False)
check("CLRA s. 7 -> rejected", section_hit("Construction Lien Act s. 7"), False)
check("no section ref at all", section_hit("the Charter was not engaged"), False)

print("\n'charter' alone must not manufacture a section hit")
check("charter without a section ref", section_hit("this is a Charter case"), False)

print("\na civil case must not out-score a criminal one on precedent signals")
fam = {"title": "Johnson v. Bobanovic", "court": "onsc",
       "description": "Family - Preservation of property - Family Law Act, s. 12 - "
                      "Definition of spouse - s. 1 - s. 2 - principle",
       "keywords": "", "subjects": []}
crim = {"title": "R. v. Smith", "court": "onca",
        "description": "Criminal law - Charter s. 8 - search and seizure - exclusion of evidence",
        "keywords": "", "subjects": []}
fam_c, crim_c = classify_ruling(fam), classify_ruling(crim)
check("family-law case gains no precedent points from FLA sections",
      fam_c["precedent_score"], 1)   # only 'principle', not s.1/s.2/s.12
check("criminal case keeps its Charter section signal",
      crim_c["precedent_score"] >= 1, True)
check("criminal case out-ranks the family case",
      crim_c["combined_relevance"] > fam_c["combined_relevance"], True)

print("\nJP docket axes")
from classify import (CHARTER_RE, BAIL_RE, PROVINCIAL_OFFENCE_RE,  # noqa: E402
                      JP_BUCKET_ORDER, JP_BUCKET_LABELS)

def bucket_of(title, body, court="oncj"):
    return classify_ruling({"title": title, "court": court, "description": body,
                            "keywords": "", "subjects": []})["jp_bucket"]

check("Charter case -> charter bucket",
      bucket_of("R. v. Smith", "Charter s. 8 - exclusion of evidence under s. 24(2)"), "charter")
check("bail case -> bail bucket",
      bucket_of("R. v. Jones", "judicial interim release - show cause - surety"), "bail")
check("HTA prosecution -> provincial bucket",
      bucket_of("R. v. Lee", "Highway Traffic Act - speeding - s. 128"), "provincial")
check("municipal prosecution -> provincial bucket",
      bucket_of("Brampton (City) v. Wright", "Provincial Offences Act - by-law contravention"), "provincial")
check("plain criminal -> criminal bucket",
      bucket_of("R. v. Doe", "Criminal Code - assault - sentencing"), "criminal")
check("civil -> other bucket",
      bucket_of("Acme Inc. v. Beta Ltd.", "contract - damages - negligence", "onsc"), "other")

print("\nword boundaries (the substring trap that already bit the section refs)")
check("'bailiff' is not bail", bool(BAIL_RE.search("the bailiff attended")), False)
check("'bailed out' is not bail", bool(BAIL_RE.search("he bailed out")), False)
check("'bail' is bail", bool(BAIL_RE.search("bail was denied")), True)
check("'s. 800' is not a Charter s. 8 ref", bool(CHARTER_RE.search("s. 800 of the Code")), False)
check("'Highway Traffic Act' flagged", bool(PROVINCIAL_OFFENCE_RE.search("Highway Traffic Act, s. 128")), True)

print("\nbriefing section order is JP priority")
check("charter leads", JP_BUCKET_ORDER[0], "charter")
check("bail second", JP_BUCKET_ORDER[1], "bail")
check("provincial third", JP_BUCKET_ORDER[2], "provincial")
check("every bucket has a label", all(b in JP_BUCKET_LABELS for b in JP_BUCKET_ORDER), True)

print("\njudgment body: read off disk, then gated on a criminal proceeding")
import tempfile  # noqa: E402
from classify import load_judgment_text, is_criminal_proceeding, _JUDGMENT_CACHE  # noqa: E402

_body_dir = os.path.join(tempfile.gettempdir(), "classify_check_bodies")
os.makedirs(_body_dir, exist_ok=True)


def _body(name, text):
    p = os.path.join(_body_dir, name)
    with open(p, "w") as f:
        f.write(text)
    return {"title": "Acme Inc. v. Beta Ltd.", "court": "onsc", "description": "",
            "keywords": "", "subjects": [], "full_text_path": p}


# The bug this guards: writers put the body in `full_text_path`, and classify_ruling
# read the inline `full_text` key, which no tier ever populates.
check("body is read from full_text_path",
      load_judgment_text(_body("wired.txt", "the accused pleaded guilty")) != "", True)
check("record with only a path reports full_text coverage",
      classify_ruling(_body("cov.txt", "the accused was convicted")).get("coverage_tier"), "full_text")
check("missing file yields empty body, not an exception",
      load_judgment_text({"full_text_path": os.path.join(_body_dir, "nope.txt")}), "")
check("record with no body stays metadata_only",
      classify_ruling({"title": "Acme Inc. v. Beta Ltd.", "court": "onsc",
                       "description": "", "keywords": "", "subjects": []})["coverage_tier"],
      "metadata_only")

# The regression this guards: a raw substring match over 100 KB of ordinary English
# flagged 47 of 52 full-text records criminal, 0 of them criminal.
CIVIL_BODY = ("The applicant seeks judicial review. The evidence was filed. A warrant "
              "was executed by police. He was arrested and released on bail. The "
              "sentence of the tribunal is disclosed. Statement of claim, search and "
              "seizure, custody of the funds, driving record, officer of the corporation.")
check("civil body full of crime words is NOT admitted",
      is_criminal_proceeding(CIVIL_BODY), False)
check("civil body does not make the record criminal",
      classify_ruling(_body("civil.txt", CIVIL_BODY))["is_criminal"], False)

ELECTION_BODY = ("The returning officer counted the ballots. The statement of the vote "
                 "was incorrect. The Canada Elections Act, s. 524, and the Canadian "
                 "Charter of Rights and Freedoms, s. 3, are engaged.")
check("election body is NOT admitted (Sinclair-Desgagne shape)",
      is_criminal_proceeding(ELECTION_BODY), False)

CRIM_BODY = ("The accused was charged with robbery. Crown counsel called evidence. "
             "The indictment alleged assault. Proof beyond a reasonable doubt.")
check("criminal body IS admitted",
      is_criminal_proceeding(CRIM_BODY), True)
check("criminal body contributes crime signal",
      classify_ruling(_body("crim_body.txt", CRIM_BODY))["is_criminal"], True)

check("one marker alone is not enough",
      is_criminal_proceeding("the accused attended a civil mediation"), False)
check("gate never admits an empty body", is_criminal_proceeding(""), False)

_before = len(_JUDGMENT_CACHE)
_rec = _body("cache.txt", "the accused was convicted by a jury")
load_judgment_text(_rec)
_growth = len(_JUDGMENT_CACHE) - _before
load_judgment_text(_rec)
check("body read is cached on (path, mtime, size)",
      len(_JUDGMENT_CACHE) - _before - _growth, 0)

print("\nconsumers use the SHARED classifier (no duplicated keyword lists)")
# Three copies of this logic already drifted once. These guard against a fourth appearing.
_pod = os.path.expanduser("~/.hermes/scripts/generate_court_podcast_data.py")
_psrc = open(_pod).read()
check("podcast imports the pipeline classifier", "from classify import" in _psrc, True)
check("podcast has no local CRIME_KEYWORDS list", "CRIME_KEYWORDS = [" not in _psrc, True)
check("podcast has no local classify_ruling()", "def classify_ruling(" not in _psrc, True)
check("podcast carries the JP bucket into its output", '"jp_bucket"' in _psrc, True)

_dash = "/opt/jarvis-lite/server/cron_sections.py"
if os.path.exists(_dash):
    _dsrc = open(_dash).read()
    check("dashboard imports the pipeline classifier",
          "from classify import classify_ruling" in _dsrc, True)
    check("dashboard exposes jp_bucket", '"jp_bucket"' in _dsrc, True)
    check("dashboard has no duplicate section-ref keywords",
          '"s. 1", "s. 2"' not in _dsrc, True)

_js = "/opt/jarvis-lite/static/cron_sections.js"
if os.path.exists(_js):
    _jsrc = open(_js).read()
    check("dashboard UI shows a docket badge", "courtDocketBadge" in _jsrc, True)
    check("dashboard UI defaults to docket order", 'sort: "docket"' in _jsrc, True)

print("\nJev shadow still imports and builds a state (no API)")
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "jev_shadow", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev_shadow.py"))
js = importlib.util.module_from_spec(spec)
spec.loader.exec_module(js)
check("build_state works on an empty record", isinstance(js.build_state({}), str), True)
check("importance question is wired into the shadow",
      "importance" in js.question_set(), True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
