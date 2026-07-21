# Vivino wine scraper

Fetches wine **descriptive data** from Vivino's `explore` view — name, facts,
categories, grapes, food pairings, flavors, and style. **Ratings and prices are
deliberately excluded** (though both are present in the underlying payload if you
ever want them).

Output is JSON Lines (one wine per line), which loads cleanly into Postgres
either as `jsonb` or via the normalized schema in `schema.sql`.

## How it works

Vivino's old `/api/explore/explore` JSON endpoint is now edge-blocked (returns
403/415 to scripted calls), and headless browsers are served a challenge stub.
So the scraper instead:

1. Drives a **headed** Chromium (via Playwright) with a persistent profile, so
   consent and session cookies are established once and reused.
2. Builds the explore page's `e=` state token directly — it is just
   `base64url(zlib("<querystring>"))` encoding the filters and page number, e.g.
   `mr=1&min=0&max=5000&cy=ch&c=CHF&p=1&wt[]=1&cys[]=fr`. No UI clicking needed.
3. Navigates to `https://www.vivino.com/en/explore?e=<token>` and extracts the
   wine data that Vivino **server-renders into the page HTML** (~24 wines/page).

Downstream (`parse_record` → JSONL → Postgres) is unchanged and provider-agnostic.

> **Must run headed.** Headless is detected and blocked. Leave a display available
> and don't pass `--headless`. On the first run you may need to complete a cookie
> consent or interactive challenge in the browser window once; the persistent
> profile remembers it afterwards.

## Files

| File | Purpose |
|------|---------|
| `vivino_scraper.py` | Headed-browser scraper: token builder + SSR extractor + crawl loop |
| `merge_master.py` | Merge a session's JSONL into the deduped master ledger; report new vs known |
| `schema.sql` | Postgres table + indexes |
| `load_to_postgres.py` | Idempotent UPSERT loader for the JSONL |
| `requirements.txt` | Python deps |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 1. Run a test batch first

```bash
python vivino_scraper.py --test
```

Pulls Switzerland + France, red + white, 2 pages each → `wines_test.jsonl`.
A Chromium window will open. Inspect the output, confirm the fields look right,
then scale up.

## 2. Full runs

```bash
# specific origin countries & types
python vivino_scraper.py --countries ch,fr,it,es --types 1,2,3,4 --max-pages 25 --out wines.jsonl

# defaults (8 countries, 4 types, 20 pages each)
python vivino_scraper.py
```

`wine_type_ids`: `1`=Red `2`=White `3`=Sparkling `4`=Rosé `7`=Dessert `24`=Fortified.

Filter / breadth knobs:

| Flag | Meaning |
|------|---------|
| `--countries` | origin countries (ISO codes) — one crawl segment each |
| `--types` | wine type ids |
| `--max-pages` | pages per segment (~24 wines/page) |
| `--min-rating` | minimum average rating (default `1` = broadest) |
| `--min-price` / `--max-price` | price band filter |
| `--order-by` | explore sort key, e.g. `ratings_count`, `best_picks` |
| `--market` / `--currency` | browsing-market locale (pricing only; default `ch`/`CHF`) |
| `--min-delay` / `--max-delay` | politeness delay between page loads |

## 3. Load into Postgres

```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/wines
python load_to_postgres.py wines_test.jsonl
```

The loader creates the schema on first run and UPSERTs by `vintage_id`, so
re-running never duplicates rows.

## Fields captured

Identity/name: `wine_id`, `vintage_id`, `name`, `vintage_year`, `seo_name`, `vivino_url`.
Categories: `wine_type` / `wine_type_id`, `is_natural`.
Style/facts: `style_name`, `style_varietal_name`, `style_description`, `style_blurb`,
`style_body(+description)`, `style_acidity(+description)`.
Origin: `winery_name`, `region_name`, `country_code`, `country_name`.
Composition/facts (jsonb): `grapes`, `foods` (pairings), `flavors` (taste keywords).

## Cross-session dedup (the master ledger)

Each run writes its own `data/wines-<date>.jsonl` (deduped *within* that run). To
dedup *across* runs and see what's genuinely new, merge into a cumulative master:

```bash
python merge_master.py data/wines-2026-07-21.jsonl
# → Session ...: 1082 scraped · 240 new · 842 already-known · 5310 total in master
```

`data/wines_master.jsonl` holds one line per unique `vintage_id` (freshest version
wins). The automation wrapper runs this automatically and puts the
`new / already-known / master total` counts in its Telegram summary.

## Notes on pagination & coverage

Each explore view returns ~24 wines, and pagination (`p=1,2,3,…`) returns disjoint
results, so you can page deep within a single filter. To go broad, the crawl also
varies `(origin country × wine type)` — each is its own segment, and results are
de-duplicated by `vintage_id` across the whole run. Narrowing further by price
band, rating, or grape yields still more distinct result sets.

## Running it on a schedule

See [`automation/AUTOMATION.md`](automation/AUTOMATION.md) for hands-off setups —
a macOS `launchd` job and a Linux `systemd` + `Xvfb` service (headed Chromium is
required, so both give the browser a real or virtual display).

## Tuning if you get blocked

Run headed (never `--headless`). Increase delays (`--min-delay 3 --max-delay 8`),
reduce how many segments you run back-to-back, and let the persistent profile keep
its clearance cookie. If a page comes back as a tiny stub the scraper refreshes the
session and retries automatically.
