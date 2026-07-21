#!/usr/bin/env bash
#
# Wrapper that runs the Vivino scraper, (optionally) loads the result into
# Postgres, and sends a detailed Telegram notification on every run.
# Used by both the macOS launchd job and the Linux systemd/cron setup.
#
# Display: this scraper MUST run headed (Vivino blocks headless).
#   * macOS  — launchd user-agent runs in your GUI session, so a window opens.
#   * Linux  — call this under a virtual display, e.g.  xvfb-run -a run_scrape.sh
#
# Config via environment or a git-ignored .env at the repo root (all optional):
#   VIVINO_COUNTRIES     default: ch,fr,it,es,us,de,pt,at
#   VIVINO_TYPES         default: 1,2,3,4
#   VIVINO_MAX_PAGES     default: 25
#   VIVINO_MIN_DELAY     default: 3
#   VIVINO_MAX_DELAY     default: 8
#   DATABASE_URL         if set, the JSONL is loaded into Postgres after scraping
#   TELEGRAM_BOT_TOKEN   if set (with chat id), a Telegram message is sent per run
#   TELEGRAM_CHAT_ID     your chat id
#
set -uo pipefail   # NOTE: no -e — we handle failures so we can still notify.

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
RUN_LOG="$(mktemp -t vivino_run.XXXXXX)"     # just this run's output, for the summary
trap 'rm -f "$RUN_LOG"' EXIT

COUNTRIES="${VIVINO_COUNTRIES:-ch,fr,it,es,us,de,pt,at}"
TYPES="${VIVINO_TYPES:-1,2,3,4}"
MAX_PAGES="${VIVINO_MAX_PAGES:-25}"
MIN_DELAY="${VIVINO_MIN_DELAY:-3}"
MAX_DELAY="${VIVINO_MAX_DELAY:-8}"

HOST="$(hostname -s 2>/dev/null || hostname)"

# --- Telegram helper -------------------------------------------------------- #
notify() {
    local text="$1"
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "$(date '+%F %T')  Telegram not configured — skipping notification"
        return 0
    fi
    # Telegram caps messages at 4096 chars; keep well under.
    text="${text:0:3900}"
    curl -sS --max-time 20 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "disable_web_page_preview=true" \
        >/dev/null \
        && echo "$(date '+%F %T')  Telegram notification sent" \
        || echo "$(date '+%F %T')  Telegram notification failed"
}

# Per-segment breakdown parsed from this run's scraper log (portable via python).
segment_summary() {
    "$PY" - "$RUN_LOG" <<'PYEOF'
import re, sys
seg, order, counts = None, [], {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.search(r"=== (.+?) / (.+?) ===", line)
    if m:
        seg = f"{m.group(1)}/{m.group(2)}"
        if seg not in counts:
            counts[seg] = 0; order.append(seg)
        continue
    n = re.search(r"\+(\d+) wines", line)
    if n and seg:
        counts[seg] += int(n.group(1))
print("; ".join(f"{s} {counts[s]}" for s in order) or "(no segments)")
PYEOF
}
# ---------------------------------------------------------------------------- #

START_TS=$(date +%s)
{
    echo "=========================================================="
    echo "$(date '+%F %T')  START  host=$HOST countries=$COUNTRIES types=$TYPES pages=$MAX_PAGES"

    "$PY" vivino_scraper.py \
        --countries "$COUNTRIES" --types "$TYPES" --max-pages "$MAX_PAGES" \
        --min-delay "$MIN_DELAY" --max-delay "$MAX_DELAY" --out "$OUT"
    SCRAPE_RC=$?

    # Cross-session dedup: merge into the master ledger, reporting new vs known.
    if [[ "$SCRAPE_RC" == "0" && -s "$OUT" ]]; then
        echo "$(date '+%F %T')  MERGING into master ledger"
        "$PY" merge_master.py "$OUT"
    fi

    DB_STATUS="skipped (no DATABASE_URL)"
    if [[ -n "${DATABASE_URL:-}" ]]; then
        if "$PY" load_to_postgres.py "$OUT"; then
            DB_STATUS="loaded"
        else
            DB_STATUS="LOAD FAILED"
        fi
    fi

    echo "$(date '+%F %T')  DONE (scrape rc=$SCRAPE_RC, db=$DB_STATUS)"
} 2>&1 | tee -a "$RUN_LOG" >> "$LOG"

# tee/pipe swallows the scraper rc; recover it (SCRAPE_RC lived in the subshell).
SCRAPE_RC=$(grep -oE 'scrape rc=[0-9]+' "$RUN_LOG" | tail -1 | grep -oE '[0-9]+' || echo 1)
DB_STATUS=$(grep -oE 'db=[^)]+' "$RUN_LOG" | tail -1 | sed 's/^db=//' || echo "unknown")

WINES=0; [[ -f "$OUT" ]] && WINES=$(wc -l < "$OUT" | tr -d ' ')
DUR=$(( $(date +%s) - START_TS ))
ELAPSED="$((DUR/60))m $((DUR%60))s"
SEGMENTS="$(segment_summary)"

# Recover the merge counts (the merge ran inside the tee'd subshell).
MERGE_LINE=$(grep -oE 'MERGE session=[0-9]+ new=[0-9]+ known=[0-9]+ total=[0-9]+' "$RUN_LOG" | tail -1)
NEW=$(sed -n 's/.*new=\([0-9]*\).*/\1/p' <<<"$MERGE_LINE"); NEW=${NEW:-0}
KNOWN=$(sed -n 's/.*known=\([0-9]*\).*/\1/p' <<<"$MERGE_LINE"); KNOWN=${KNOWN:-0}
TOTAL=$(sed -n 's/.*total=\([0-9]*\).*/\1/p' <<<"$MERGE_LINE"); TOTAL=${TOTAL:-0}

if [[ "$SCRAPE_RC" == "0" && "$WINES" -gt 0 ]]; then
    notify "✅ Vivino scrape OK — ${DATE} (${HOST})
Wines this run: ${WINES}  (new ${NEW} · already-known ${KNOWN})
Master ledger total: ${TOTAL}
Duration: ${ELAPSED}
DB: ${DB_STATUS}
Segments: ${SEGMENTS}"
    exit 0
else
    ERR_TAIL="$(grep -iE 'error|traceback|exception|blocked|giving up' "$RUN_LOG" | tail -15)"
    [[ -z "$ERR_TAIL" ]] && ERR_TAIL="$(tail -15 "$RUN_LOG")"
    notify "❌ Vivino scrape FAILED — ${DATE} (${HOST})
Scraper exit: ${SCRAPE_RC}  | wines written: ${WINES}
DB: ${DB_STATUS}
Duration: ${ELAPSED}
Segments: ${SEGMENTS}
--- log tail ---
${ERR_TAIL}"
    exit 1
fi
