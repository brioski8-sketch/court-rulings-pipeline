# CanLII Integration — Implementation Progress

Plan: `~/.hermes/plans/2026-09-17_101500-canlii-api-full-text-integration.md`
Started: 2026-09-17. Ontario-first ranking added by product direction.

## Done and verified

### Task 1 — `robots_guard.py` + `test_robots_guard.py` ✅
Executable terms-of-use boundary. Explicit denylist (canlii.org, canlii.ca,
ontariocourts.ca) *plus* a live robots.txt check; unreachable robots.txt is treated
as DENY, not permission.
- **14/14 tests pass**, including live robots.txt fetches against the real hosts.
- `require_allowed(url)` now wraps the fetch in `pull_rulings.py`.

### Task 2 — `pull_canlii_api.py` ✅
Ontario discovery + per-case metadata via the sanctioned API.
- Uses `caseBrowse/{lang}/{db}/` to list, then `caseBrowse/{lang}/{db}/{caseId}/`
  per case for `keywords`, `topics`, `decisionDate`, `docketNumber`.
- `offset` always sent (mandatory). Sequential only, 1.5s delay, 429 backoff,
  900-call run budget against the 5,000/day plan limit.
- **Verified:** 7-day pull = 117 listed across onca/onsc/onscdc/oncj → 33 criminal
  cases detailed, 31 with keywords. Median keywords length 1,622 chars, max 2,337.
- Output: `data/canlii_YYYY-MM-DD.jsonl` (33 records, written).

### Task 3 — forbidden CanLII RSS path removed ✅ (core files)
`pull_rulings.py`: `CANLII_SCRAPE_OK` flag, `CANLII_FEEDS` dict and the enabling
branch are **deleted**, replaced by an explanatory comment. No one can re-enable
scraping with an env var.
- **Archived 2026-09-17:** all 143 retired one-off scripts moved to
  `legacy/` (move, not delete), including the ones hardcoding canlii.org URLs and
  `check_tools2.py` with its spoofed `Mozilla/5.0` UA. `legacy/README.md` states the
  do-not-run rule. Top level is now exactly the 8 active files.
- `README.md` rewritten — it still documented the dead CanLII RSS feeds and a
  Mon/Wed/Fri schedule that has not been true since the job went weekly.

- **RESOLVED — see the disk-cleanup finding at the end of this file.** `test_robots_guard.py` vanished from disk between the first
  successful test run and the archive step — the `.pyc` remained in `__pycache__`, and it
  was missing *before* the `mv` ran, so the archive did not cause it. Cause not
  determined; a diagnostic command was blocked by the terminal guard. The file has been
  recreated and re-verified (14/14 pass). **If other files go missing, treat it as a
  real problem, not a fluke.**

### Task 6 — `classify.py` (shared classifier + Ontario-first ranking) ✅
Single source of truth so the briefing and podcast cannot drift.
- Ontario-first `rank_key()`: tier 0 Ontario criminal/LE → tier 1 SCC criminal/LE →
  tier 2 rest → tier 3 historical backfill.
- `select_top()` guardrail: reserves one slot for the best SCC/FCA criminal case so
  a landmark ruling isn't buried by a busy Ontario week. Never pads.
- **Recency fix:** `publishedAfter` filters on CanLII *posting* date, so backfilled
  old judgments were leaking into "this week". 6 of 33 records were 2016–2024
  decisions. They are now demoted (labelled), not deleted.
- Verified against real data: top 5 is now all recent 2026 Ontario criminal matters
  (ONCA ×2, ONSC ×2, ONCJ ×1).

## Done (continued)

### Task 4 — `pull_federal.py` ✅
FCA + FCT list and FULL TEXT. Discovery uses the undocumented `?iframe=true` view; the
`nav_date.do` variant returns 25 items with clean
`<span class="title"><a href=...>` markup and a direct `document.do` link per case.
- `document.do` returns a **PDF**; extracted via PyMuPDF.
- **Prefix trap documented in-code:** FCA is `/fca-caf/`, FCT is `/fc-cf/` — `/fct-cf/` 404s.
- Zero-rows is a loud warning, not silent success (the iframe behaviour is undocumented).
- Verified: 2026 FCA 153 → 9,208 chars extracted.

