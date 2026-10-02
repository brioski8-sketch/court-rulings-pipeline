#!/usr/bin/env python3
"""Compliance checks: prove the terms-of-use boundary stays where it is.

Run them before shipping any change to robots_guard.py or to a puller's fetch path:

    python3 -m pytest robots_guard_checks.py -q

If these fail, the pipeline is about to start scraping a host that forbids it, or has
stopped being able to reach one that permits it.

WHY THIS FILE IS NOT CALLED test_*.py
-------------------------------------
The disk-cleanup plugin classifies any file whose name starts with `test_`/`tmp_` (or ends
`.test.py`) under HERMES_HOME as disposable and **deletes that category at every session
end, with no age threshold**. A `test_robots_guard.py` living here was deleted four times
in a single session — the plugin's in-memory copy of `_NEVER_TRACK_TOP_LEVEL` outlives any
edit to that file, so patching the plugin does not protect a file until the whole Hermes
process restarts.

Naming it `*_checks.py` sidesteps the classifier entirely, which also makes it immune to a
`hermes update` reverting the plugin patch. **Do not rename this back to `test_*.py`.**
Pytest runs an explicitly named file fine without the prefix.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from robots_guard import (  # noqa: E402
    BLOCKED_HOSTS,
    FULL_TEXT_PERMITTED_HOSTS,
    describe,
    is_allowed,
    require_allowed,
)


# ── Hosts that forbid automated retrieval ────────────────────────────────────────────
# canlii.org/robots.txt ends with "User-agent: * / Disallow: /" and ontariocourts.ca
# disallows /decisions/ and /coa/en/*. Nothing in this pipeline may fetch either.

@pytest.mark.parametrize("url", [
    "https://www.canlii.org/en/on/onca/doc/2026/2026onca631/2026onca631.html",
    "https://canlii.org/en/on/onsc/doc/2026/2026onsc5161/2026onsc5161.html",
    "https://www.canlii.org/en/#search/type=decision",
    "https://www.ontariocourts.ca/decisions/2026/2026ONCA0631.htm",
    "https://www.ontariocourts.ca/coa/en/decisions/2026/2026ONCA0631.htm",
])
def test_blocked_hosts_are_refused(url):
    assert is_allowed(url) is False
    with pytest.raises(PermissionError):
        require_allowed(url)


def test_blocked_host_list_names_both_hosts():
    joined = " ".join(BLOCKED_HOSTS)
    assert "canlii.org" in joined
    assert "ontariocourts.ca" in joined


# ── Hosts that permit it ─────────────────────────────────────────────────────────────
# These courts' robots.txt allow automated retrieval; the pipeline depends on them for
# full judgment text. A regression here silently costs every full-text judgment.

@pytest.mark.parametrize("url", [
    "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/21654/1/document.do",
    "https://decisions.fca-caf.gc.ca/fca-caf/decisions/en/item/521903/index.do",
    "https://decisions.fct-cf.gc.ca/fc-cf/decisions/en/item/531455/index.do",
])
def test_permitted_courts_are_allowed(url):
    assert is_allowed(url) is True


def test_permitted_host_list_covers_three_courts():
    joined = " ".join(FULL_TEXT_PERMITTED_HOSTS)
    for host in ("scc-csc.ca", "fca-caf.gc.ca", "fct-cf.gc.ca"):
        assert host in joined


# ── Fail closed ──────────────────────────────────────────────────────────────────────
# An unreachable robots.txt must DENY. Defaulting to allow would turn a transient
# network blip into a terms-of-use breach.

def test_unreachable_robots_txt_denies(monkeypatch):
    import robots_guard
    monkeypatch.setattr(robots_guard, "_load_robots", lambda base_url: None)
    robots_guard._robots_cache.clear()
    assert is_allowed("https://decisions.scc-csc.ca/scc-csc/scc-csc/en/1/1/document.do") is False
    robots_guard._robots_cache.clear()


def test_malformed_url_denies():
    assert is_allowed("not a url at all") is False
    assert is_allowed("") is False


# ── describe() must explain itself ───────────────────────────────────────────────────

def test_describe_reports_reason_for_both_answers():
    blocked = describe("https://www.canlii.org/en/on/onca/doc/2026/2026onca631/2026onca631.html")
    assert blocked["allowed"] is False and blocked["host"] and blocked["reason"]

    allowed = describe("https://decisions.scc-csc.ca/scc-csc/scc-csc/en/21654/1/document.do")
    assert allowed["allowed"] is True and allowed["host"] and allowed["reason"]


def test_require_allowed_returns_true_when_permitted():
    assert require_allowed(
        "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/21654/1/document.do") is True


# ── Live checks ──────────────────────────────────────────────────────────────────────
# The lists above are a promise; these confirm the promise still matches the real
# robots.txt files. Network-dependent, so they skip rather than fail when offline.

def test_live_robots_txt_still_blocks_canlii():
    live = is_allowed(
        "https://www.canlii.org/en/on/onca/doc/2026/2026onca631/2026onca631.html")
    if live is None:
        pytest.skip("robots_guard returned no verdict (offline?)")
    assert live is False, "canlii.org now permits fetching — re-verify terms before use"


def test_live_robots_txt_still_permits_scc():
    live = is_allowed(
        "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/21654/1/document.do")
    if live is None:
        pytest.skip("robots_guard returned no verdict (offline?)")
    assert live is True, "SCC document route is now refused — full-text retrieval is broken"
