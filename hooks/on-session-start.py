#!/usr/bin/env python3
"""
on-session-start.py — invoked by Claude Code on the SessionStart event.

Looks up the most recent meaningful session for THIS project (matching by
slug or cwd) and emits a context block on stdout. Claude Code injects that
output into the new session's system prompt, so the agent starts with
"here's what we worked on last time" without the user having to ask.

Token budget target: 2-3K tokens of distilled prose. We:
  1. Pick the most recent session with >= 2 turns (skip noise).
  2. Trim each message body to ~600 chars unless it's the last 2 user/assistant pair.
  3. Drop overly verbose tool-output paste-backs (heuristic: lines with >100 chars of contiguous non-prose).
  4. Cap total output at ~12000 chars (~3K tokens).

Output format is intentionally plain text with light markdown so it reads
naturally in the system prompt.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 on stdout/stderr regardless of platform default (Windows cp1252
# will crash on emoji / em-dash / Unicode arrows in session content otherwise).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from lib.recall_db import RecallDB, default_db_path  # noqa: E402
from lib.session_parser import fmt_iso_short  # noqa: E402
from lib.team_config import _resolve_project_root, load_team_recall_config  # noqa: E402
from lib.team_recall_client import TeamRecallError, list_recent  # noqa: E402

MAX_OUTPUT_CHARS = 12000  # ~3K tokens for local recall block
PER_TURN_TRIM = 600  # chars per message before tail
TAIL_PAIRS_FULL = 2  # last N user/assistant pairs are kept full-length

# Phase 2C extension — also surface recent activity from OTHER engineers
TEAM_ACTIVITY_LOOKBACK_DAYS = 7  # only show team sessions newer than this
TEAM_ACTIVITY_MAX_SESSIONS = 10  # how many to render in the block
TEAM_ACTIVITY_FETCH_LIMIT = 30  # over-fetch so we have headroom after filtering
TEAM_ACTIVITY_MAX_CHARS = 3000  # ~750 tokens for the team-activity block


def _log(msg: str) -> None:
    try:
        log_dir = Path.home() / ".claude" / "projects" / "_total-recall-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "session-start.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


def _resolve_project_slug(payload: dict) -> str | None:
    slug = payload.get("project_slug") or payload.get("projectSlug")
    if isinstance(slug, str):
        return slug
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        # Claude Code's project slug convention: replace path separators + colons with dashes.
        # Best-effort — match how the projects directory names match the cwd.
        slug = cwd.replace("\\", "-").replace("/", "-").replace(":", "-")
        # Some Claude Code versions add a leading 'C--' or similar; keep it as-is.
        return slug.lstrip("-")
    return None


def _trim_turn(text: str, full: bool) -> str:
    """Trim a single message body. `full` keeps the entire content."""
    if full:
        return text
    if len(text) <= PER_TURN_TRIM:
        return text
    return text[:PER_TURN_TRIM].rstrip() + " […]"


def _format_team_activity(sessions: list[dict], current_engineer: str) -> str:
    """Render a compact "what the team has been working on" block.

    Excludes sessions authored by `current_engineer`. Shows engineer +
    branch + turn count + top tool counts per session, no message content
    (the agent can drill in via the `team-conversations` skill if needed).

    Returns empty string if there's nothing to show after filtering.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - (TEAM_ACTIVITY_LOOKBACK_DAYS * 86400)

    rendered: list[str] = []
    rendered.append(
        f"Recent team activity (other engineers, last {TEAM_ACTIVITY_LOOKBACK_DAYS} days):"
    )
    rendered.append("")

    shown = 0
    for s in sessions:
        if shown >= TEAM_ACTIVITY_MAX_SESSIONS:
            break

        engineer = s.get("engineer_id", "?")
        if engineer == current_engineer:
            continue  # exclude self

        # Recency filter: use ended_at (most recent activity in the session)
        # rather than started_at, so a long-lived session that started weeks
        # ago but was touched today still counts as active. Continue past
        # stale rows rather than break, since list_recent_team_recall orders
        # by started_at — a stale row may appear before a fresh one.
        started_at_raw = s.get("started_at") or ""
        ended_at_raw = s.get("ended_at") or started_at_raw
        recency_ts: float | None = None
        if isinstance(ended_at_raw, str) and ended_at_raw:
            try:
                dt = datetime.fromisoformat(ended_at_raw.replace("Z", "+00:00"))
                recency_ts = dt.timestamp()
            except ValueError:
                recency_ts = None
        if recency_ts is not None and recency_ts < cutoff:
            continue

        # Display the session by its END time so engineers see how recently
        # it was last touched.
        when = fmt_iso_short(ended_at_raw)
        branch = s.get("git_branch") or ""
        branch_part = f" on `{branch}`" if branch else ""
        project = s.get("project_slug", "") or "?"
        turn_count = s.get("turn_count", 0)

        # Tool-call summary: list_recent_team_recall doesn't return
        # tool_call_freq, just the metadata we asked for. Skip the tools
        # line for now — agent can see tool patterns by drilling into
        # the session via team-conversations skill.
        rendered.append(f"- [{when}] **{engineer}**{branch_part} ({turn_count} turns)")
        rendered.append(f"   project: {project}")
        shown += 1

    if shown == 0:
        return ""

    block = "\n".join(rendered)
    if len(block) > TEAM_ACTIVITY_MAX_CHARS:
        block = block[:TEAM_ACTIVITY_MAX_CHARS].rstrip() + "\n\n[…trimmed…]"
    return block