### Task 5 — `pull_scc_text.py` ✅
Attaches full text to SCC rulings from the official JSON feed's item URLs.
- Verified: **2026 SCC 30 (`R. v. R.B.-C.`, decided 2026-09-11) → 76,555 chars / 11,633 words**
  of genuine judgment text.
- Shares `extract_pdf_text` with pull_federal — not duplicated.
- Dry-run semantics fixed to match pull_federal (dry-run suppresses writing, not fetching).

### Task 7 (part) — loader wired ✅
`analyze_rulings.py` now loads every tier, richest first
(`federal_` → `scc_` → `canlii_` → `rulings_`) and merges by case fingerprint so a
full-text record is never replaced by the bare feed record.
- Verified end-to-end: 65 unique rulings merged (33 CanLII Ontario + 32 SCC feed + 1 SCC text).
- Briefing now leads with Ontario: **9 of the top 10 are Ontario**, and the reserved slot
  pulled in the SCC full-text case. Ontario entries render as case briefs with charge,
  statute + section, authorities applied, and disposition.
- The duplicate classifier in `analyze_rulings.py` is retired (renamed `_legacy_...`);
  `classify.classify_ruling` is confirmed bound at module level.

### Fixes found only by running it
- **Recency window 120 → 45 days.** With 120, a June decision that CanLII backfilled
  appeared in a weekly brief. 38 records are now demoted as backfill.
- **RFC-2822 date parsing added.** RSS-era records use `Mon, 01 Jun 2026 04:00:00 GMT`,
  which previously failed to parse and was silently treated as *recent*.

### Correction to my own earlier verification
A check I ran claimed old June/July cases were polluting the top 10. That was **my
throwaway script** globbing every `rulings_*` file and ignoring the cutoff — not the
pipeline, which filters by filename date correctly. Re-verified using the pipeline's own
loader before changing anything.

### Task 11 (added at product direction) — Jarvis-lite court page ✅ data, ⬜ UI

The dashboard endpoint globbed `rulings_*` only and parsed RFC dates only — so it would have
shown **zero Ontario records** and blank dates. Fixed and verified live:

- `COURT_SOURCE_PREFIXES` = `federal_ → scc_ → canlii_ → rulings_`, dedupe by case fingerprint.
- `_court_parse_date` handles ISO **and** RFC-2822 (both formats exist in the data dir).
- `_court_summary` = first issue block, capped at 240 chars (keywords are 1,400-2,000 chars).
- SCC feed boilerplate (`"<subjects> - New document published on <date>"`) stripped wherever it
  appears, not just when leading — it was leaking into summaries as
  "Constitutional law - New document published on 2026-09-11".
- **Verified over HTTP:** 1,596 rulings, **1,026 Ontario**, 0 blank dates, 0 boilerplate rows.
- **Still to do (UI):** tier badge per row (`full text` vs `CanLII brief`) so a metadata row never
  looks like a read judgment, and a detail view for the full `keywords` field.

### Task 7 (rest) — briefing honesty + two-section layout ✅
- **Coverage statement** at the top of every briefing: how many full texts were retrieved vs
  synopsis-only, and an explicit line that Ontario is synopsis-not-judgment and why.
- **Per-entry tier label**: `FULL JUDGMENT TEXT` / `SYNOPSIS ONLY — judgment not read`. A reader
  never has to guess which one they're looking at.
- **Two sections.** Ontario-first ordering alone buried all 25 full-text judgments below 33
  Ontario synopses — the briefing *retrieved* 25 real judgments and *displayed* none. Now:
  `ONTARIO RULINGS (synopsis)` then `FULL JUDGMENT TEXT RETRIEVED (SCC/Federal Courts)`.
- **Court tallies count in-window decisions only.** The SCC feed file holds the whole year's
  corpus, so the header claimed "Supreme Court of Canada: 32" in a week with one SCC ruling.
  Now 1.
