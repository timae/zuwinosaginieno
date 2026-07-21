#!/usr/bin/env python3
"""
Merge a session's scraped JSONL into a cumulative, de-duplicated master file
and report how many wines are new vs. already known.

The master (`data/wines_master.jsonl` by default) is the cross-session source of
truth: one line per unique `vintage_id`. Each session's records are merged in,
with the freshest version winning for wines already present.

Usage:
    python merge_master.py data/wines-2026-07-21.jsonl
    python merge_master.py data/wines-endurance.jsonl --master data/wines_master.jsonl

Prints a human summary and a final machine-readable line the run wrapper parses:
    MERGE session=<unique in this file> new=<added> known=<already had> total=<master size>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def load_master(path: Path) -> dict[int, dict]:
    """Return {vintage_id: record} for an existing master (empty if none)."""
    master: dict[int, dict] = {}
    if not path.exists():
        return master
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = rec.get("vintage_id")
            if vid is not None:
                master[vid] = rec
    return master


def read_session(path: Path) -> dict[int, dict]:
    """Return {vintage_id: record} for a session file (already de-duped in-run)."""
    session: dict[int, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            vid = rec.get("vintage_id")
            if vid is not None:
                session[vid] = rec
    return session


def atomic_write(path: Path, records: dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for rec in records.values():
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge a session JSONL into the master.")
    ap.add_argument("session", help="path to the session's scraped JSONL")
    ap.add_argument("--master", default="data/wines_master.jsonl",
                    help="cumulative deduped master file")
    args = ap.parse_args(argv)

    session_path = Path(args.session)
    master_path = Path(args.master)

    if not session_path.exists():
        print(f"ERROR: session file not found: {session_path}", file=sys.stderr)
        return 2

    master = load_master(master_path)
    before = len(master)

    session = read_session(session_path)
    session_unique = len(session)
    new_ids = [vid for vid in session if vid not in master]
    known = session_unique - len(new_ids)

    # Freshest version wins for wines we already had; new ones are added.
    master.update(session)
    total = len(master)

    atomic_write(master_path, master)

    print(
        f"Session {session_path.name}: {session_unique} scraped · "
        f"{len(new_ids)} new · {known} already-known · {total} total in master "
        f"(was {before})"
    )
    # Machine-readable line for the run wrapper.
    print(f"MERGE session={session_unique} new={len(new_ids)} "
          f"known={known} total={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
