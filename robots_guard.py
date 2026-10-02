"""Refuse to fetch any URL whose host disallows it in robots.txt.

Why this module exists
----------------------
Two hosts carry Ontario judgment text — canlii.org and ontariocourts.ca — and both
blanket-disallow automated access:

    canlii.org      User-agent: *  /  Disallow: /
    ontariocourts.ca  Disallow: /decisions/, /decisions/*, /coa/en/*, /rss/*

That is a hard boundary, not a speed bump. A single agent-driven fetch is still an
automated client; "it was only one page" is not a defence, and CanLII actively
enforces (this pipeline already receives DataDome blocks on canlii.org full text).

The sanctioned route to CanLII data is the REST API at api.canlii.org, which is
metadata-only BY DESIGN. The approved key covers caseBrowse + caseCitator only.
CanLII have stated that requests for direct content access "won't be granted".

Do not "fix" this module to allow those hosts. If you believe a host is wrongly
blocked, check its robots.txt yourself and update the test in
test_robots_guard.py with the evidence.
"""

import functools
import urllib.parse
import urllib.request
import urllib.robotparser

# Identify honestly, with a contact address. Never spoof a browser User-Agent:
# misrepresentation is both dishonest and what turns a rate-limit into an IP block.
USER_AGENT = "HermesCourtBriefing/1.0 (mailto:agentvi@agentmail.to)"

# Hosts we are permitted to fetch full text from. Recorded here so the intent is
# explicit and reviewable, rather than implicit in the calling code.
FULL_TEXT_PERMITTED_HOSTS = frozenset({
    "decisions.scc-csc.ca",     # Supreme Court of Canada
    "decisions.fca-caf.gc.ca",  # Federal Court of Appeal
    "decisions.fct-cf.gc.ca",   # Federal Court  (note: path prefix is /fc-cf/)
})

# Hosts carrying Ontario judgment text that forbid automated access. Kept as an
# explicit denylist as well as a robots check, so a robots.txt outage (which we
# treat as "deny" anyway) can never quietly open the door.
BLOCKED_HOSTS = frozenset({
    "canlii.org",
    "www.canlii.org",
    "canlii.ca",
    "ontariocourts.ca",
    "www.ontariocourts.ca",
})

_robots_cache = {}


def _load_robots(base_url):
    """Fetch and parse a host's robots.txt. Unreachable robots.txt is NOT permission."""
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(base_url + "/robots.txt")
    try:
        req = urllib.request.Request(base_url + "/robots.txt", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
        rp.parse(body.splitlines())
        return rp
    except Exception:
        # Default to deny. Silence is not consent, and a failed robots.txt fetch
        # must never be read as "no rules found, therefore allowed".
        return None


def _robots_for(base_url):
    if base_url not in _robots_cache:
        _robots_cache[base_url] = _load_robots(base_url)
    return _robots_cache[base_url]


def is_allowed(url, *, user_agent=USER_AGENT):
    """Return True only if this URL is safe and permitted to fetch programmatically."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return False

    # Explicit denylist first — never negotiable, even if robots.txt is unreachable.
    if host in BLOCKED_HOSTS:
        return False

    base = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_for(base)
    if rp is None:
        return False
    return bool(rp.can_fetch(user_agent, url))


def require_allowed(url):
    """Raise if the URL may not be fetched. Use at every fetch site."""
    if not is_allowed(url):
        raise PermissionError(
            f"robots.txt / denylist forbids automated access to: {url}\n"
            "This is a terms-of-use boundary, not a bug. See robots_guard.py docstring."
        )
    return True


@functools.lru_cache(maxsize=64)
def describe(url):
    """Human-readable verdict, for logging and the briefing's coverage note."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    allowed = is_allowed(url)
    if host in BLOCKED_HOSTS:
        reason = "host on denylist (blanket robots.txt disallow)"
    elif allowed:
        reason = "permitted by robots.txt"
    else:
        reason = "disallowed by robots.txt"
    return {"url": url, "host": host, "allowed": allowed, "reason": reason}


if __name__ == "__main__":
    import sys

    for u in sys.argv[1:]:
        d = describe(u)
        print(f"{'ALLOW' if d['allowed'] else 'BLOCK'}  {d['host']:<28} {d['reason']}")
