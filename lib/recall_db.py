"""
recall_db.py — SQLite + FTS5 wrapper for total-recall recall layer.

DB lives at ~/.claude/projects/<project-slug>/recall.db (per-machine, per-user,
per-project). Stdlib only.

Schema
------
- sessions: one row per ingested transcript file, with rollup metadata.
- messages: one row per kept (user|assistant text) turn.
- messages_fts: FTS5 virtual table indexing message content for keyword search.

Ingestion is idempotent — re-running on the same JSONL won't dupe rows
because we PRIMARY KEY on message uuid (Claude Code's stable per-message ID).
That lets us call the ingest hook on every Stop event without worrying.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .session_parser import Message, SessionSummary

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    project_slug      TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT NOT NULL,
    turn_count        INTEGER NOT NULL,
    tool_call_freq    TEXT NOT NULL,        -- JSON
    git_branch        TEXT,
    cwd               TEXT,
    last_synced_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_started
    ON sessions(project_slug, started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    uuid          TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_uuid   TEXT,
    timestamp     TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_ts
    ON messages(session_id, timestamp);

-- FTS5 virtual table over message content.
-- 'porter' tokenizer handles English stemming (DB / database / databases all match).
-- We store nothing extra (content='messages') so it stays content-less and
-- references the parent table via rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='rowid', tokenize='porter');

-- Triggers keep FTS in sync with messages.
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


class RecallDB:
    """Thin wrapper around a per-project SQLite recall.db."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        cur = self._conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        if row is None:
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── Ingestion ────────────────────────────────────────────────────────

    def ingest(self, summary: SessionSummary, messages: list[Message]) -> dict[str, int]:
        """Insert/update a session + its messages. Returns counts.

        Idempotent: messages keyed by uuid so re-running on the same
        transcript is a no-op.
        """
        import json as _json

        inserted_msgs = 0
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                  (session_id, project_slug, started_at, ended_at, turn_count, tool_call_freq, git_branch, cwd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  ended_at = excluded.ended_at,
                  turn_count = excluded.turn_count,
                  tool_call_freq = excluded.tool_call_freq,
                  git_branch = excluded.git_branch,
                  cwd = excluded.cwd,
                  last_synced_at = datetime('now')
                """,
                (
                    summary.session_id,
                    summary.project_slug,
                    summary.started_at,
                    summary.ended_at,
                    summary.turn_count,
                    _json.dumps(summary.tool_call_freq, sort_keys=True),
                    summary.git_branch,
                    summary.cwd,
                ),
            )

            for m in messages:
                cur = conn.execute(
                    """
                    INSERT INTO messages (uuid, session_id, parent_uuid, timestamp, role, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uuid) DO NOTHING
                    """,
                    (m.uuid, summary.session_id, m.parent_uuid, m.timestamp, m.role, m.content),
                )
                if cur.rowcount > 0:
                    inserted_msgs += 1

        return {"messages_inserted": inserted_msgs, "messages_total": len(messages)}

    # ── Read APIs (used by CLI tools) ────────────────────────────────────

    def search(
        self,
        query: str,
        project_slug: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """FTS5 BM25 keyword search.

        Returns up to `limit` snippets. Each row includes the message,
        a context snippet, and the parent session's metadata.

        `query` should already be FTS-formatted by the caller (e.g.
        `"database" OR "schema" OR "migration"`). We don't try to
        rewrite the user's intent — the agent constructs the query.
        """
        sql = """
        SELECT
            m.uuid AS message_uuid,
            m.session_id,
            m.timestamp,
            m.role,
            snippet(messages_fts, 0, '«', '»', '…', 12) AS snippet,
            bm25(messages_fts) AS score,
            s.project_slug,
            s.started_at AS session_started_at,
            s.git_branch
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        JOIN sessions s ON s.session_id = m.session_id
        WHERE messages_fts MATCH :query
        """
        params: dict[str, Any] = {"query": query}
        if project_slug:
            sql += " AND s.project_slug = :slug"
            params["slug"] = project_slug
        sql += " ORDER BY bm25(messages_fts) ASC LIMIT :limit"
        params["limit"] = limit

        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def list_recent_sessions(
        self,
        project_slug: str | None = None,
        limit: int = 10,
        min_turns: int = 2,
    ) -> list[dict[str, Any]]:
        """Recent sessions, newest first.

        `min_turns` filters out trivial single-exchange sessions (the
        Reddit author's "noise" filter — most one-turn sessions are
        accidental opens).
        """
        sql = """
        SELECT session_id, project_slug, started_at, ended_at, turn_count,
               tool_call_freq, git_branch, cwd
        FROM sessions
        WHERE turn_count >= :min_turns
        """
        params: dict[str, Any] = {"min_turns": min_turns}
        if project_slug:
            sql += " AND project_slug = :slug"
            params["slug"] = project_slug
        sql += " ORDER BY started_at DESC LIMIT :limit"
        params["limit"] = limit

        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_session_messages(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """All messages for a session, in chronological order."""
        sql = """
        SELECT uuid, parent_uuid, timestamp, role, content
        FROM messages
        WHERE session_id = :sid
        ORDER BY timestamp ASC
        """
        params: dict[str, Any] = {"sid": session_id}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit

        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def session_count(self, project_slug: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM sessions"
        params: tuple[Any, ...] = ()
        if project_slug:
            sql += " WHERE project_slug = ?"
            params = (project_slug,)
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def default_db_path(project_slug: str) -> Path:
    """Standard recall.db location for a project slug.

    On Windows this resolves to %USERPROFILE%\\.claude\\projects\\<slug>\\recall.db
    On macOS/Linux it's ~/.claude/projects/<slug>/recall.db
    """
    return Path.home() / ".claude" / "projects" / project_slug / "recall.db"
