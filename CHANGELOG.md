# Changelog

All notable changes to **Total Recall — Claude Code** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-13

Initial public release. Extracted from a private internal plugin that had been
running in production for several months and scrubbed of project-specific
defaults.

### Added

- **Personal recall** — every Claude Code session-stop hook ingests the just-
  finished transcript into a local SQLite + FTS5 database under
  `~/.claude/total-recall/recall.db`. The session-start hook auto-injects a
  short summary of the most recent prior session for the same project as a
  system-prompt prelude, so the next session begins with a memory of the
  last one.
- **Team recall (optional)** — point `TOTAL_RECALL_TEAM_URL` /
  `TOTAL_RECALL_TEAM_KEY` at a Supabase Postgres and every team member's
  session-stop hook also pushes a redacted summary to a shared table. The
  next session-start hook for any team member sees recent activity from
  the rest of the team.
- **Tools** — `recall-search`, `recall-list`, `team-recall-search`,
  `check-team-memory`, `promote-lesson`. Mapped to slash commands via
  Claude Code's plugin convention.
- **Redaction layer** — built-in JWT-shape pattern plus a user-configurable
  `redactions.json` file so you can scrub your own project-specific
  credentials before they hit the SQLite db or get pushed to team-recall.
