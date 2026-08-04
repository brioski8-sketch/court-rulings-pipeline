#!/bin/bash
# Full Court Rulings Pipeline
# 1. Pull court rulings from CanLII RSS
# 2. Pull Hansard legislative debates
# 3. Generate combined briefing
set -e

DIR="$HOME/.hermes/court-rulings"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] === Court Rulings Pipeline ==="
echo ""

echo "=== Step 1: Pulling court rulings ==="
cd "$DIR" && python3 pull_rulings.py
echo ""

echo "=== Step 2: Pulling Hansard debates ==="
cd "$DIR" && python3 pull_hansard.py
echo ""

echo "=== Step 3: Generating briefing ==="
cd "$DIR" && python3 analyze_rulings.py
echo ""

echo "[$TIMESTAMP] Pipeline complete."
