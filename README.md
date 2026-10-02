# Canadian & Ontario Court Rulings Pipeline + Legislative Debates

## Compliance first — read this before adding a data source

`canlii.org/robots.txt` ends with `User-agent: * / Disallow: /`. **Automated access to the
CanLII website is forbidden.** `ontariocourts.ca` likewise disallows `/decisions/`, `/coa/en/*`
and `/rss/*`. Judgments on those hosts may not be fetched by this pipeline — not with curl,
not with a browser, not via a third-party scraper service.

Every fetch goes through `robots_guard.py`, which enforces this with an explicit denylist **plus**
a live robots.txt check (an unreachable robots.txt is treated as DENY, not permission).
`robots_guard_checks.py` asserts the boundary holds — 16 checks, run them before touching any
fetcher: `python3 -m pytest robots_guard_checks.py -q`

**Why that file is not named `test_*.py`.** The disk-cleanup plugin treats any `test_*`/`tmp_*`
file under `HERMES_HOME` as disposable and deletes that category at **every session end, with no
age threshold**. A copy named `test_robots_guard.py` was deleted four times in one session: the
plugin's in-memory exemption list outlives any edit to it, so patching the plugin does not protect
a file until the whole Hermes process restarts. Naming it `*_checks.py` sidesteps the classifier
and survives plugin updates. **Do not rename it back.**

**The sanctioned route to CanLII data is the REST API** (`api.canlii.org/v1`, key at
`~/.hermes/canlii_api_key.txt`, mode 600). It is **metadata only by design** — no judgment text,
no full-text search. CanLII have stated that content-access requests will not be granted.

## Data Sources

### Ontario — CanLII REST API (sanctioned, metadata only)
`pull_canlii_api.py`. Courts: `onca`, `onsc`, `onscdc`, `oncj`.

| Endpoint | Returns |
|---|---|
| `/v1/caseBrowse/en/{db}/?offset=N&publishedAfter=DATE` | case list (title, citation, URL). `offset` is MANDATORY |
| `/v1/caseBrowse/en/{db}/{caseId}/` | per-case `keywords`, `topics`, `decisionDate`, `docketNumber` |
| `/v1/caseCitator/en/{db}/{caseId}/{citingCases\|citedCases\|citedLegislations}` | citation network |

The per-case `keywords` field is the Ontario product: ~1,400–2,000 characters giving, per issue,
the charge, the statute and section, the legal question, the authorities applied, and the
disposition. **It is not the judgment** — no reasoning, no facts, no quotable passages.

Plan limits: 5,000 queries/day, 2 req/s, 1 concurrent. The client is strictly sequential with a
1.5s delay, 429 backoff, and a hard per-run call ceiling. There is no `caseMetadata` endpoint.

### Supreme Court of Canada (full text available)
- List: `https://decisions.scc-csc.ca/scc-csc/scc-csc/en/json/rss.do` (whole corpus — filter by date)
- Full text: `/scc-csc/scc-csc/en/{item}/1/document.do` returns a **PDF**; extract with `fitz`/pymupdf

### Federal Court of Appeal & Federal Court (full text available)
Both are Lexum/Norma SPAs whose plain URLs return an empty JS shell. Appending **`?iframe=true`**
returns server-rendered HTML with real item links:
- FCA: `https://decisions.fca-caf.gc.ca/fca-caf/en/nav.do?iframe=true`
- FCT: `https://decisions.fct-cf.gc.ca/fc-cf/en/nav.do?iframe=true` — **prefix is `/fc-cf/`, not `/fct-cf/`**

Use **`nav_date.do?iframe=true`** instead of `nav.do` — it returns 25 dated items with clean
`<span class="title"><a href=...>` markup and a direct `document.do` link per case
(`nav.do` gives only 5 undated ones). Full text: `{prefix}/decisions/en/{item}/1/document.do`
returns a **PDF**. Some items 404 there (reasons not published yet) — that is a missing
document, not a failure.

### Ontario Legislative Debates (Hansard)
| Source | Access |
|---|---|
| House Documents page | HTML — transcript dates for current session (44-1) |
| Individual transcripts | HTML — full debate text |
| Keyword Search API | POST `https://aihansardsearch-apim.azure-api.net/api/search/keyword` (no auth) |

## Pipeline

