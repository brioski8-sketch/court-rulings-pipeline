#!/usr/bin/env python3
"""
Regression checks for jev_shadow.py — NO API calls, runs offline in <1s.

Named *_checks.py on purpose: the disk-cleanup plugin deletes any test_*/tmp_* file
under HERMES_HOME at session end with no age threshold, so a test_*.py file would not
survive. See robots_guard_checks.py for the same pattern.

Run: python3 jev_shadow_checks.py
"""
import os
import sys
import shutil
import tempfile
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("jev_shadow", os.path.join(HERE, "jev_shadow.py"))
js = importlib.util.module_from_spec(spec)
spec.loader.exec_module(js)

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if got == want else 'FAIL'}  {name}")


def _row(**kw):
    """Build a shadow row with stubbed Jev answers (no API)."""
    rec = {
        "title": kw.get("title", "R. v. Test"),
        "court": kw.get("court", "onca"),
        "neutral_citation": kw.get("cit", "2026 ONCA 1"),
        "keywords": kw.get("keywords", ""),
        "description": "",
        "subjects": [],
    }
    answers = {
        "criminal_matter": {"type": "noul", "noul": kw["crim"]},
        "frame_ok": {"type": "noul", "noul": kw["frame"]},
        "relevance": {"type": "choice", "choice": kw.get("choice", "Medium"),
                      "confidence": kw["conf"], "probabilities": {}},
    }
    orig = js.decide
    js.decide = lambda *a, **k: ({"answers": answers, "model": "stub"}, {"ok": True, "http": 200, "error": None, "cost": 0.0})
    try:
        return js.score_one(rec)
    finally:
        js.decide = orig


print("gate truth table (criminal descision = keyword is_criminal)")
# R. v. Test -> looks_criminal_title True -> keyword is_criminal True
check("good frame + agreeing criminal + confident -> OK",
      _row(crim=0.97, frame=0.60, conf=0.80)["jev"]["flag"], "OK")
check("state unjudgeable -> STATE_UNJUDGEABLE (not auto-accepted)",
      _row(crim=0.97, frame=0.18, conf=0.80)["jev"]["flag"], "STATE_UNJUDGEABLE")
check("low pick confidence -> LOW_CONF",
      _row(crim=0.97, frame=0.60, conf=0.30)["jev"]["flag"], "LOW_CONF")
check("keyword fires but Jev disagrees -> CRIM_DISAGREE",
      _row(crim=0.01, frame=0.60, conf=0.95)["jev"]["flag"], "CRIM_DISAGREE")
check("only OK is auto-accepted",
      _row(crim=0.01, frame=0.60, conf=0.95)["jev"]["auto_accept"], False)

print("\nempty state builds without crash")
check("build_state on {} returns str", isinstance(js.build_state({}), str), True)

print("\nspread sample never exceeds the limit")
items = list(range(100))
check("spread len", len(js.spread_sample(items, 12)), 12)
check("spread covers the tail", js.spread_sample(items, 12)[-1] > 90, True)

print("\nsampling draws from the WHOLE pool, not the first max_items")
# Regression: an earlier cap truncated `items` before sampling, so every run only ever
# saw the first 200 records (all federal). Sampling must span the full archive.
_big = [{"title": f"case {i}", "court": "onca", "neutral_citation": f"2026 ONCA {i}",
         "keywords": "", "description": "", "subjects": []} for i in range(1000)]
_orig_load, _orig_score, _orig_dir = js.load_merged, js.score_one, js.SHADOW_DIR
_tmpdir = tempfile.mkdtemp(prefix="jev_shadow_checks_")
js.load_merged = lambda *a, **k: _big
js.score_one = lambda rec, **k: {"title": rec["title"], "citation": rec["neutral_citation"],
                                 "jev": None, "error": None, "cost": 0.0}
js.SHADOW_DIR = _tmpdir   # never let a test overwrite the real shadow output
try:
    _out = js.run(limit=12, max_items=12)
    seen = [r["title"] for r in _out["rows"]]
finally:
    js.load_merged, js.score_one, js.SHADOW_DIR = _orig_load, _orig_score, _orig_dir
    shutil.rmtree(_tmpdir, ignore_errors=True)
check("sample reaches deep into the pool",
      any(int(t.split()[-1]) > 800 for t in seen), True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
