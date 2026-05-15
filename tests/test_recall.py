"""
test_recall.py — unit tests for total-recall.

Run from the plugin dir:
    cd .claude/plugins/total-recall && python -m unittest tests/test_recall.py

Stdlib-only so we can run it anywhere Python 3.10+ is installed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from lib.recall_db import RecallDB  # noqa: E402
from lib.session_parser import Message, SessionSummary, parse_jsonl, redact_secrets  # noqa: E402
from lib.team_config import load_team_recall_config  # noqa: E402


def _make_jsonl(tmpdir: Path, lines: list[dict]) -> Path:
    p = tmpdir / "sample.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    return p


class TestParser(unittest.TestCase):
    def test_strips_thinking_and_tool_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project_dir = tmp / "C--proj-slug"
            project_dir.mkdir()
            jsonl = _make_jsonl(
                project_dir,
                [
                    {"type": "queue-operation", "timestamp": "2026-05-06T10:00:00Z"},
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": None,
                        "timestamp": "2026-05-06T10:00:01Z",
                        "sessionId": "sess-1",
                        "gitBranch": "feat/x",
                        "cwd": "/repo",
                        "message": {"role": "user", "content": "Hello, can you help me?"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "timestamp": "2026-05-06T10:00:02Z",
                        "sessionId": "sess-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "...long reasoning..."},
                                {"type": "text", "text": "Sure, what do you need?"},
                                {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
                            ],
                        },
                    },
                    {"type": "system", "timestamp": "2026-05-06T10:00:03Z"},
                ],
            )
            summary, msgs = parse_jsonl(jsonl)

        self.assertEqual(summary.session_id, "sess-1")
        self.assertEqual(summary.project_slug, "C--proj-slug")
        self.assertEqual(summary.turn_count, 2)
        self.assertEqual(summary.tool_call_freq, {"Bash": 1})
        self.assertEqual(summary.git_branch, "feat/x")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].role, "user")
        self.assertEqual(msgs[0].content, "Hello, can you help me?")
        self.assertEqual(msgs[1].role, "assistant")
        # Only the text block survives — thinking + tool_use stripped.
        self.assertEqual(msgs[1].content, "Sure, what do you need?")

    def test_tolerates_truncated_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project_dir = tmp / "proj"
            project_dir.mkdir()
            p = project_dir / "trunc.jsonl"
            with p.open("w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "u1",
                            "timestamp": "2026-05-06T10:00:00Z",
                            "sessionId": "s1",
                            "message": {"role": "user", "content": "first"},
                        }
                    )
                    + "\n"
                )
                fh.write('{"type":"assistant","uuid":"a1","timestamp":"2026-05-06T10:00:01Z","sess')
            summary, msgs = parse_jsonl(p)
        self.assertEqual(summary.session_id, "s1")
        self.assertEqual(len(msgs), 1)


class TestRecallDB(unittest.TestCase):
    def _make_db(self) -> tuple[RecallDB, Path]:
        td = Path(tempfile.mkdtemp())
        db = RecallDB(td / "recall.db")
        return db, td

    def test_ingest_idempotent(self) -> None:
        db, _ = self._make_db()
        try:
            summary = SessionSummary(
                session_id="s1",
                project_slug="proj",
                started_at="2026-05-06T10:00:00Z",
                ended_at="2026-05-06T10:01:00Z",
                turn_count=2,
                tool_call_freq={"Bash": 1},
                git_branch="main",
                cwd="/r",
            )
            msgs = [
                Message(
                    uuid="u1",
                    parent_uuid=None,
                    timestamp="2026-05-06T10:00:00Z",
                    role="user",
                    content="hello",
                ),
                Message(
                    uuid="a1",
                    parent_uuid="u1",
                    timestamp="2026-05-06T10:00:30Z",
                    role="assistant",
                    content="hi there",
                ),
            ]
            r1 = db.ingest(summary, msgs)
            r2 = db.ingest(summary, msgs)  # same payload again
            self.assertEqual(r1["messages_inserted"], 2)
            self.assertEqual(r2["messages_inserted"], 0)  # no dupes
            self.assertEqual(db.session_count("proj"), 1)
        finally:
            db.close()

    def test_fts_search(self) -> None:
        db, _ = self._make_db()
        try:
            summary = SessionSummary(
                session_id="s1",
                project_slug="proj",
                started_at="2026-05-06T10:00:00Z",
                ended_at="2026-05-06T10:01:00Z",
                turn_count=2,
                tool_call_freq={},
                git_branch=None,
                cwd=None,
            )
            db.ingest(
                summary,
                [
                    Message(
                        uuid="u1",
                        parent_uuid=None,
                        timestamp="2026-05-06T10:00:00Z",
                        role="user",
                        content="we need to update the database schema",
                    ),
                    Message(
                        uuid="a1",
                        parent_uuid="u1",
                        timestamp="2026-05-06T10:00:30Z",
                        role="assistant",
                        content="run an ALTER TABLE migration on the rcs_saved_reports table",
                    ),
                ],
            )
            results = db.search('"schema" OR "migration"', project_slug="proj", limit=5)
            self.assertEqual(len(results), 2)
            # Both messages should match; the assistant message has both keywords (well, "migration").
            self.assertTrue(
                any(
                    "schema" in r["snippet"].lower() or "migration" in r["snippet"].lower()
                    for r in results
                )
            )
        finally:
            db.close()

    def test_list_recent_sessions_filters_low_turn(self) -> None:
        db, _ = self._make_db()
        try:
            # Two sessions: one with 1 turn (noise), one with 4.
            for sid, n in [("noise", 1), ("real", 4)]:
                summary = SessionSummary(
                    session_id=sid,
                    project_slug="proj",
                    started_at="2026-05-06T10:00:00Z",
                    ended_at="2026-05-06T10:01:00Z",
                    turn_count=n,
                    tool_call_freq={},
                    git_branch=None,
                    cwd=None,
                )
                msgs = [
                    Message(
                        uuid=f"{sid}-{i}",
                        parent_uuid=None,
                        timestamp=f"2026-05-06T10:0{i}:00Z",
                        role=("user" if i % 2 == 0 else "assistant"),
                        content=f"msg {i}",
                    )
                    for i in range(n)
                ]
                db.ingest(summary, msgs)
            recent = db.list_recent_sessions(project_slug="proj", limit=5, min_turns=2)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["session_id"], "real")
        finally:
            db.close()


class TestRedaction(unittest.TestCase):
    """The redactor ships with one built-in pattern (JWT shape) and supports
    user-configured patterns via redactions.json. These tests verify both.
    """

    def test_passes_through_unrelated_text(self) -> None:
        text = "Nothing sensitive here, just running a normal query."
        self.assertEqual(redact_secrets(text), text)

    def test_redacts_jwt_shape(self) -> None:
        # Sample JWT (HS256, 3 segments). NOT a real credential.
        sample_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        text = f"Here's the token: {sample_jwt} please use it"
        redacted = redact_secrets(text)
        self.assertNotIn("eyJzdWIi", redacted)
        self.assertIn("<JWT-REDACTED>", redacted)
        # Surrounding text preserved
        self.assertIn("Here's the token:", redacted)
        self.assertIn("please use it", redacted)

    def test_user_redactions_loaded_from_env_path(self) -> None:
        """Drop a redactions.json on disk, point TOTAL_RECALL_REDACTIONS at it,
        re-import the module, and verify the user pattern fires."""
        import importlib
        import json
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {"pattern": r"MySecret[A-Za-z0-9]+", "replacement": "<USER-REDACTED>"},
                    {
                        "pattern": r"(user@example\.com)\s*/\s*\S+",
                        "replacement": r"\1 / <PW-REDACTED>",
                    },
                ],
                f,
            )
            path = f.name
        try:
            os.environ["TOTAL_RECALL_REDACTIONS"] = path
            # Force the module to re-load the patterns at import time
            import lib.session_parser as sp

            importlib.reload(sp)
            text = "Found MySecretABC123 and user@example.com / hunter2 in the log."
            redacted = sp.redact_secrets(text)
            self.assertNotIn("MySecretABC123", redacted)
            self.assertIn("<USER-REDACTED>", redacted)
            self.assertIn("user@example.com", redacted)  # email preserved
            self.assertNotIn("hunter2", redacted)
            self.assertIn("<PW-REDACTED>", redacted)
        finally:
            os.environ.pop("TOTAL_RECALL_REDACTIONS", None)
            os.unlink(path)
            # Restore the module to default state for any later tests
            import lib.session_parser as sp

            importlib.reload(sp)

    def test_jwt_pattern_does_not_match_short_base64(self) -> None:
        # Short base64-looking strings should NOT trigger the JWT pattern.
        text = "Image data: eyJ123.eyJ456.short"
        redacted = redact_secrets(text)
        self.assertEqual(redacted, text)  # nothing should change

    def test_message_dataclass_applies_redaction(self) -> None:
        """Message.__post_init__ should run redact_secrets — verified with a JWT."""
        sample_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkFsaWNlIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        m = Message(
            uuid="u1",
            parent_uuid=None,
            timestamp="2026-05-07T10:00:00Z",
            role="user",
            content=f"my session token was {sample_jwt} ok",
        )
        self.assertNotIn("eyJzdWIi", m.content)
        self.assertIn("<JWT-REDACTED>", m.content)


class TestTeamConfig(unittest.TestCase):
    def test_returns_none_when_unconfigured(self) -> None:
        # Use a temp dir that has no .claude/settings.local.json AND clear env
        td = Path(tempfile.mkdtemp())
        os.environ.pop("TOTAL_RECALL_TEAM_URL", None)
        os.environ.pop("TOTAL_RECALL_TEAM_KEY", None)
        os.environ.pop("TOTAL_RECALL_USER_ID", None)
        cfg = load_team_recall_config(td)
        self.assertIsNone(cfg)

    def test_env_vars_take_precedence(self) -> None:
        td = Path(tempfile.mkdtemp())
        os.environ["TOTAL_RECALL_TEAM_URL"] = "https://env.example.com/"
        os.environ["TOTAL_RECALL_TEAM_KEY"] = "env-key"
        os.environ["TOTAL_RECALL_USER_ID"] = "env-engineer"
        try:
            cfg = load_team_recall_config(td)
            self.assertIsNotNone(cfg)
            assert cfg is not None
            self.assertEqual(cfg.url, "https://env.example.com")  # trailing slash stripped
            self.assertEqual(cfg.service_key, "env-key")
            self.assertEqual(cfg.engineer_id, "env-engineer")
        finally:
            os.environ.pop("TOTAL_RECALL_TEAM_URL", None)
            os.environ.pop("TOTAL_RECALL_TEAM_KEY", None)
            os.environ.pop("TOTAL_RECALL_USER_ID", None)

    def test_settings_local_json_is_read(self) -> None:
        os.environ.pop("TOTAL_RECALL_TEAM_URL", None)
        os.environ.pop("TOTAL_RECALL_TEAM_KEY", None)
        os.environ.pop("TOTAL_RECALL_USER_ID", None)
        td = Path(tempfile.mkdtemp())
        claude_dir = td / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.local.json").write_text(
            json.dumps(
                {
                    "total_recall": {
                        "team_recall": {
                            "url": "https://settings.example.com",
                            "service_key": "settings-key",
                            "engineer_id": "settings-engineer",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        # Run from inside the fake repo so the walk-up finds settings.local.json
        cfg = load_team_recall_config(td)
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.url, "https://settings.example.com")
        self.assertEqual(cfg.service_key, "settings-key")
        self.assertEqual(cfg.engineer_id, "settings-engineer")


class TestTeamActivityFormatter(unittest.TestCase):
    """Tests for the SessionStart team-activity block formatter (Phase 2C-extension)."""

    def setUp(self) -> None:
        # Hyphens in module names confuse normal `import`; load via runpy.
        import runpy

        ns = runpy.run_path(str(_PLUGIN_DIR / "hooks" / "on-session-start.py"))
        self._format_team_activity = ns["_format_team_activity"]
        self._lookback_days = ns["TEAM_ACTIVITY_LOOKBACK_DAYS"]

    def _session(self, *, eid: str, days_ago: float, branch: str = "feat/x", turns: int = 10):
        from datetime import datetime, timedelta, timezone

        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return {
            "session_id": f"sess-{eid}-{int(days_ago * 100)}",
            "engineer_id": eid,
            "project_slug": "C--proj-slug",
            "started_at": ts,
            "ended_at": ts,
            "turn_count": turns,
            "git_branch": branch,
            "cwd": "/repo",
        }

    def test_filters_out_current_engineer(self) -> None:
        sessions = [
            self._session(eid="robert", days_ago=0.5),
            self._session(eid="daisy", days_ago=1.0),
            self._session(eid="robert", days_ago=2.0),
        ]
        block = self._format_team_activity(sessions, current_engineer="robert")
        self.assertIn("daisy", block)
        # The bullet for the daisy line should appear; "robert" should not appear
        # as an engineer id (it may appear in the header, so check the bullet form)
        self.assertNotIn("**robert**", block)

    def test_returns_empty_when_only_self(self) -> None:
        sessions = [
            self._session(eid="robert", days_ago=0.5),
            self._session(eid="robert", days_ago=1.0),
        ]
        block = self._format_team_activity(sessions, current_engineer="robert")
        self.assertEqual(block, "")

    def test_filters_out_old_sessions(self) -> None:
        # Recency now uses ended_at, and we `continue` past stale rows
        # (rather than `break`) because list_recent_team_recall orders by
        # started_at — a stale row could appear before a fresh one.
        sessions = [
            self._session(eid="daisy", days_ago=1.0),
            self._session(eid="morgan", days_ago=self._lookback_days + 5),
            self._session(eid="casey", days_ago=2.0),  # fresh, after a stale row
        ]
        block = self._format_team_activity(sessions, current_engineer="robert")
        self.assertIn("daisy", block)
        self.assertIn("casey", block)  # not skipped despite earlier stale row
        self.assertNotIn("morgan", block)

    def test_renders_branch_and_turn_count(self) -> None:
        sessions = [self._session(eid="daisy", days_ago=0.5, branch="feat/awesome", turns=42)]
        block = self._format_team_activity(sessions, current_engineer="robert")
        self.assertIn("daisy", block)
        self.assertIn("`feat/awesome`", block)
        self.assertIn("42 turns", block)


class TestResolveJsonl(unittest.TestCase):
    """Tests for on-session-stop._resolve_jsonl — the 3-way path resolution."""

    def setUp(self) -> None:
        import runpy

        ns = runpy.run_path(str(_PLUGIN_DIR / "hooks" / "on-session-stop.py"))
        self._resolve_jsonl = ns["_resolve_jsonl"]

    def test_resolves_from_transcript_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            jsonl = tmp / "session.jsonl"
            jsonl.write_text("{}", encoding="utf-8")
            result = self._resolve_jsonl({"transcript_path": str(jsonl)})
            self.assertEqual(result, jsonl)

    def test_resolves_from_transcriptPath_camelCase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            jsonl = tmp / "session.jsonl"
            jsonl.write_text("{}", encoding="utf-8")
            result = self._resolve_jsonl({"transcriptPath": str(jsonl)})
            self.assertEqual(result, jsonl)

    def test_resolves_most_recent_jsonl_via_glob(self) -> None:
        """When session_id doesn't match a file, falls back to most recent .jsonl."""
        import time

        with tempfile.TemporaryDirectory() as td:
            # Build the ~/.claude/projects/<slug>/ structure
            home_dir = Path(td)
            proj_dir = home_dir / ".claude" / "projects" / "my-proj"
            proj_dir.mkdir(parents=True)
            old = proj_dir / "old-session.jsonl"
            old.write_text("{}", encoding="utf-8")
            time.sleep(0.05)  # ensure different mtime
            new = proj_dir / "new-session.jsonl"
            new.write_text("{}", encoding="utf-8")

            original_home = Path.home
            Path.home = staticmethod(lambda: home_dir)  # type: ignore[assignment]
            try:
                result = self._resolve_jsonl(
                    {
                        "project_slug": "my-proj",
                        # no session_id or transcript_path
                    }
                )
                self.assertEqual(result, new)
            finally:
                Path.home = original_home  # type: ignore[assignment]

    def test_returns_none_for_empty_payload(self) -> None:
        result = self._resolve_jsonl({})
        self.assertIsNone(result)

    def test_returns_none_for_nonexistent_transcript_path(self) -> None:
        result = self._resolve_jsonl({"transcript_path": "/does/not/exist.jsonl"})
        # transcript_path doesn't exist, falls through to slug-based lookup
        # which also fails → None
        self.assertIsNone(result)


