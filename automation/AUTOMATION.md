# Running the scraper autonomously

The scraper **must run headed** — Vivino detects and blocks headless Chromium.
That's the one thing that shapes every automation choice below: the process
needs a real or virtual display.

Everything is driven by one wrapper, [`run_scrape.sh`](run_scrape.sh), which
scrapes to a dated JSONL in `data/`, logs to `logs/`, and (if `DATABASE_URL` is
set) loads the result into Postgres. Configure it via environment variables or a
`.env` file at the repo root (git-ignored):

```bash
# .env  (repo root — never committed; copy from .env.example)
DATABASE_URL=postgresql://user:pass@localhost:5432/wines
VIVINO_COUNTRIES=ch,fr,it,es,us,de,pt,at
VIVINO_TYPES=1,2,3,4
VIVINO_MAX_PAGES=25
# Telegram (see "Telegram notifications" below):
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789
```

## Telegram notifications

`run_scrape.sh` sends a detailed message on **every** run — a ✅ summary with the
wine count, duration, DB-load status, and per-segment breakdown on success, or a
❌ report with the exit code and log tail on failure. It's a no-op (just logs a
line) until you set both variables in `.env`.

One-time setup:

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, and
   copy the **bot token** it gives you (e.g. `123456789:AA...`).
2. Send your new bot any message (e.g. "hi") so it has a chat to reply to.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   the `"chat":{"id":...}` value — that's your **chat id**.
4. Put both in `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=123456789
   ```

Test it end to end without waiting for the schedule:

```bash
bash automation/run_scrape.sh   # runs a scrape and messages you when done
```

Prerequisites on any host:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # on Linux: playwright install --with-deps chromium
```

---

## Option 1 — macOS (launchd)

Runs on your Mac in your GUI login session, so Chromium opens a real window. The
machine must be awake and logged in when the job fires (`caffeinate` or Energy
Saver settings help). Schedule in the plist is **Sunday 03:00**.

```bash
# 1. Adjust the checkout path in the plist if it isn't /Users/tim/DEV/vivinoscrape
# 2. Install and load the user agent:
cp automation/ch.nine.vivinoscrape.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ch.nine.vivinoscrape.plist

# 3. Test it right now (doesn't wait for Sunday):
launchctl kickstart -k gui/$(id -u)/ch.nine.vivinoscrape
tail -f logs/scrape-$(date +%F).log
```

Change the cadence by editing `StartCalendarInterval` in the plist, then
re-`bootstrap` it (bootout first). Remove entirely with:

```bash
launchctl bootout gui/$(id -u)/ch.nine.vivinoscrape
```

**Why launchd, not cron?** macOS `cron` runs outside the GUI session and can't
open a browser window; a launchd *user agent* can.

---

## Option 2 — Linux server (systemd + Xvfb)

For a truly unattended box with no monitor, run headed Chromium under **Xvfb**, a
virtual framebuffer. `systemd` schedules it.

```bash
sudo apt-get install -y xvfb
.venv/bin/playwright install --with-deps chromium

# Put the checkout at /opt/vivinoscrape (or edit the unit paths + User=)
sudo cp automation/vivinoscrape.service automation/vivinoscrape.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vivinoscrape.timer   # schedule it
sudo systemctl start vivinoscrape.service        # run once now to test
journalctl -u vivinoscrape.service -f            # watch
```

The service runs `xvfb-run -a run_scrape.sh`, so Chromium gets a display without
a monitor. Cadence lives in `vivinoscrape.timer` (`OnCalendar=`).

### ⚠️ One-time profile seeding (important)

A fresh server has no Vivino consent/clearance cookie, and a datacenter IP gets
challenged harder than a home connection. Before the first unattended run, seed
the persistent profile **interactively once**:

- SSH in with X-forwarding (`ssh -X`) or use a VNC/remote desktop session, then
  run `source .venv/bin/activate && python vivino_scraper.py --test` and clear any
  cookie banner / challenge in the window that appears, **or**
- seed `.vivino-browser-profile/` on your Mac (run `--test` once) and copy that
  directory up to the server.

After that the profile carries the clearance cookie and unattended runs work.
If runs start coming back blocked, re-seed the profile.

### cron alternative

If you'd rather use cron than systemd:

```cron
# crontab -e   (as the scraper user)
0 3 * * 0  cd /opt/vivinoscrape && /usr/bin/xvfb-run -a automation/run_scrape.sh
```

---

## Monitoring & good habits

- **Logs:** each run appends to `logs/scrape-<date>.log`. Check the tail for
  `DONE` vs a Python traceback.
- **Output:** dated files in `data/wines-<date>.jsonl`; the Postgres loader
  UPSERTs by `vintage_id`, so re-runs never duplicate rows.
- **Alerting:** wrap the call, or add to `run_scrape.sh`, a check that emails/pings
  you if the exit code is non-zero or the JSONL has zero lines — a WAF-protected
  site can silently start returning nothing.
- **Politeness:** keep `--min-delay/--max-delay` generous (3–8s) and avoid running
  many back-to-back segments; scraping is against Vivino's ToS, so run sparingly
  and at low volume.
- **Fragility:** tokens/DOM can change and challenges can escalate. Expect to
  revisit this occasionally — full hands-off scraping of a protected site is never
  truly set-and-forget.
