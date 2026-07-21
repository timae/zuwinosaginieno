#!/usr/bin/env python3
"""
Load scraped wines.jsonl into Postgres.

Creates the schema (schema.sql) if needed, then UPSERTs each record by
vintage_id so re-runs are idempotent.

Usage:
    export DATABASE_URL=postgresql://user:pass@localhost:5432/wines
    python load_to_postgres.py wines_test.jsonl
    python load_to_postgres.py wines.jsonl --batch 500

Requires: psycopg2-binary (see requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

COLUMNS = [
    "vintage_id", "wine_id", "name", "vintage_name", "vintage_year",
    "seo_name", "vivino_url", "wine_type_id", "wine_type", "is_natural",
    "style_id", "style_name", "style_varietal_name", "style_description",
    "style_blurb", "style_body", "style_body_description", "style_acidity",
    "style_acidity_description", "winery_id", "winery_name", "region_id",
    "region_name", "country_code", "country_name",
    "style_grapes", "style_foods", "flavors",
]
JSONB_COLS = {"style_grapes", "style_foods", "flavors"}

# Back-compat: files scraped before the rename used these key names.
LEGACY_KEYS = {"style_grapes": "grapes", "style_foods": "foods"}


def row_from_record(rec: dict) -> tuple:
    out = []
    for col in COLUMNS:
        val = rec.get(col)
        if val is None and col in LEGACY_KEYS:
            val = rec.get(LEGACY_KEYS[col])
        if col in JSONB_COLS:
            val = json.dumps(val or [])
        out.append(val)
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="path to scraped JSONL file")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--schema", default="schema.sql")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args()

    if not args.dsn:
        print("ERROR: set DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2

    conn = psycopg2.connect(args.dsn)
    conn.autocommit = False
    cur = conn.cursor()

    schema_path = Path(args.schema)
    if schema_path.exists():
        cur.execute(schema_path.read_text())
        conn.commit()

    placeholders = ",".join(["%s"] * len(COLUMNS))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in COLUMNS if c != "vintage_id")
    sql = (
        f"INSERT INTO wines ({','.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (vintage_id) DO UPDATE SET {updates}"
    )

    batch, total = [], 0
    with Path(args.jsonl).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("vintage_id"):
                continue
            batch.append(row_from_record(rec))
            if len(batch) >= args.batch:
                psycopg2.extras.execute_batch(cur, sql, batch)
                conn.commit()
                total += len(batch)
                print(f"  committed {total}")
                batch = []
    if batch:
        psycopg2.extras.execute_batch(cur, sql, batch)
        conn.commit()
        total += len(batch)

    print(f"DONE. upserted {total} wines")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
