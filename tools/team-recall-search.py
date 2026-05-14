#!/usr/bin/env python3
"""
team-recall-search.py — search the team-shared session corpus via Postgres FTS.

Same shape as recall-search.py (Phase 1, local) but hits the Supabase RPC
that searches across every team member's ingested sessions. Used by the
`team-conversations` skill.

If team-recall isn't configured (no env / settings.local.json), this tool
exits cleanly with a helpful message rather than erroring.

Usage:
    team-recall-search.py "deno cache" "_shared module"
    team-recall-search.py --engineer alice --limit 5 "PostgREST"
    team-recall-search.py --json "saved reports"

Postgres `websearch_to_tsquery` is permissive: pass terms separated by
spaces, use `OR` between alternatives, `"phrase"` for exact match,
`-term` to exclude. Examples:
    "postgrest fk"                         (AND of both terms)
    "schema OR migration"                  (either)
    '"saved reports" -archived'            (phrase, exclude term)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from lib.team_config import load_team_recall_config  # noqa: E402
from lib.team_recall_client import TeamRecallError, search  # noqa: E402


def _fmt_iso(ts: str | None) -> str:
    if not ts:
        return ""
    return ts[:16].replace("T", " ")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Search team-shared Claude Code session history via Postgres FTS.",
    )
    p.add_argument(
        "query_terms",
        nargs="+",
        help="One or more terms / phrases. Joined with spaces and passed to "
        "websearch_to_tsquery.",
    )
    p.add_argument("--engineer", default=None, help="Filter to a specific engineer_id (e.g. 'alice')")
    p.add_argument("--project", default=None, help="Filter to a specific project_slug")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    cfg = load_team_recall_config()
    if cfg is None:
        print(
            "Team-recall is not configured on this machine.\n"
            "Set TOTAL_RECALL_TEAM_URL + TOTAL_RECALL_TEAM_KEY env vars,\n"
            "or add an `total_recall.team_recall` block to .claude/settings.local.json.\n"
            "See .claude/plugins/total-recall/README.md.",
            file=sys.stderr,
        )
        return 0

    query = " ".join(args.query_terms).strip()
    if not query:
        print("ERROR: query is empty.", file=sys.stderr)
        return 2

    try:
        results = search(
            url=cfg.url,
            service_key=cfg.service_key,
            query=query,
            limit=args.limit,
            engineer_id=args.engineer,
            project_slug=args.project,
        )
    except TeamRecallError as e:
        print(f"team-recall search failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    if not results:
        print(f"No team-recall matches for: {query}")
        return 0

    print(f"Found {len(results)} team-recall match(es) for: {query}\n")
    for i, r in enumerate(results, 1):
        when = _fmt_iso(r.get("ts"))
        eid = r.get("engineer_id", "?")
        proj = r.get("project_slug", "?")
        branch = r.get("git_branch") or ""
        branch_part = f" • `{branch}`" if branch else ""
        rank = r.get("rank", 0.0)
        preview = (r.get("content_preview") or "").replace("\n", " ")
        print(f"{i}. [{when}] {eid}{branch_part}  (rank {rank:.3f})")
        print(f"   project: {proj}")
        print(f"   session: {r.get('session_id')}")
        print(f"   role:    {r.get('role')}")
        print(f"   preview: {preview}")
        print()

    print("Tip: --engineer <id> to narrow by author; --project <slug> to narrow by repo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
