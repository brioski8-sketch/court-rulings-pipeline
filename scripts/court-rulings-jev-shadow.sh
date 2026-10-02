#!/bin/bash
# Jev shadow-mode run for the court-rulings pipeline (Monday, after the briefing).
#
# SHADOW ONLY: scores the week's new rulings with Jev into court-rulings/shadow/ and
# changes nothing about the briefing. Scores only rulings pulled since its own last
# run (its own watermark), so it never re-scores the whole archive.
#
# DELIVERY: the report is a wide table, which is unreadable as chat text. The markdown
# file is EMAILED as an attachment and Telegram gets ONE summary line. Previously this
# dumped the whole table into Telegram, which the user rejected.
#
# Watchdog pattern: prints nothing when there were no new rulings, so an empty week
# sends no message at all.
set -uo pipefail

DIR="$HOME/.hermes/court-rulings"
cd "$DIR" || exit 1

OUT=$(python3 jev_shadow.py --since-last --limit 0 --max-items 200 2>&1)

if printf '%s' "$OUT" | grep -q "No new rulings"; then
  exit 0
fi

# The report files just written. The EMAIL carries the rendered HTML table; the markdown
# and CSV ride along as attachments for anyone who wants the raw data.
HTML=$(ls -t "$DIR"/shadow/jev_shadow_*.html 2>/dev/null | head -1)
CSV=$(ls -t "$DIR"/shadow/jev_shadow_*.csv 2>/dev/null | head -1)
if [ -z "$HTML" ]; then
  echo "Jev shadow: report ran but no HTML table was produced."
  exit 1
fi

SCORED=$(printf '%s\n' "$OUT" | sed -n 's/^| Rulings scored | \([0-9][0-9]*\).*/\1/p')
AGREE=$(printf '%s\n' "$OUT" | sed -n 's/^| Criminal \/ not agreement | \(.*\) |$/\1/p')
FLAGS=$(printf '%s\n' "$OUT" | sed -n 's/^| \([A-Z_][A-Z_]*\) | \([0-9][0-9]*\) |.*/\1=\2/p' | paste -sd', ' -)

SUBJECT="Jev shadow report - $(date +%F) - court rulings"
BODY="Jev shadow-mode scoring for the week's court rulings.

Scored:             ${SCORED:-?}
Criminal agreement: ${AGREE:-?}
Gate outcomes:      ${FLAGS:-none}

This message has an HTML version with the full table; if you are reading this text-only,
open the attached CSV instead.

Reminder: this is SHADOW MODE. None of it affects the briefing."

EMAIL_OUT=$(~/.hermes/scripts/send_report_email.py \
  --subject "$SUBJECT" \
  --body "$BODY" \
  --html-file "$HTML" \
  --attach "$CSV" 2>&1)
EMAIL_RC=$?

if [ $EMAIL_RC -ne 0 ]; then
  # Surface the failure instead of losing the report silently.
  echo "Jev shadow ran (${SCORED:-?} rulings) but the report email FAILED: ${EMAIL_OUT}"
  exit 1
fi

echo "Jev shadow - court rulings: ${SCORED:-?} rulings, ${AGREE:-?} agreement with the keyword classifier. Table emailed ($(basename "$CSV"))."
exit 0
