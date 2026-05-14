# Contributing to Total Recall

Thanks for considering a contribution. This document is short on purpose.

## Reporting bugs

Open an issue with:

- Your platform (Windows / macOS / Linux) and Python version
- The Claude Code version (`claude --version`)
- A minimal reproduction or the failing log lines from
  `~/.claude/projects/_total-recall-logs/*.log`

## Submitting changes

1. Fork, create a branch off `main`.
2. Add or update tests for any behavioral change. The whole project is
   stdlib-only and `tests/test_recall.py` runs in under two seconds — no
   excuse to skip them.
3. Run the tests before pushing:
   ```bash
   python -m unittest tests/test_recall.py
   ```
4. Open a PR with a clear description of what changed and why.

## Design constraints (please respect)

- **Stdlib-only at runtime.** No `requirements.txt`. The session-stop hook
  runs on every Claude Code session-end and absolutely cannot drag in a
  surprise dependency. The only outbound network call (team-recall push)
  uses `urllib.request` with manual JSON.
- **Hooks must never raise.** Misconfigured or broken plugin code that
  raises in a hook can crash the Claude Code session. Every hook wraps
  its body in a top-level `try/except`, logs to
  `~/.claude/projects/_total-recall-logs/*.log`, and exits 0.
- **Redaction is a security boundary.** Anything that lands in the SQLite
  recall db or gets pushed to team-recall has already been run through
  `redact_secrets()`. Don't bypass it. If you need a new built-in
  pattern, add a test.

## Scope

This plugin is intentionally small. It does ONE thing: ingest Claude Code
session transcripts, store them locally (and optionally on a shared
Postgres), and surface them at the start of the next session. If you
want to add a feature, ask in an issue first whether it belongs here or
in a separate plugin that consumes the same recall db.

## Release process

1. Update `CHANGELOG.md` with the new version's changes.
2. Bump `version` in `pyproject.toml`.
3. Tag the release: `git tag -a v0.x.y -m "..."` then `git push --tags`.
4. (Optional, once we publish to PyPI) `python -m build && twine upload dist/*`.
