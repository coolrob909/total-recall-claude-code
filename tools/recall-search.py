#!/usr/bin/env python3
"""
recall-search.py — keyword-search past conversations via FTS5 BM25.

Invoked by the past-conversations skill. The agent constructs the query
(extracts keywords from the user's intent), passes it as `--query`. We
just run FTS5 and emit results as plain text or JSON.

Usage:
    recall-search.py --query "database OR migration OR schema" [--project SLUG] [--limit 10] [--json]

The query syntax is FTS5's: terms are AND'd by default, use OR / NOT /
parentheses for boolean logic, and "phrase" for exact match.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from lib.recall_db import RecallDB, default_db_path  # noqa: E402
from lib.session_parser import fmt_iso_short  # noqa: E402


def _list_project_slugs() -> list[str]:
    """Find every project slug that has a recall.db on this machine."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "recall.db").exists())


def main() -> int:
    p = argparse.ArgumentParser(description="Search past Claude Code conversations.")
    p.add_argument("--query", required=True, help="FTS5 query (e.g. 'foo OR bar')")
    p.add_argument("--project", default=None, help="Project slug (default: all projects)")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    # If --project is given, search just that one DB. If not, walk all DBs and merge.
    if args.project:
        project_slugs = [args.project]
    else:
        project_slugs = _list_project_slugs()

    if not project_slugs:
        print("No recall databases found.", file=sys.stderr)
        return 0

    all_results: list[dict] = []
    for slug in project_slugs:
        db_path = default_db_path(slug)
        if not db_path.exists():
            continue
        try:
            db = RecallDB(db_path)
            try:
                rows = db.search(args.query, project_slug=slug, limit=args.limit)
                all_results.extend(rows)
            finally:
                db.close()
        except Exception as e:
            print(f"warning: search failed on {slug}: {e}", file=sys.stderr)

    # Re-sort merged results by BM25 score (lower is better in SQLite FTS5).
    all_results.sort(key=lambda r: r.get("score", 0.0))
    all_results = all_results[: args.limit]

    if args.json:
        print(json.dumps(all_results, indent=2, default=str))
        return 0

    if not all_results:
        print(f"No matches for: {args.query}")
        return 0

    print(f"Found {len(all_results)} match(es) for: {args.query}\n")
    for i, r in enumerate(all_results, 1):
        when = fmt_iso_short(r.get("session_started_at", ""))
        branch = r.get("git_branch") or ""
        branch_part = f" • `{branch}`" if branch else ""
        snippet = (r.get("snippet") or "").replace("\n", " ")
        proj = r.get("project_slug", "")
        print(f"{i}. [{when}{branch_part}] ({r.get('role', '?')})")
        print(f"   project: {proj}")
        print(f"   session: {r.get('session_id', '')}")
        print(f"   snippet: {snippet}")
        print()

    print(
        "Tip: copy a session id and run `recall-list.py --session <id>` to see "
        "the full conversation."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
