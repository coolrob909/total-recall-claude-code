#!/usr/bin/env python3
"""
recall-list.py — browse recent sessions or pull a single session's transcript.

Two modes:
  1. List recent sessions chronologically (--limit N --project SLUG)
  2. Dump a single session's clean conversation text (--session <id>)

Used by the past-conversations skill when the agent wants to recall by date
("what did we work on yesterday?") rather than by keyword.
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
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "recall.db").exists())


def _open_db_for_session(session_id: str) -> tuple[RecallDB | None, str | None]:
    """Walk all project recall.dbs to find which one owns this session id."""
    for slug in _list_project_slugs():
        db = RecallDB(default_db_path(slug))
        try:
            rows = db._conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if rows:
                return db, slug
            db.close()
        except Exception:
            db.close()
    return None, None


def cmd_list(args: argparse.Namespace) -> int:
    if args.project:
        project_slugs = [args.project]
    else:
        project_slugs = _list_project_slugs()

    rows: list[dict] = []
    for slug in project_slugs:
        db_path = default_db_path(slug)
        if not db_path.exists():
            continue
        db = RecallDB(db_path)
        try:
            rows.extend(db.list_recent_sessions(project_slug=slug, limit=args.limit, min_turns=2))
        finally:
            db.close()

    rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    rows = rows[: args.limit]

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if not rows:
        print("No sessions on record.")
        return 0

    print(f"Most recent {len(rows)} session(s):\n")
    for r in rows:
        when = fmt_iso_short(r.get("started_at", ""))
        branch = r.get("git_branch") or ""
        branch_part = f" • {branch}" if branch else ""
        sid = r.get("session_id", "")
        turns = r.get("turn_count", 0)
        proj = r.get("project_slug", "")
        print(f"  [{when}{branch_part}] {turns} turns • {sid}")
        print(f"    project: {proj}")
    print(
        "\nTip: pass --session <id> to dump the clean transcript of one session."
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db, slug = _open_db_for_session(args.session)
    if db is None:
        print(f"Session not found: {args.session}", file=sys.stderr)
        return 1
    try:
        msgs = db.get_session_messages(args.session)
    finally:
        db.close()

    if not msgs:
        print("Session has no messages.")
        return 0

    if args.json:
        print(json.dumps(msgs, indent=2, default=str))
        return 0

    print(f"Session {args.session} (project: {slug}, {len(msgs)} turns)\n")
    for m in msgs:
        when = fmt_iso_short(m.get("timestamp", ""))
        role = "User" if m["role"] == "user" else "Claude"
        print(f"[{when}] {role}:")
        print(m["content"])
        print()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="List or dump past Claude Code sessions.")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--project", default=None, help="Restrict to one project slug")
    p.add_argument("--session", default=None, help="Dump a specific session's transcript")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.session:
        return cmd_show(args)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())