def _emit_team_activity_block(stdout: object, payload: dict | None = None) -> None:  # type: ignore[type-arg]
    """Best-effort: fetch + emit a team-activity block. Never raises.

    The whole block is wrapped in <total-recall-team-activity> so the agent
    can recognize it as injected context.
    """
    try:
        cfg = load_team_recall_config(_resolve_project_root(payload=payload))
        if cfg is None:
            return  # team-recall not configured; quiet skip

        sessions = list_recent(
            url=cfg.url,
            service_key=cfg.service_key,
            limit=TEAM_ACTIVITY_FETCH_LIMIT,
            engineer_id=None,  # we want all engineers, then filter client-side
            project_slug=None,
            min_turns=2,
        )

        block = _format_team_activity(sessions, current_engineer=cfg.engineer_id)
        if not block:
            return  # nothing to show

        sys.stdout.write(
            "<total-recall-team-activity>\n"
            "Auto-injected by total-recall. Recent sessions from OTHER engineers, "
            "drawn from the team-shared Postgres recall corpus. Use the "
            "`team-conversations` skill to drill into any session, or the "
            "`check-team-memory` skill to look up codified rules.\n\n"
            f"{block}\n"
            "</total-recall-team-activity>\n"
        )
        sys.stdout.flush()
        _log(f"injected team-activity block ({len(block)} chars)")
    except TeamRecallError as e:
        _log(f"team-activity fetch failed (non-fatal): {e}")
    except Exception as e:
        _log(f"team-activity unexpected error (non-fatal): {e}")


def _format_session(session: dict, messages: list[dict]) -> str:
    """Render a single session as a context block."""
    parts: list[str] = []
    started = fmt_iso_short(session.get("started_at", ""))
    branch = session.get("git_branch") or ""
    branch_part = f" on `{branch}`" if branch else ""
    parts.append(f"### Previous session ({started}{branch_part})")
    parts.append("")

    # Tool-call summary (one line, signal-only).
    tcf_raw = session.get("tool_call_freq") or "{}"
    try:
        tcf = json.loads(tcf_raw) if isinstance(tcf_raw, str) else tcf_raw
        if isinstance(tcf, dict) and tcf:
            top = sorted(tcf.items(), key=lambda x: -x[1])[:6]
            parts.append("Tools used: " + ", ".join(f"{k} ×{v}" for k, v in top))
            parts.append("")
    except Exception:
        pass

    # The last 2 user/assistant pairs are kept full; earlier turns get trimmed.
    n = len(messages)
    full_from = max(0, n - (TAIL_PAIRS_FULL * 2))
    for i, m in enumerate(messages):
        is_full = i >= full_from
        role = "User" if m["role"] == "user" else "Claude"
        body = _trim_turn(m["content"], full=is_full)
        parts.append(f"**{role}:** {body}")
        parts.append("")

    return "\n".join(parts)


def _emit_local_recall_block(project_slug: str, payload: dict) -> None:
    """Best-effort: inject the most recent local-machine session for this
    project. Never raises (memory write should never break Claude Code).
    """
    try:
        db_path = default_db_path(project_slug)
        if not db_path.exists():
            # First-ever session for this project on this machine — nothing local
            # to inject. (Team-activity may still fire below.)
            return

        db = RecallDB(db_path)
        try:
            recent = db.list_recent_sessions(project_slug=project_slug, limit=1, min_turns=2)
            if not recent:
                return
            session = recent[0]
            current_session_id = payload.get("session_id") or payload.get("sessionId")
            if isinstance(current_session_id, str) and session["session_id"] == current_session_id:
                # Same session as we're starting — likely a resume; nothing new.
                return

            msgs = db.get_session_messages(session["session_id"])
            if not msgs:
                return

            block = _format_session(session, msgs)
            if len(block) > MAX_OUTPUT_CHARS:
                block = block[:MAX_OUTPUT_CHARS].rstrip() + "\n\n[…trimmed for context budget…]"

            sys.stdout.write(
                "<total-recall-recall>\n"
                "Auto-injected by total-recall. This is the most recent prior session "
                "for this project on THIS machine (signal-distilled, tool noise stripped).\n\n"
                f"{block}\n"
                "</total-recall-recall>\n"
            )
            sys.stdout.flush()
            _log(
                f"injected local session {session['session_id'][:8]} "
                f"({len(msgs)} turns, {len(block)} chars)"
            )
        finally:
            db.close()
    except Exception as e:
        _log(f"local recall inject failed (non-fatal): {e}\n{traceback.format_exc()}")


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
            _log(f"non-JSON stdin: {raw[:200]!r}")

    project_slug = _resolve_project_slug(payload)
    if not project_slug:
        # Without a project slug we can't scope local recall. We still fire
        # team-activity below since that's not project-scoped.
        _emit_team_activity_block(sys.stdout, payload=payload)
        return 0

    # 1. Local recall (per-machine, per-engineer) — Phase 1
    _emit_local_recall_block(project_slug, payload)

    # 2. Team activity (Postgres, all configured engineers) — Phase 2C
    # Fires INDEPENDENTLY of local recall, so a brand-new engineer with
    # no local SQLite still gets the team-activity block on session 1.
    _emit_team_activity_block(sys.stdout, payload=payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
