"""
session_parser.py — JSONL transcript → clean structured messages.

Claude Code stores raw session transcripts as JSONL at
~/.claude/projects/<project-slug>/<session-uuid>.jsonl.

A 28MB transcript turns into ~2-3K tokens of human-readable conversation
once we strip:
  - thinking blocks (model internal monologue)
  - tool_use / tool_result blocks (file paths, command outputs, JSON)
  - queue-operation / attachment / last-prompt / system metadata lines

What we KEEP:
  - user messages (string content, or text blocks if structured)
  - assistant text blocks (the actual prose responses)
  - per-session metadata: started_at, ended_at, turn_count, tool_call_freq

What we REDACT:
  - JWT-shaped tokens (built-in pattern). Defense-in-depth so even if a
    session-stop hook pushes content to a shared Postgres team-recall, a
    JWT pasted into agent conversation doesn't leak.
  - Anything in a user-supplied `redactions.json` config file — project-
    specific credentials, test-account passwords, etc. See the README for
    the config schema. Patterns are applied to local SQLite ingestion too,
    since SQLite recall.db files might be backed up or shared.

This module is stdlib-only (json + pathlib + datetime + re + os).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Line types in the JSONL we care about.
KEEP_TYPES = {"user", "assistant"}

# Content block types in assistant messages we keep (vs. drop thinking/tool_*).
KEEP_BLOCK_TYPES = {"text"}


# Defense-in-depth credential redaction. Two flavors:
#
# 1. Literal known-leaked credentials we want to scrub by exact match
#    (prod postgres pw, demo passwords, test-account creds).
#
# 2. JWT-shape generic match. The team-recall service-role JWT lives on
#    every engineer's machine in `.claude/settings.local.json`; pasting it
#    into a session by accident would leak it into team-recall Postgres
#    where every other engineer can read it. The generic pattern catches
#    any 3-segment base64-dotted JWT (header.payload.signature with
#    `eyJ` prefix on both header and payload). Side effect: legitimate
#    JWT pastes in agent conversation get scrubbed too. That's the right
#    trade — engineers shouldn't be sharing literal JWTs in session text.
#
# We deliberately avoid broad heuristics like "anything 32+ chars looks
# like a key" because those produce too many false positives in code
# discussions. The pattern below is specifically JWT-shaped.
# Ships with one generic pattern: JWT-shape. Three base64url segments separated
# by dots, header and payload both starting with `eyJ`. Min lengths avoid false
# positives on short base64-ish strings.
#
# Side effect: any legitimate JWT pasted into session text gets scrubbed too.
# That's the right trade — engineers shouldn't be sharing literal JWTs in
# session text.
_BUILTIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}"),
        "<JWT-REDACTED>",
    ),
]


def _load_custom_redactions() -> list[tuple[re.Pattern[str], str]]:
    """Load user-defined redaction patterns from a JSON config file.

    Format (UTF-8 JSON):
      [
        {"pattern": "MySecretPassword123!", "replacement": "<REDACTED-test-cred>"},
        {"pattern": "(testuser@example\\.com)\\s*/\\s*\\S+", "replacement": "\\1 / <REDACTED>"}
      ]

    Resolved from (first that exists):
      - $TOTAL_RECALL_REDACTIONS  (env var pointing at any JSON file)
      - ~/.claude/total-recall/redactions.json
      - <repo root>/.claude/total-recall/redactions.json  (project-scoped overrides)

    Returns an empty list on any error — never blocks session parsing.
    """
    import json

    candidates: list[Path] = []
    env_path = os.environ.get("TOTAL_RECALL_REDACTIONS")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".claude" / "total-recall" / "redactions.json")
    # Project-scoped override (relative to CWD, walking up to find .claude/)
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        proj = parent / ".claude" / "total-recall" / "redactions.json"
        if proj.exists():
            candidates.append(proj)
            break

    patterns: list[tuple[re.Pattern[str], str]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                pat = entry.get("pattern")
                repl = entry.get("replacement", "<REDACTED>")
                if not isinstance(pat, str) or not isinstance(repl, str):
                    continue
                patterns.append((re.compile(pat), repl))
        except Exception:
            # Misconfigured user file should never block recall — silently skip.
            continue
    return patterns


_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = (
    _load_custom_redactions() + _BUILTIN_PATTERNS
)


def redact_secrets(text: str) -> str:
    """Apply known-secret redactions. Returns the text unchanged if none match.

    Patterns: user-supplied (via redactions.json) first, then built-in (JWT shape).
    """
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class Message:
    """One human-readable turn (user or assistant text)."""

    uuid: str
    parent_uuid: str | None
    timestamp: str  # ISO8601 from the JSONL
    role: str  # 'user' | 'assistant'
    content: str  # plain text only

    def __post_init__(self) -> None:
        # Normalize whitespace (no leading/trailing) but keep internal newlines.
        # Then apply credential redaction so downstream stores never see
        # known-leaked secrets.
        self.content = redact_secrets(self.content.strip())


@dataclass
class SessionSummary:
    """Per-session rollup metadata. Lives in the `sessions` SQLite table."""

    session_id: str
    project_slug: str
    started_at: str  # ISO of first user message
    ended_at: str  # ISO of last message in transcript
    turn_count: int  # total kept messages (user + assistant)
    tool_call_freq: dict[str, int] = field(default_factory=dict)
    git_branch: str | None = None
    cwd: str | None = None


def _extract_text_from_assistant(content: object) -> str:
    """An assistant `message.content` may be a string OR an array of blocks.

    Array blocks have a `.type` of: thinking, text, tool_use, tool_result.
    We only emit `text` blocks. Multiple text blocks are joined with newlines.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in KEEP_BLOCK_TYPES:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n\n".join(parts)