1. `pull_rulings.py` — SCC JSON feed (robots-guarded)
2. `pull_canlii_api.py` — Ontario discovery + per-case metadata (`--criminal-only`)
3. `pull_federal.py` — FCA/FCT list **+ full judgment text**
4. `pull_scc_text.py` — SCC full judgment text (depends on step 1's feed records)
5. `pull_hansard.py` — current session transcripts + historical keyword-API matches
6. `analyze_rulings.py` — merges every tier, generates the briefing

Run it with **`~/.hermes/scripts/court-rulings-pipeline.sh`** (the canonical copy — it exports
`CANLII_API_KEY` and drives all six steps). `run_pipeline.sh` in this directory is a thin
delegator to it; keeping two implementations caused drift once already.

Sources are independent: a failed step is reported and the run continues, so one dead API still
produces a briefing from the tiers that worked. Exit 2 = completed with failures, exit 1 = the
briefing itself failed.

## Ranking

Ontario first (product direction, 2026-09-17). See `classify.py`:
tier 0 Ontario criminal/LE → tier 1 SCC criminal/LE → tier 2 everything else → tier 3 historical
backfill. One podcast slot is reserved for the best SCC/FCA criminal case so a landmark ruling is
never buried by a busy Ontario week. Ontario-first is a priority, never a quota — the selection
is padded only with the next-best cases overall.

**Recency caveat:** `publishedAfter` filters on when CanLII *posted* a case, not when it was
*decided*. CanLII backfills old judgments constantly — a weekly pull legitimately contains 2020
and 2023 decisions. Anything older than `RECENT_DECISION_DAYS` (**45**) is demoted and ranked
last rather than reported as new. Both the briefing and the podcast enforce this independently;
if they ever disagree, `classify.py` is the one that is right.

**Briefing shape:** two sections, deliberately. *Ontario rulings* (synopsis) then *Full judgment
text retrieved* (SCC/FCA/FCT). Ontario-first ordering alone buried all 25 full-text judgments
beneath 33 Ontario synopses, so the briefing retrieved real judgments and displayed none.
Every entry is labelled `FULL JUDGMENT TEXT` or `SYNOPSIS ONLY — judgment not read`, and the
header carries a coverage statement of what was and was not seen.

## Output
- `data/rulings_YYYY-MM-DD.jsonl` — SCC feed records (metadata)
- `data/canlii_YYYY-MM-DD.jsonl` — Ontario records (+ `keywords` synopsis)
- `data/federal_YYYY-MM-DD.jsonl` — FCA/FCT records (+ full text)
- `data/scc_YYYY-MM-DD.jsonl` — SCC records (+ full text)
- `data/hansard_YYYY-MM-DD.jsonl` — Hansard entries
- `text/{citation}.txt` — extracted judgment text, one file per case
- `reports/briefing_YYYY-MM-DD.txt` — combined briefing

**Consumers of this dataset** (do not change the file layout or field names without updating them):
- `~/.hermes/scripts/generate_court_podcast_data.py` — podcast episode data
- `/opt/jarvis-lite/server/cron_sections.py` → `/api/cron/court_rulings` — the dashboard's
  Court Rulings page. It reads **all four** `data/` prefixes and merges them; a new prefix must
  be added to `COURT_SOURCE_PREFIXES` there or the dashboard silently ignores that tier.

## Cron
- Court Rulings Briefing — **Mon 07:30 ET** (job `474777f209e8`)
- Court Rulings Podcast — **Mon 08:15 ET** (job `eb759681800b`)
- Jev Shadow — Court Rulings — **Mon 08:30 ET** (`court-rulings-jev-shadow.sh`)

## Files
- `pull_rulings.py` — SCC feed fetcher
- `pull_canlii_api.py` — Ontario CanLII REST API client (discovery + per-case metadata)
- `pull_hansard.py` — Ontario Hansard fetcher
- `analyze_rulings.py` — briefing generator
- `classify.py` — **single source of truth** for classification + Ontario-first ranking
- `jev_shadow.py` / `jev_shadow_checks.py` — shadow scoring pass over the week's rulings,
  run after the briefing so its output can be diffed against the live ranking without
  affecting it. Writes to `shadow/`, which is data and is not versioned.
- `robots_guard.py` / `robots_guard_checks.py` — the terms-of-use boundary and its checks
- `pull_federal.py` — FCA/FCT fetcher + full-text extraction
- `pull_scc_text.py` — SCC full-text extraction
- `run_pipeline.sh` — delegator to `~/.hermes/scripts/court-rulings-pipeline.sh`

**Not published in this repo:** `legacy/` (retired one-off scripts — several hardcode
forbidden `canlii.org` URLs and at least one sends a spoofed browser User-Agent, so they
are deliberately kept out of version control and must not be run), plus `data/`,
`reports/`, `text/`, `shadow/` and the one-off case research notes.
