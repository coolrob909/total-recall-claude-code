# Total Recall — Claude Code

**Persistent local + team memory for Claude Code sessions.**
SQLite + FTS5. Stdlib-only at runtime. No external services required for the personal-memory half. Postgres (Supabase or any compatible) for the optional team-recall half.

[![CI](https://github.com/REPLACE-WITH-YOUR-USERNAME/total-recall-claude-code/actions/workflows/ci.yml/badge.svg)](https://github.com/REPLACE-WITH-YOUR-USERNAME/total-recall-claude-code/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## What it does

Every Claude Code session leaves a transcript at `~/.claude/projects/<slug>/<uuid>.jsonl`. Total Recall hooks into Claude Code's session-stop lifecycle to:

1. **Parse the transcript** — strip thinking blocks, tool-use blocks, system metadata. Keep user + assistant prose.
2. **Redact secrets** — built-in JWT pattern plus any project-specific patterns you configure (passwords, test-account creds, etc.).
3. **Ingest into a local SQLite + FTS5 database** at `~/.claude/total-recall/recall.db`. Sub-millisecond full-text search across every session you've ever had.
4. **Optionally push a summary to a shared Postgres table** so your team sees what each engineer worked on most recently.

Then on the next session-start, the hook injects a short system-prompt prelude:

- A summary of the most recent prior session on the same project (so the next session knows what happened last time)
- A bulleted list of recent activity from your teammates (if team-recall is configured)

The result: your Claude Code sessions remember themselves, and your team has a passive awareness of what everyone else has been working on — without anyone having to write a status update.

---

## Install — pick one

### Option 1 — Direct clone (zero install)

Drop the repo into your project's `.claude/plugins/` directory. Claude Code's plugin convention picks up the hooks automatically.

```bash
cd /path/to/your/project
mkdir -p .claude/plugins
cd .claude/plugins
git clone https://github.com/REPLACE-WITH-YOUR-USERNAME/total-recall-claude-code.git total-recall
```

No `pip install`, no PATH changes, no Python venv. Total Recall is stdlib-only so it just works on whatever Python 3.10+ Claude Code is invoking.

Start a new Claude Code session in the project and the hooks fire.

### Option 2 — `pip install`

If you'd rather manage it as a Python dependency:

```bash
pip install total-recall-claude-code
```

Then create a thin shim plugin in your project so Claude Code's hook discovery still finds the entry points:

```bash
mkdir -p .claude/plugins/total-recall/hooks
cat > .claude/plugins/total-recall/hooks/on-session-start.py <<'PY'
#!/usr/bin/env python3
from total_recall.hooks.on_session_start import main
if __name__ == "__main__":
    main()
PY
cat > .claude/plugins/total-recall/hooks/on-session-stop.py <<'PY'
#!/usr/bin/env python3
from total_recall.hooks.on_session_stop import main
if __name__ == "__main__":
    main()
PY
```

(The clone approach is simpler. Pip-install is for users who want to vendor the package the conventional Python way.)

---

## What you get out of the box

Once installed, no configuration is needed for the personal-memory features. The next time you start a Claude Code session:

| File | What it is |
|---|---|
| `~/.claude/total-recall/recall.db` | SQLite + FTS5 database of every session |
| `~/.claude/projects/_total-recall-logs/*.log` | Hook diagnostics (errors here, never crashes) |
| `<plugin-dir>/tools/recall-search.py` | CLI: full-text search across all your sessions |
| `<plugin-dir>/tools/recall-list.py` | CLI: list recent sessions, optionally filtered by project |

Run the CLIs directly:

```bash
cd .claude/plugins/total-recall
python tools/recall-search.py "PostgREST FK cache"
python tools/recall-list.py --limit 10
```

Or, if you installed via pip:

```bash
total-recall-search "PostgREST FK cache"
total-recall-list --limit 10
```

---

## Team recall (optional)

Personal recall works offline with zero setup. Team recall adds a shared Postgres table so every team member sees recent activity from the rest of the team at the top of each session.

### Setup a Postgres table

Supabase works out of the box. Any Postgres-with-PostgREST host works too. Create the table:

```sql
CREATE TABLE IF NOT EXISTS total_recall_team_sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  engineer_id     text NOT NULL,
  project_slug    text NOT NULL,
  session_id      text NOT NULL,
  started_at      timestamptz NOT NULL,
  ended_at        timestamptz NOT NULL,
  turn_count      integer NOT NULL,
  git_branch      text,
  summary         text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trts_engineer_recent
  ON total_recall_team_sessions (engineer_id, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_trts_project_recent
  ON total_recall_team_sessions (project_slug, ingested_at DESC);

-- If using Supabase, enable RLS and add a policy:
ALTER TABLE total_recall_team_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Team members can read all" ON total_recall_team_sessions
  FOR SELECT USING (true);

-- Only the service_role JWT (used by the plugin) should INSERT.
-- service_role bypasses RLS by design.
```

### Configure the plugin

Three values:

| | What | How to get it |
|---|---|---|
| `TOTAL_RECALL_TEAM_URL` | Supabase / PostgREST URL | Supabase Console → Project Settings → API |
| `TOTAL_RECALL_TEAM_KEY` | `service_role` JWT | Same page, ⚠️ keep secret |
| `TOTAL_RECALL_USER_ID` | Your handle, e.g. `alex`, `sam` | Choose any short lowercase id |

**Via env vars** (good for CI / one-off):

```bash
export TOTAL_RECALL_TEAM_URL="https://your-project.supabase.co"
export TOTAL_RECALL_TEAM_KEY="<jwt>"
export TOTAL_RECALL_USER_ID="alex"
```

**Via `.claude/settings.local.json`** (good for per-project, gitignored):

```json
{
  "total_recall": {
    "team_recall": {
      "url": "https://your-project.supabase.co",
      "service_key": "<jwt>",
      "engineer_id": "alex"
    }
  }
}
```

The plugin will push redacted session summaries to your team Postgres at session-stop, and pull the most recent activity from the rest of the team at session-start.

---

## Customizing the redactor

The session content that lands in the SQLite recall.db (and, optionally, the team Postgres) is run through a regex redactor first. Out of the box, one pattern is built in:

- **JWT-shape** — three base64url segments separated by dots, header + payload starting with `eyJ`. Catches OAuth tokens, Supabase service JWTs, anything else of that shape.

To add your own project-specific patterns (passwords, test-account credentials, hostnames you don't want in the recall db), create a JSON file:

```json
[
  {
    "pattern": "MyTestPassword[A-Za-z0-9]+",
    "replacement": "<REDACTED-test-cred>"
  },
  {
    "pattern": "(testuser@example\\.com)\\s*/\\s*\\S+",
    "replacement": "\\1 / <REDACTED>"
  }
]
```

Save it at one of (first match wins):

1. `$TOTAL_RECALL_REDACTIONS` (env var pointing at any JSON file)
2. `~/.claude/total-recall/redactions.json` (your home dir, all projects)
3. `<project-root>/.claude/total-recall/redactions.json` (per-project override)

Patterns are Python regex. `replacement` may include numbered groups (`\\1`, `\\2`).

A misconfigured `redactions.json` will be **silently skipped** — the redactor never blocks session ingestion on user-config errors. To verify your patterns loaded, run the test suite with the env var set.

---

## How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Claude Code session                                              │
│    ┌──────────────┐    ┌──────────────┐    ┌─────────────────┐   │
│    │ SessionStart │    │   (working)  │    │  SessionStop    │   │
│    │     hook     │    │              │    │      hook       │   │
│    └──────┬───────┘    └──────────────┘    └────────┬────────┘   │
└───────────┼───────────────────────────────────────────┼──────────┘
            │                                           │
            │ inject system-prompt prelude:             │ ingest JSONL,
            │  - last session for this project          │  redact secrets,
            │  - recent team activity (if configured)   │  write SQLite + (optional) Postgres
            │                                           │
            ▼                                           ▼
   ┌──────────────────────┐               ┌──────────────────────┐
   │ ~/.claude/total-     │ ◀──────────── │  total-recall/       │
   │   recall/recall.db   │   reads       │  hooks/on-session-   │
   │   (SQLite + FTS5)    │   for next    │       stop.py        │
   └──────────────────────┘    session    └──────────┬───────────┘
                                                     │
                                                     │ (if configured)
                                                     ▼
                              ┌──────────────────────────────────────┐
                              │ Postgres / Supabase                  │
                              │ total_recall_team_sessions           │
                              └──────────────────────────────────────┘
```

**Personal recall (no setup):** every session's transcript → parsed → redacted → SQLite + FTS5 → searchable.

**Team recall (opt-in):** the same session-stop hook also pushes a redacted summary to a Postgres table that the rest of the team reads at their next session-start.

Both layers are best-effort and never crash the Claude Code session. All hook bodies are wrapped in `try/except` with errors logged to `~/.claude/projects/_total-recall-logs/*.log`.

---

## Configuration reference

| Env var | What | Default |
|---|---|---|
| `TOTAL_RECALL_TEAM_URL` | Postgres / PostgREST base URL for team-recall | (team-recall disabled) |
| `TOTAL_RECALL_TEAM_KEY` | service_role JWT for team-recall | (team-recall disabled) |
| `TOTAL_RECALL_USER_ID` | Your handle in team-recall (e.g. `alex`) | derived from `git config user.name`, or `unknown` |
| `TOTAL_RECALL_REDACTIONS` | Path to a custom `redactions.json` | none |

| File path | What |
|---|---|
| `~/.claude/total-recall/recall.db` | Personal SQLite + FTS5 db |
| `~/.claude/total-recall/redactions.json` | User-level custom redaction patterns |
| `<project>/.claude/total-recall/redactions.json` | Project-scoped redaction overrides |
| `<project>/.claude/settings.local.json` | Per-project team-recall config (gitignored) |
| `~/.claude/projects/_total-recall-logs/*.log` | Hook diagnostics |

---

## Troubleshooting

### "Why isn't anything appearing at the start of new sessions?"

1. Check that the hooks fired: `ls -la ~/.claude/projects/_total-recall-logs/` should have recent files.
2. Check that the SQLite db has rows: `sqlite3 ~/.claude/total-recall/recall.db "SELECT COUNT(*) FROM sessions;"`
3. Open one of the log files for stack traces.

### "How do I clear my recall history?"

Delete `~/.claude/total-recall/recall.db`. The next session will start a fresh one.

### "How do I clear a single project's history?"

```bash
sqlite3 ~/.claude/total-recall/recall.db "DELETE FROM sessions WHERE project_slug = 'C--your-project-slug';"
sqlite3 ~/.claude/total-recall/recall.db "DELETE FROM messages WHERE session_id NOT IN (SELECT session_id FROM sessions);"
```

The `project_slug` is the escaped-path form Claude Code uses (e.g. `C--Users-alex-projects-my-app`). Find your slug via `ls ~/.claude/projects/`.

### "Does this work on Windows / WSL / macOS / Linux?"

Yes — CI runs all four matrix combinations of Python 3.10/3.11/3.12/3.13 × Ubuntu / macOS / Windows. The only OS-specific bit is the project-slug escape format, which is handled by Claude Code's own path-naming and not by Total Recall.

### "How big does recall.db get?"

About 1–2 MB per hundred sessions on average. The FTS5 index roughly doubles raw transcript size. A year of heavy use lands well under a hundred MB.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests run in under two seconds and have no dependencies — no excuse to skip them in a PR.

## License

[MIT](LICENSE). Use it for anything, including commercial work. No warranty.

## Acknowledgements

This started as an internal plugin built for a private codebase that had a few months of use under real conditions before being extracted. The extraction kept the well-tested core (SQLite + FTS5 schema, session parser, hook structure) and replaced the project-specific defaults (hardcoded credentials, internal URLs, named teammates) with user-configurable equivalents.
