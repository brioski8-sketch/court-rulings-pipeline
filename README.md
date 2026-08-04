# Court Rulings Pipeline

Automated scanning of Canadian criminal-law court rulings (SCC, ONCA, ONCJ, ONSC,
federal courts) with briefing generation.

- `fetch_rss.sh` — pull rulings from CanLII RSS feeds.
- `analyze_rulings.py` — identify precedent-setting decisions + Hansard context, generate briefing.
- `scripts/court-rulings-pipeline.sh` — orchestration wrapper.

Reports / data are gitignored.