def _extract_tool_uses(content: object) -> list[str]:
    """Return the names of any tool_use blocks in an assistant message."""
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def parse_jsonl(jsonl_path: Path) -> tuple[SessionSummary, list[Message]]:
    """Parse a Claude Code JSONL transcript file.

    Returns (summary, messages). Messages are ordered by file order (which
    matches conversation order). Tool-call counts are rolled up into the
    summary so we keep the signal of "what the agent did" without storing
    the raw payloads.
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(f"transcript not found: {jsonl_path}")

    messages: list[Message] = []
    tool_freq: Counter[str] = Counter()
    session_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    git_branch: str | None = None
    cwd: str | None = None

    with jsonl_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                # Tolerate the occasional truncated line — Claude Code
                # writes the JSONL incrementally; a crash mid-write can
                # leave a partial last line. Skip and continue.
                continue

            line_type = obj.get("type")
            ts = obj.get("timestamp")
            sid = obj.get("sessionId")
            if isinstance(sid, str) and session_id is None:
                session_id = sid
            if isinstance(ts, str):
                if started_at is None:
                    started_at = ts
                ended_at = ts
            if not git_branch:
                gb = obj.get("gitBranch")
                if isinstance(gb, str) and gb:
                    git_branch = gb
            if not cwd:
                wd = obj.get("cwd")
                if isinstance(wd, str) and wd:
                    cwd = wd

            if line_type not in KEEP_TYPES:
                continue

            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue

            role = msg.get("role")
            content_raw = msg.get("content")
            uuid = obj.get("uuid")
            parent_uuid = obj.get("parentUuid")
            ts_msg = ts or ""

            if not isinstance(uuid, str):
                continue

            if role == "user":
                # User content is usually a string; sometimes a list with text blocks.
                if isinstance(content_raw, str):
                    text = content_raw
                else:
                    text = _extract_text_from_assistant(content_raw)
                if text.strip():
                    messages.append(
                        Message(
                            uuid=uuid,
                            parent_uuid=parent_uuid if isinstance(parent_uuid, str) else None,
                            timestamp=ts_msg,
                            role="user",
                            content=text,
                        )
                    )
            elif role == "assistant":
                text = _extract_text_from_assistant(content_raw)
                tool_freq.update(_extract_tool_uses(content_raw))
                if text.strip():
                    messages.append(
                        Message(
                            uuid=uuid,
                            parent_uuid=parent_uuid if isinstance(parent_uuid, str) else None,
                            timestamp=ts_msg,
                            role="assistant",
                            content=text,
                        )
                    )

    # Derive project_slug from the parent dir name. Claude Code names this
    # by escaping the project's absolute path (e.g. on Windows,
    # "C--Users-alex-projects-my-app" for "C:\\Users\\alex\\projects\\my-app").
    project_slug = jsonl_path.parent.name

    summary = SessionSummary(
        session_id=session_id or jsonl_path.stem,
        project_slug=project_slug,
        started_at=started_at or "",
        ended_at=ended_at or "",
        turn_count=len(messages),
        tool_call_freq=dict(tool_freq),
        git_branch=git_branch,
        cwd=cwd,
    )

    return summary, messages


def iter_recent_jsonls(projects_dir: Path, project_slug: str | None = None) -> Iterator[Path]:
    """Yield JSONL transcripts for the given project (or all), newest first.

    Used by the CLI tools to enumerate sessions for browsing.
    """
    if not projects_dir.exists():
        return iter(())

    if project_slug:
        target_dir = projects_dir / project_slug
        if not target_dir.exists():
            return iter(())
        candidates = sorted(
            target_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return iter(candidates)

    # All projects, all sessions
    all_jsonls: list[Path] = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        all_jsonls.extend(proj_dir.glob("*.jsonl"))
    all_jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return iter(all_jsonls)


def fmt_iso_short(ts: str) -> str:
    """Render an ISO8601 timestamp as 'YYYY-MM-DD HH:MM' for human display."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]