class TestSessionStopMain(unittest.TestCase):
    """Tests for on-session-stop.main() — the full stdin→ingest pipeline."""

    def setUp(self) -> None:
        import runpy

        self._ns = runpy.run_path(str(_PLUGIN_DIR / "hooks" / "on-session-stop.py"))
        self._main = self._ns["main"]
        self._resolve_jsonl = self._ns["_resolve_jsonl"]

    def _make_transcript(self, tmpdir: Path, slug: str = "test-proj") -> Path:
        """Create a minimal valid JSONL transcript."""
        proj_dir = tmpdir / ".claude" / "projects" / slug
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "sess-001.jsonl"
        lines = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "timestamp": "2026-05-08T10:00:00Z",
                "sessionId": "sess-001",
                "gitBranch": "development",
                "cwd": "/repo",
                "message": {"role": "user", "content": "Hello world"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "timestamp": "2026-05-08T10:00:01Z",
                "sessionId": "sess-001",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hi there!"},
                    ],
                },
            },
        ]
        with jsonl.open("w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        return jsonl

    def test_local_ingest_writes_to_db(self) -> None:
        """The stop hook's pipeline (resolve → parse → ingest) writes to local SQLite.

        We test the pipeline components directly rather than calling main(),
        because main() captures its imports at runpy time and can't be
        monkey-patched from outside. This mirrors exactly what main() does
        internally: resolve_jsonl → parse_jsonl → RecallDB.ingest.
        """
        from lib.recall_db import RecallDB
        from lib.session_parser import parse_jsonl

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            jsonl = self._make_transcript(tmpdir, slug="test-proj")

            # Step 1: resolve (the hook would call _resolve_jsonl)
            resolved = self._resolve_jsonl({"transcript_path": str(jsonl)})
            self.assertIsNotNone(resolved)

            # Step 2: parse (the hook calls parse_jsonl)
            summary, messages = parse_jsonl(resolved)
            self.assertEqual(summary.session_id, "sess-001")
            self.assertEqual(len(messages), 2)

            # Step 3: ingest to local DB (the hook calls RecallDB.ingest)
            db_path = tmpdir / "recall.db"
            db = RecallDB(db_path)
            try:
                stats = db.ingest(summary, messages)
                self.assertEqual(stats["messages_inserted"], 2)
                self.assertEqual(db.session_count(), 1)
                msgs = db.get_session_messages("sess-001")
                self.assertEqual(len(msgs), 2)
                self.assertEqual(msgs[0]["content"], "Hello world")
                self.assertEqual(msgs[1]["content"], "Hi there!")
            finally:
                db.close()

    def test_returns_zero_on_empty_stdin(self) -> None:
        """main() should exit 0 even with empty stdin (no transcript to find)."""
        original_stdin = sys.stdin
        try:
            sys.stdin = __import__("io").StringIO("")
            exit_code = self._main()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(exit_code, 0)

    def test_returns_zero_on_invalid_json_stdin(self) -> None:
        """main() should exit 0 on malformed JSON (never fail the session)."""
        original_stdin = sys.stdin
        try:
            sys.stdin = __import__("io").StringIO("this is not json{{{")
            exit_code = self._main()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(exit_code, 0)

    def test_returns_zero_when_jsonl_not_found(self) -> None:
        """main() should exit 0 when payload points to nonexistent file."""
        payload = json.dumps({"transcript_path": "/tmp/nonexistent-session.jsonl"})
        original_stdin = sys.stdin
        try:
            sys.stdin = __import__("io").StringIO(payload)
            exit_code = self._main()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(exit_code, 0)


class TestSessionStopErrorIsolation(unittest.TestCase):
    """Verify that local ingest failure doesn't block the team-recall path,
    and that team-recall failure doesn't cause a non-zero exit."""

    def setUp(self) -> None:
        import runpy

        self._ns = runpy.run_path(str(_PLUGIN_DIR / "hooks" / "on-session-stop.py"))
        self._main = self._ns["main"]

    def test_never_exits_nonzero(self) -> None:
        """Even if every internal path fails, main() returns 0."""
        # Feed a payload that points to an existing but unparseable file
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            home_dir = tmpdir / "home"
            proj_dir = home_dir / ".claude" / "projects" / "bad-proj"
            proj_dir.mkdir(parents=True)
            bad_jsonl = proj_dir / "bad.jsonl"
            bad_jsonl.write_text("not valid jsonl at all\n{broken", encoding="utf-8")

            payload = json.dumps({"transcript_path": str(bad_jsonl)})
            original_stdin = sys.stdin
            try:
                sys.stdin = __import__("io").StringIO(payload)
                exit_code = self._main()
            finally:
                sys.stdin = original_stdin

            # Must NEVER exit non-zero — this is the core safety contract
            self.assertEqual(exit_code, 0)

    def test_team_recall_failure_is_nonfatal(self) -> None:
        """If team-recall raises, main() still returns 0."""
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            # Create a valid transcript
            proj_dir = tmpdir / ".claude" / "projects" / "team-fail-proj"
            proj_dir.mkdir(parents=True)
            jsonl = proj_dir / "sess-team-fail.jsonl"
            lines = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "parentUuid": None,
                    "timestamp": "2026-05-08T10:00:00Z",
                    "sessionId": "sess-team-fail",
                    "message": {"role": "user", "content": "test message"},
                },
            ]
            with jsonl.open("w", encoding="utf-8") as fh:
                for obj in lines:
                    fh.write(json.dumps(obj) + "\n")

            # Patch default_db_path to temp dir
            from lib import recall_db as recall_db_mod

            original_default = recall_db_mod.default_db_path
            recall_db_mod.default_db_path = lambda slug: tmpdir / "recall.db"  # type: ignore[assignment]

            # Patch team config to return a config (so team-recall path fires)
            # but patch ingest_session to raise
            import lib.team_config
            import lib.team_recall_client as trc_mod

            original_config = lib.team_config.load_team_recall_config
            original_ingest = trc_mod.ingest_session

            from lib.team_config import TeamRecallConfig

            fake_cfg = TeamRecallConfig(
                url="https://fake.example.com", service_key="fake", engineer_id="test"
            )
            lib.team_config.load_team_recall_config = lambda *a, **kw: fake_cfg  # type: ignore[assignment]
            trc_mod.ingest_session = lambda **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
                trc_mod.TeamRecallError("simulated network failure")
            )

            payload = json.dumps({"transcript_path": str(jsonl)})
            original_stdin = sys.stdin
            try:
                sys.stdin = __import__("io").StringIO(payload)
                exit_code = self._main()
            finally:
                sys.stdin = original_stdin
                recall_db_mod.default_db_path = original_default  # type: ignore[assignment]
                lib.team_config.load_team_recall_config = original_config  # type: ignore[assignment]
                trc_mod.ingest_session = original_ingest  # type: ignore[assignment]

            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