- **`keywords` blocks split on `|`.** They were newline-split, so a 1,800-char blob printed as a
  single unreadable line. Capped at 320 chars per block, max 3, with a (+N further issues) note.

### Task 8 — podcast ✅
- **The podcast only read `rulings_*`**, so Ontario never reached it. Now loads all four tiers.
- **It scored on `description`, which is empty for Ontario** (their content is in `keywords`) —
  every Ontario case scored zero and could never make the top 5. Now scores on whichever field
  holds substance.
- **Recency term added to the sort.** Two of five picks were backfill (2023 and 2020) because the
  archive outranked the news on keyword count. Now in-window → Ontario → relevance.
- Output carries `coverage_tier`, `neutral_citation`, `keywords`, and a `narration_note` telling
  the script what it may assert: full text = reasoning may be described; synopsis = describe the
  charge/statute/issue/disposition and do NOT characterise the court's reasoning.
- Verified: 5 picks, all current Ontario (Sep 4-10). Ledger restored after testing.

### Task 9 — end-to-end run ✅
Full pipeline, all six steps, **exit 0**. 91 unique rulings merged. 25 full judgments on disk
(91,844 words across 26 text files). Monday's cron runs this exact script, so this was mandatory,
not optional.

### Task 10 — pipeline + cron ✅
- `~/.hermes/scripts/court-rulings-pipeline.sh` rewritten: six steps, `CANLII_API_KEY` export,
  per-step error isolation (exit 2 = partial, exit 1 = briefing failed), politeness gaps.
- `run_pipeline.sh` in the data dir now **delegates** to it — the two had already drifted (the
  local copy was missing the Ontario, federal and SCC-text steps entirely).
- Cron `474777f209e8` already invokes the canonical script; no re-point needed.

### Bug found during the dry run
A single **HTTP 404** on one federal `document.do` URL killed the entire federal pull —
`fetch()` re-raised non-429 HTTP errors. Some FCT items have no document (reasons unpublished).
404 is now "missing document, move on", and per-case extraction is isolated so one bad PDF can't
cost the other 24.

### Finding — the disk-cleanup plugin was deleting the test file
`test_robots_guard.py` vanished **four times** (twice before I found the cause, twice after —
the plugin's in-memory exemption list outlived my patch to it). Cause: the `disk-cleanup` plugin classifies any
`test_*`/`tmp_*` file under `HERMES_HOME` as disposable and deletes that category at **every
session end, with no age threshold**; only a hardcoded top-level dir list is exempt, and
`court-rulings` was not on it. Confirmed in `~/.hermes/disk-cleanup/cleanup.log`
(`TRACKED ... (test, 2.5 KB)` → `DELETED ...`). Patched `_NEVER_TRACK_TOP_LEVEL` to add the real
project trees; verified project test files are now protected while `tmp/`, `cache/`, `pastes/`
still clean normally. **A `hermes update` reverts this** — documented in the `hermes-maintenance`
skill.

Because the running process kept deleting the file, the durable fix was to **rename it out of the
classifier's reach**: `robots_guard_checks.py` (16 checks, all pass). Pytest runs an explicitly
named file fine without the `test_` prefix, and the name survives plugin updates. Do not rename it
back.

### Task 11 (UI) — dashboard tier badge + synopsis detail ✅
- `STATIC` only (`cron_sections.js`, `style.css`) — no server change needed for this step.
- New **Coverage** column: green `full text` / grey `CanLII brief` badge per row, each with a
  tooltip saying what may and may not be assumed.
- **Click a row to expand** the full CanLII synopsis — split on `|` and newline into issue
  blocks. Verified expanding to 4 blocks.
- Explicit hex colours (not opacity) to match the existing arxiv contrast fixes.
- **Verified in a live browser on the LAN:** 1,622 rulings, 1,026 Ontario, 0 blank dates;
  SCC filter shows exactly 1 `full text` row and 35 `CanLII brief`; expand works.

## All tasks complete. Nothing remaining.

## Late fixes found while verifying the delivery path

**The Monday cron prompt was pointing at a retired script.** Job `474777f209e8` runs
`court-rulings-pipeline.sh` as its pre-step *and* then hands a prompt to an agent — and that
prompt still said "run `bash pull_rulings.sh` … this fetches latest rulings from **CanLII RSS**",
a file that now lives in `legacy/`. Monday's agent would have hit a missing script and improvised.
Rewritten: verify the pre-step ran, read all four tiers, honour `coverage_tier`, Ontario-first,
write the enriched `.md` **without** overwriting the pipeline's `.txt` (which carries the coverage
statement), copy to Drive, deliver with a coverage note. Podcast prompt had one matching stale
line (RSS) — also fixed. Backups: `cron/jobs.json.bak.courtprompt-*`.

