#!/bin/bash
# Court Rulings Pipeline — canonical entry point (run by the Monday cron).
#
# Steps, in order. Order is load-bearing: pull_scc_text depends on the feed records
# that pull_rulings writes, and analyze_rulings merges everything at the end.
#
#   1. pull_rulings.py     SCC feed  -> data/rulings_*.jsonl      (metadata)
#   2. pull_canlii_api.py  Ontario   -> data/canlii_*.jsonl       (metadata + keywords synopsis)
#   3. pull_federal.py     FCA/FCT   -> data/federal_*.jsonl      (+ full judgment text)
#   4. pull_scc_text.py    SCC       -> data/scc_*.jsonl          (+ full judgment text)
#   5. pull_hansard.py     Ontario legislature -> data/hansard_*.jsonl
#   6. analyze_rulings.py  -> reports/briefing_*.txt
#
# COMPLIANCE: canlii.org and ontariocourts.ca both disallow automated retrieval. Ontario
# data comes from the sanctioned CanLII REST API (metadata + synopsis only, never the
# judgment). Full text comes only from courts whose robots.txt permits it (SCC, FCA, FCT).
# Do not add a step that fetches a ruling's text from a host that forbids it.
#
# Sources are independent: a failure in one is reported and the pipeline continues, so a
# single dead API still produces a briefing from the tiers that worked. Only a failure in
# the final briefing step is fatal.
set -uo pipefail

DIR="$HOME/.hermes/court-rulings"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

# CanLII REST API key (metadata API). Kept outside any repo at ~/.hermes/canlii_api_key.txt
# (chmod 600). Plan limits: 5,000 queries/day, 2 req/s, 1 concurrent request;
# metadata only (no document text, no full-text search).
if [ -f "$HOME/.hermes/canlii_api_key.txt" ]; then
  export CANLII_API_KEY="$(tr -d '\n' < "$HOME/.hermes/canlii_api_key.txt")"
else
  echo "WARNING: CanLII API key file missing — Ontario pull will be skipped."
fi

FAILED=()
step() {
  local label="$1"; shift
  echo "=== $label ==="
  if "$@"; then
    echo ""
    # Politeness gap between stages: each script spaces its own requests, but the
    # handoff between them would otherwise be a back-to-back pair.
    sleep 3
  else
    echo "!! FAILED: $label (continuing; briefing will use the sources that succeeded)"
    echo ""
    FAILED+=("$label")
    sleep 2
  fi
}

echo "[$TIMESTAMP] === Court Rulings Pipeline ==="
echo ""
cd "$DIR" || exit 1

step "Step 1/6: SCC feed + federal list" python3 pull_rulings.py
step "Step 2/6: Ontario rulings (CanLII API)" python3 pull_canlii_api.py --criminal-only
step "Step 3/6: Federal courts (FCA/FCT) + full text" python3 pull_federal.py
step "Step 4/6: SCC full judgment text" python3 pull_scc_text.py
step "Step 5/6: Ontario Hansard debates" python3 pull_hansard.py

echo "=== Step 6/6: Generating briefing ==="
if ! python3 analyze_rulings.py; then
  echo "!! FATAL: briefing generation failed."
  exit 1
fi

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "[$TIMESTAMP] Pipeline completed WITH FAILURES: ${FAILED[*]}"
  exit 2
fi
echo "[$TIMESTAMP] Pipeline complete."
