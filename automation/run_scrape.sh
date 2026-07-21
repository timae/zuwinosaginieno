#!/usr/bin/env bash
#
# Wrapper that runs the Vivino scraper and (optionally) loads the result into
# Postgres. Used by both the macOS launchd job and the Linux systemd/cron setup.
#
# Display: this scraper MUST run headed (Vivino blocks headless).
#   * macOS  — launchd user-agent runs in your GUI session, so a window opens.
#   * Linux  — call this under a virtual display, e.g.  xvfb-run -a run_scrape.sh
#
# Config via environment (all optional):
#   VIVINO_COUNTRIES   default: ch,fr,it,es,us,de,pt,at
#   VIVINO_TYPES       default: 1,2,3,4
#   VIVINO_MAX_PAGES   default: 25
#   VIVINO_MIN_DELAY   default: 3
#   VIVINO_MAX_DELAY   default: 8
#   DATABASE_URL       if set, the JSONL is loaded into Postgres after scraping
#
set -euo pipefail

# Repo root = parent of this script's directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "No venv at $PY — run: python -m venv .venv && pip install -r requirements.txt && playwright install chromium" >&2; exit 1; }

# Load secrets/overrides from an optional .env (never committed).
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

DATE="$(date +%F)"
mkdir -p "$ROOT/data" "$ROOT/logs"
OUT="$ROOT/data/wines-$DATE.jsonl"
LOG="$ROOT/logs/scrape-$DATE.log"

COUNTRIES="${VIVINO_COUNTRIES:-ch,fr,it,es,us,de,pt,at}"
TYPES="${VIVINO_TYPES:-1,2,3,4}"
MAX_PAGES="${VIVINO_MAX_PAGES:-25}"
MIN_DELAY="${VIVINO_MIN_DELAY:-3}"
MAX_DELAY="${VIVINO_MAX_DELAY:-8}"

{
  echo "=========================================================="
  echo "$(date '+%F %T')  START  countries=$COUNTRIES types=$TYPES pages=$MAX_PAGES"

  "$PY" vivino_scraper.py \
    --countries "$COUNTRIES" --types "$TYPES" --max-pages "$MAX_PAGES" \
    --min-delay "$MIN_DELAY" --max-delay "$MAX_DELAY" --out "$OUT"

  if [[ -n "${DATABASE_URL:-}" ]]; then
    echo "$(date '+%F %T')  LOADING into Postgres"
    "$PY" load_to_postgres.py "$OUT"
  else
    echo "$(date '+%F %T')  DATABASE_URL not set — skipping DB load (JSONL: $OUT)"
  fi

  echo "$(date '+%F %T')  DONE"
} >> "$LOG" 2>&1
