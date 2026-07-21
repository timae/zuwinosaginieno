#!/usr/bin/env python3
"""
Vivino wine data scraper.

Fetches wine descriptive data (name, facts, categories, grapes, type/style)
from Vivino's `explore` view. Deliberately EXCLUDES rating and price
information as requested (both are available in the payload if ever needed).

Vivino's old /api/explore/explore JSON endpoint is now edge-blocked (403/415)
and headless browsers are challenged. Instead we drive a *headed* Chromium and
read the server-rendered results embedded in the explore page HTML. The explore
filter/pagination state is an `e=` token — base64url(zlib(querystring)) — which
we construct directly, so no UI automation is needed.

Data is written as JSON Lines (one JSON object per wine vintage) which maps
cleanly into a Postgres jsonb column and/or the normalized schema in schema.sql.

Design notes:
  * Works in batches (pagination via the `p` token param) per
    (origin country, wine_type) combination; ~24 wines per page.
  * Uses a persistent Chromium profile so consent/session state is reused.
  * Retries with backoff, refreshing the session if it looks challenged.
  * Deduplicates by vintage id across the whole run.
  * `--test` runs a small batch so you can validate before scaling up.

Usage:
    python vivino_scraper.py --test
    python vivino_scraper.py --countries ch,fr,it --types 1,2,3 --max-pages 25
    python vivino_scraper.py --help

Requires: `playwright` for browser-backed fetching. Install with:
    pip install playwright
    playwright install chromium

The browser profile is persisted so consent and valid session cookies can be reused.
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import json
import logging
import random
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

try:
    from playwright.sync_api import (
        BrowserContext,
        Page,
        Playwright,
        sync_playwright,
    )
    _HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    BrowserContext = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]
    Playwright = Any  # type: ignore[misc,assignment]
    _HAVE_PLAYWRIGHT = False

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# The explore page is server-rendered; results are embedded in the HTML. The
# old /api/explore/explore JSON endpoint is edge-blocked (403/415) and headless
# browsers are challenged, so we drive a headed Chromium and read the SSR data.
EXPLORE_URL = "https://www.vivino.com/en/explore"

# Vivino wine_type_id mapping (categories).
WINE_TYPES = {
    1: "Red",
    2: "White",
    3: "Sparkling",
    4: "Rosé",
    7: "Dessert",
    24: "Fortified",
}

PER_PAGE = 24          # explore renders 24 results per page
DEFAULT_MAX_PAGES = 20
DEFAULT_COUNTRIES = ["ch", "fr", "it", "es", "us", "de", "pt", "at"]
DEFAULT_TYPES = [1, 2, 3, 4]

# Default "market" the browser presents itself as (affects currency/availability
# only — we keep descriptive data, so this rarely matters). Overridable via CLI.
DEFAULT_MARKET = "ch"
DEFAULT_CURRENCY = "CHF"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vivino")


# --------------------------------------------------------------------------- #
# Explore token + embedded-JSON extraction
# --------------------------------------------------------------------------- #

def make_token(
    origin_country: str,
    wine_type: int,
    page: int,
    min_rating: float,
    min_price: int,
    max_price: int,
    market: str,
    currency: str,
    order_by: str,
) -> str:
    """
    Build the explore `e=` token: base64url(zlib(querystring)).

    Vivino encodes the whole filter state this way. Constructing it directly
    lets us paginate (`p`) and filter (origin `cys[]`, type `wt[]`) without
    driving the site's UI.
    """
    params = [
        ("mr", min_rating),
        ("min", min_price),
        ("max", max_price),
        ("cy", market),
        ("c", currency),
        ("p", page),
        ("gf", "varietal"),
        ("ord", "desc"),
        ("oby", order_by),
        ("wt[]", wine_type),
        ("cys[]", origin_country.lower()),
    ]
    qs = urlencode(params)
    comp = zlib.compress(qs.encode("utf-8"))
    return base64.urlsafe_b64encode(comp).decode("ascii").rstrip("=")


def _extract_matches_html(page_html: str) -> list[dict[str, Any]]:
    """
    Pull the embedded `"matches":[ ... ]` array out of the server-rendered HTML.

    The payload is HTML-entity-encoded JSON; we unescape it then balance-scan
    the array so nested braces/brackets inside string values don't fool us.
    """
    text = html_lib.unescape(page_html)
    key = '"matches":['
    best: list[dict[str, Any]] = []
    start = 0
    while True:
        k = text.find(key, start)
        if k == -1:
            break
        i = k + len(key) - 1  # position of the opening '['
        depth = 0
        in_str = False
        esc = False
        while i < len(text):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        try:
            arr = json.loads(text[k + len(key) - 1:i + 1])
            if isinstance(arr, list) and len(arr) > len(best):
                best = arr
        except Exception:
            pass
        start = k + len(key)
    return best


def _records_matched(page_html: str) -> int:
    m = re.search(r'"records_matched"\s*:\s*(\d+)', html_lib.unescape(page_html))
    return int(m.group(1)) if m else 0


def _looks_blocked(page_html: str) -> bool:
    """Detect the tiny challenge/block stub served to unhappy sessions."""
    return len(page_html) < 20_000 and '"matches":[' not in html_lib.unescape(page_html)


class BrowserFetcher:
    """Persistent headed Chromium session that reads server-rendered explore data."""

    def __init__(
        self,
        profile_dir: Path,
        headless: bool,
        market: str = DEFAULT_MARKET,
        currency: str = DEFAULT_CURRENCY,
        min_rating: float = 1,
        min_price: int = 0,
        max_price: int = 5000,
        order_by: str = "ratings_count",
        bootstrap_timeout_ms: int = 90_000,
    ) -> None:
        if not _HAVE_PLAYWRIGHT:
            raise RuntimeError(
                "Playwright is required. Run: pip install playwright && "
                "playwright install chromium"
            )

        self.market = market
        self.currency = currency
        self.min_rating = min_rating
        self.min_price = min_price
        self.max_price = max_price
        self.order_by = order_by

        if headless:
            log.warning(
                "Headless mode is detected and blocked by Vivino; use headed "
                "(omit --headless) so the real browser session can load results."
            )

        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw: Playwright = sync_playwright().start()
        self.context: BrowserContext = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        self.page: Page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
        self._bootstrap(bootstrap_timeout_ms)

    def _bootstrap(self, timeout_ms: int) -> None:
        page = self.page
        log.info("Opening Vivino in Chromium to initialize consent/session cookies")
        page.goto(
            EXPLORE_URL,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        for label in (
            "Accept All Cookies",
            "Accept All",
            "Allow all",
            "I agree",
        ):
            try:
                button = page.get_by_role("button", name=label, exact=False)
                if button.count():
                    button.first.click(timeout=2_500)
                    log.info("Accepted cookie consent")
                    break
            except Exception:
                continue

        # Give the browser challenge and first-party scripts time to establish
        # their normal session state. In headed mode, a user can complete any
        # interactive challenge presented by the site.
        page.wait_for_timeout(6_000)

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._pw.stop()

    def fetch_page(
        self,
        country: str,
        wine_type: int,
        page: int,
        max_retries: int = 5,
        debug: bool = False,
    ) -> dict[str, Any] | None:
        """
        Load one explore page (origin `country` × `wine_type` × `page`) and
        return {"matches": [...], "records_matched": N} from the SSR payload.
        """
        token = make_token(
            origin_country=country,
            wine_type=wine_type,
            page=page,
            min_rating=self.min_rating,
            min_price=self.min_price,
            max_price=self.max_price,
            market=self.market,
            currency=self.currency,
            order_by=self.order_by,
        )
        url = f"{EXPLORE_URL}?e={token}"

        for attempt in range(1, max_retries + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                # Let the SSR markup settle (hydration can rewrite the DOM).
                self.page.wait_for_timeout(2_500)
                page_html = self.page.content()

                if debug and (attempt == 1 or _looks_blocked(page_html)):
                    log.info("DEBUG html_len=%d blocked=%s",
                             len(page_html), _looks_blocked(page_html))

                if _looks_blocked(page_html):
                    wait = min(90, 3 ** attempt) + random.uniform(0, 5)
                    log.warning(
                        "Session looks blocked/challenged; refreshing then "
                        "retrying in %.1fs", wait,
                    )
                    self._bootstrap(90_000)
                    time.sleep(wait)
                    continue

                matches = _extract_matches_html(page_html)
                return {
                    "matches": matches,
                    "records_matched": _records_matched(page_html),
                }

            except Exception as exc:
                wait = min(30, 2 ** attempt) + random.uniform(0, 2)
                log.warning("attempt %d failed (%s), retry in %.1fs", attempt, exc, wait)
                time.sleep(wait)

        log.error("Giving up on %s/%s page %s", country, wine_type, page)
        return None


# --------------------------------------------------------------------------- #
# Parsing — extract ONLY name / facts / categories (no ratings, no prices)
# --------------------------------------------------------------------------- #

def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return default
    return obj if obj is not None else default


def parse_record(match: dict[str, Any]) -> dict[str, Any] | None:
    """
    Map one explore 'match' record to our clean wine dict.

    We intentionally drop: statistics/ratings_* and any price.* fields.
    """
    vintage = match.get("vintage") or {}
    wine = vintage.get("wine") or {}
    if not wine.get("id"):
        return None

    style = wine.get("style") or {}
    taste = wine.get("taste") or {}
    region = wine.get("region") or {}
    country = region.get("country") or {}
    winery = wine.get("winery") or {}

    grapes = [
        {"id": g.get("id"), "name": g.get("name")}
        for g in (style.get("grapes") or [])
        if isinstance(g, dict)
    ]

    # Food pairings — a descriptive "fact", not a rating.
    foods = [
        {"id": f.get("id"), "name": f.get("name")}
        for f in (style.get("food") or [])
        if isinstance(f, dict)
    ]

    # Flavor groups / taste keywords (descriptive facts).
    flavors = []
    for grp in (taste.get("flavor") or []):
        if isinstance(grp, dict):
            flavors.append({
                "group": grp.get("group"),
                "keywords": [
                    k.get("name") for k in (grp.get("primary_keywords") or [])
                    if isinstance(k, dict)
                ],
            })

    type_id = wine.get("type_id")

    return {
        # --- identity / name ---
        "vintage_id": vintage.get("id"),
        "wine_id": wine.get("id"),
        "name": wine.get("name"),
        "vintage_name": vintage.get("name"),
        "vintage_year": vintage.get("year"),
        "seo_name": wine.get("seo_name"),
        "vivino_url": (
            f"https://www.vivino.com/wines/{wine.get('id')}" if wine.get("id") else None
        ),

        # --- categories ---
        "wine_type_id": type_id,
        "wine_type": WINE_TYPES.get(type_id),
        "is_natural": wine.get("is_natural"),

        # --- style ---
        "style_id": style.get("id"),
        "style_name": style.get("name"),
        "style_varietal_name": style.get("varietal_name"),
        "style_description": style.get("description"),
        "style_blurb": style.get("blurb"),
        "style_body": style.get("body"),
        "style_body_description": style.get("body_description"),
        "style_acidity": style.get("acidity"),
        "style_acidity_description": style.get("acidity_description"),

        # --- producer / origin (facts) ---
        "winery_id": winery.get("id"),
        "winery_name": winery.get("name"),
        "region_id": region.get("id"),
        "region_name": region.get("name"),
        "country_code": country.get("code"),
        "country_name": country.get("name"),

        # --- composition + descriptive facts ---
        "grapes": grapes,
        "foods": foods,
        "flavors": flavors,
    }


def extract_matches(payload: dict[str, Any]) -> tuple[list[dict], int]:
    """Return (matches, total_matched) handling the explore_vintage wrapper."""
    exp = payload.get("explore_vintage") or payload
    matches = exp.get("matches") or exp.get("records") or []
    total = exp.get("records_matched") or exp.get("records_matched_count") or 0
    return matches, int(total or 0)


# --------------------------------------------------------------------------- #
# Crawl orchestration
# --------------------------------------------------------------------------- #

def crawl(
    countries: list[str],
    types: list[int],
    max_pages: int,
    out_path: Path,
    min_delay: float,
    max_delay: float,
    profile_dir: Path,
    headless: bool,
    market: str = DEFAULT_MARKET,
    currency: str = DEFAULT_CURRENCY,
    min_rating: float = 1,
    min_price: int = 0,
    max_price: int = 5000,
    order_by: str = "ratings_count",
    debug: bool = False,
) -> int:
    fetcher = BrowserFetcher(
        profile_dir=profile_dir,
        headless=headless,
        market=market,
        currency=currency,
        min_rating=min_rating,
        min_price=min_price,
        max_price=max_price,
        order_by=order_by,
    )
    seen: set[int] = set()
    written = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_path.open("w", encoding="utf-8") as fh:
            for country in countries:
                for wtype in types:
                    log.info(
                        "=== %s / %s ===",
                        country.upper(),
                        WINE_TYPES.get(wtype, wtype),
                    )
                    for page in range(1, max_pages + 1):
                        payload = fetcher.fetch_page(
                            country, wtype, page, debug=debug
                        )
                        if not payload:
                            break

                        matches, total = extract_matches(payload)
                        if not matches:
                            log.info(
                                "  page %d: empty, stopping this segment", page
                            )
                            break

                        new_here = 0
                        for match in matches:
                            rec = parse_record(match)
                            if not rec or rec["vintage_id"] in seen:
                                continue
                            seen.add(rec["vintage_id"])
                            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            written += 1
                            new_here += 1

                        fh.flush()
                        log.info(
                            "  page %d/%d: +%d wines "
                            "(total written %d, matched %d)",
                            page, max_pages, new_here, written, total,
                        )

                        # Stop the segment when the page is short (last page)
                        # or when it yields only wines we've already stored
                        # (pagination has run past the unique results).
                        if len(matches) < PER_PAGE or new_here == 0:
                            break
                        time.sleep(random.uniform(min_delay, max_delay))
    finally:
        fetcher.close()

    log.info("DONE. %d unique wines written to %s", written, out_path)
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape wine facts/categories from Vivino.")
    p.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES),
                   help="comma-separated ISO country codes (e.g. ch,fr,it)")
    p.add_argument("--types", default=",".join(str(t) for t in DEFAULT_TYPES),
                   help="comma-separated wine_type_ids (1=Red 2=White 3=Sparkling "
                        "4=Rosé 7=Dessert 24=Fortified)")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                   help="max pages per country/type segment (24 wines/page)")
    p.add_argument("--out", default="wines.jsonl", help="output JSONL path")
    p.add_argument("--min-delay", type=float, default=1.5,
                   help="min seconds between page requests")
    p.add_argument("--max-delay", type=float, default=4.0,
                   help="max seconds between page requests")
    p.add_argument("--market", default=DEFAULT_MARKET,
                   help="browsing-market country code for the session (pricing "
                        "locale; does not affect descriptive data)")
    p.add_argument("--currency", default=DEFAULT_CURRENCY,
                   help="currency code paired with --market")
    p.add_argument("--min-rating", type=float, default=1,
                   help="minimum average rating filter (1 = broadest coverage)")
    p.add_argument("--min-price", type=int, default=0, help="min price filter")
    p.add_argument("--max-price", type=int, default=5000, help="max price filter")
    p.add_argument("--order-by", default="ratings_count",
                   help="explore sort key, e.g. ratings_count or best_picks")
    p.add_argument(
        "--profile-dir",
        default=".vivino-browser-profile",
        help="persistent Chromium profile directory",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="run Chromium without a visible window; headed mode is safer initially",
    )
    p.add_argument("--test", action="store_true",
                   help="quick validation run: ch+fr, red+white, 2 pages each")
    p.add_argument("--debug", action="store_true",
                   help="dump raw HTTP status, content-type and response body")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)

    if args.test:
        countries = ["ch", "fr"]
        types = [1, 2]
        max_pages = 2
        out = Path("wines_test.jsonl")
        log.info("TEST MODE: %s x %s, %d pages each", countries, types, max_pages)
    else:
        countries = [c.strip().lower() for c in args.countries.split(",") if c.strip()]
        types = [int(t) for t in args.types.split(",") if t.strip()]
        max_pages = args.max_pages
        out = Path(args.out)

    written = crawl(
        countries,
        types,
        max_pages,
        out,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        profile_dir=Path(args.profile_dir),
        headless=args.headless,
        market=args.market,
        currency=args.currency,
        min_rating=args.min_rating,
        min_price=args.min_price,
        max_price=args.max_price,
        order_by=args.order_by,
        debug=args.debug,
    )
    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