**The checks file was renamed out of the cleaner's reach.** A plugin patch alone did **not** stop
the deletion: the plugin module is imported once into the running process, so the in-memory
exemption list outlived my edit and the file was deleted twice more (15:53 was the fourth
deletion). A fresh subprocess reported the path as protected while the live process kept deleting
it. Durable fix: `robots_guard_checks.py` — the classifier matches on filename (`test_*`, `tmp_*`,
`*.test.*`), so a `*_checks.py` name is never categorised, needs no restart, and survives a
`hermes update`. **Do not rename it back.** The plugin patch stays too, for files whose names
can't change (e.g. the existing `final_games_tracker/tests/` suite).

**A stale reference was actively dangerous.** `canadian-case-law-research/references/feed-source-guide.md`
was still headed "Confirmed Working Feeds (CanLII RSS — no auth, no bot block)" — guidance that
directly contradicts the retraction and could lead the next session into a terms violation. Now
carries a retraction banner; the historical content is kept below it as a record. Also fixed the
same skill's `web-research-sources.md` tier listing and three operational lines in its SKILL.md
(`pull_rulings.py # CanLII RSS + SCC JSON`, `run_pipeline.sh` description, and a `rulings_*.jsonl`
glob that read only one of the four tiers).

### Final state
- 10 pipeline files, all compile; **16/16 compliance checks pass**
- Briefing regenerates: 11 entries (6 Ontario synopsis + 5 full-text)
- Dashboard: HTTP 200, 1,622 rulings, 1,026 Ontario, 25 full-text, 0 blank dates
- Both court cron prompts free of stale references
- Monday marker held at `2026-09-14`; podcast ledger held at 47 entries

## Notes for the next session

**All three of the original to-dos below are DONE** — kept only so the history is readable.

- ~~`analyze_rulings.py` still loads only the `rulings_` prefix.~~ **DONE.** It loads all four
  tiers richest-first and merges by fingerprint.
- ~~`generate_court_podcast_data.py` has its own duplicate classifier and globs `rulings_` only.~~
  **Partly done.** It loads all four tiers and scores on `keywords` when `description` is empty,
  and it has recency + Ontario-first ordering. It still keeps a local `classify_ruling` rather
  than importing `classify.py` — a deliberate call, because its output shape feeds the ledger and
  swapping the classifier wholesale would have changed ledger keys. **If you touch the ranking
  rules, change them in both places or retire the copy properly.**
- ~~Do not run the Monday cron until Task 9 passes.~~ **Task 9 passed**; the pipeline runs exit 0
  and the cron now invokes it. Both court cron prompts were rewritten on 2026-09-17.

Verified API facts worth keeping: no `caseMetadata` endpoint exists; the citator is
`/v1/caseCitator/en/{db}/{caseId}/{citingCases|citedCases|citedLegislations}`; the CanLII key
lives at `~/.hermes/canlii_api_key.txt` (mode 600).

Operational facts:
- The pipeline is `~/.hermes/scripts/court-rulings-pipeline.sh` (six steps, exit 2 = partial).
  `run_pipeline.sh` here delegates to it. `pull_rulings.sh` is retired (in `legacy/`).
- `robots_guard_checks.py` is the compliance suite (16 checks). It is deliberately **not** named
  `test_*.py` — see the disk-cleanup note above. Do not rename it back.
- The dashboard reads this dataset at `COURT_SOURCE_PREFIXES` in
  `/opt/jarvis-lite/server/cron_sections.py`. A new tier needs adding there or the dashboard
  silently ignores it.
