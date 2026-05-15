#!/usr/bin/env python3
"""
on-session-stop.py — invoked by Claude Code on the Stop / SessionEnd event.

Reads the current session's JSONL, parses out the human-readable conversation,
and writes it to ~/.claude/projects/<slug>/recall.db. Idempotent (uuid-keyed),
so re-firing on every Stop is fine.

Hooks pass session metadata via stdin as JSON. We tolerate either flavor of
event — Claude Code emits slightly different shapes for `Stop` vs `SessionEnd`,
but both include enough info to locate the JSONL.

Exit codes are advisory — we never fail a session for a memory-write hiccup,
because the user shouldn't notice when it works (or fails). Errors go to stderr
and a sidecar log file.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 on stdout/stderr (Windows cp1252 will crash on Unicode otherwise).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Resolve our lib/ next to this file regardless of CWD.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from lib.recall_db import RecallDB, default_db_path  # noqa: E402
from lib.session_parser import parse_jsonl  # noqa: E402
from lib.team_config import _resolve_project_root, load_team_recall_config  # noqa: E402
from lib.team_recall_client import TeamRecallError, ingest_session  # noqa: E402


def _log(msg: str) -> None:
    """Append to a sidecar log; never raise. Used for diagnostics when hooks
    don't get to print to a visible UI."""
    try:
        log_dir = Path.home() / ".claude" / "projects" / "_total-recall-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "session-stop.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass  # logging is best-effort


def _resolve_jsonl(payload: dict) -> Path | None:
    """Find the transcript path. Several possibilities depending on hook event:
      1. payload['transcript_path'] (some Claude Code versions)
      2. payload['session_id'] + payload['cwd_or_project_slug']
      3. derive from the project slug + session UUID convention
    """
    tp = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(tp, str) and Path(tp).exists():
        return Path(tp)

    session_id = payload.get("session_id") or payload.get("sessionId")
    project_slug = payload.get("project_slug") or payload.get("projectSlug")

    # Fallback: scan for the most-recently-modified .jsonl in the matching project dir.
    if isinstance(project_slug, str):
        proj_dir = Path.home() / ".claude" / "projects" / project_slug
        if proj_dir.exists():
            if isinstance(session_id, str):
                candidate = proj_dir / f"{session_id}.jsonl"
                if candidate.exists():
                    return candidate
            jsonls = sorted(
                proj_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if jsonls:
                return jsonls[0]
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    payload: dict = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            _log(f"non-JSON stdin (first 200 chars): {raw[:200]!r}")

    jsonl_path = _resolve_jsonl(payload)
    if jsonl_path is None:
        _log("could not resolve transcript path from payload; skipping ingest")
        # exit 0 — never fail a session for our hook
        return 0

    try:
        summary, messages = parse_jsonl(jsonl_path)
        if not messages:
            _log(f"no messages parsed from {jsonl_path}; skipping")
            return 0

        db = RecallDB(default_db_path(summary.project_slug))
        try:
            stats = db.ingest(summary, messages)
            _log(
                f"local ingest {jsonl_path.name}: "
                f"{stats['messages_inserted']} new / {stats['messages_total']} total messages"
            )
        finally:
            db.close()
    except Exception as e:
        _log(f"local ingest failed for {jsonl_path}: {e}\n{traceback.format_exc()}")
        # Still continue to team-recall path; failure of one path should not
        # block the other. But never exit non-zero.

    # ── Phase 2C: dual-write to team-recall (best-effort) ──────────────────
    # If team-recall isn't configured, skip silently. If it errors, log and
    # continue; the local SQLite write above is the primary system.
    try:
        cfg = load_team_recall_config(
            _resolve_project_root(
                cwd_hint=summary.cwd if "summary" in dir() else None,
                payload=payload,
            )
        )
        if cfg is None:
            _log("team-recall not configured (no env / settings.local.json); skipping team write")
            return 0

        # Reuse the parsed summary + messages from above (don't re-parse).
        if "summary" not in locals() or "messages" not in locals():
            return 0  # parse failed; nothing to push

        msg_payload = [
            {
                "uuid":        m.uuid,
                "parent_uuid": m.parent_uuid,
                "timestamp":   m.timestamp,
                "role":        m.role,
                "content":     m.content,  # already redacted by session_parser
            }
            for m in messages
        ]

        result = ingest_session(
            url=cfg.url,
            service_key=cfg.service_key,
            session_id=summary.session_id,
            engineer_id=cfg.engineer_id,
            project_slug=summary.project_slug,
            started_at=summary.started_at,
            ended_at=summary.ended_at,
            turn_count=summary.turn_count,
            tool_call_freq=summary.tool_call_freq,
            git_branch=summary.git_branch,
            cwd=summary.cwd,
            messages=msg_payload,
        )
        _log(
            f"team-recall ingest {jsonl_path.name} (engineer={cfg.engineer_id}): "
            f"{result.get('messages_inserted', '?')} new / "
            f"{result.get('messages_total', '?')} total"
        )
    except TeamRecallError as e:
        _log(f"team-recall ingest failed (non-fatal): {e}")
    except Exception as e:
        _log(f"team-recall path errored unexpectedly (non-fatal): {e}\n{traceback.format_exc()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
