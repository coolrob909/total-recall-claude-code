#!/usr/bin/env python3
"""
promote-lesson.py — draft a `.claude/team-memory/lessons/<slug>.md` from the
current session.

Phase 2A of total-recall. This tool does NOT auto-commit anything — it writes
a draft markdown file in the right place and prints next steps. The engineer
reviews, edits if needed, stages, and PRs it (the standard flow).

Inputs:
  --slug <kebab-case-id>      file name root (becomes <slug>.md)
  --title <Title>             human-readable title (top of file)
  --symptom <text>            one-paragraph problem statement
  --root-cause <text>         one-paragraph cause
  --fix <text>                concrete steps to avoid the trap
  --example <text>            real PR/commit/session reference
  --severity <low|med|high>   optional, defaults to medium
  --ci-blind <text>           optional explanation of why CI doesn't catch it

Inputs are read from CLI flags rather than parsed from the session because
the agent will invoke this tool with structured args after analyzing the
session itself — that gives the agent control over what gets recorded.

Output:
  Writes to <repo-root>/.claude/team-memory/lessons/<slug>.md
  Refuses to overwrite an existing file (use --force-overwrite to override)
  Prints the suggested git command and next steps

Exit codes:
  0 — file written successfully
  2 — invalid input (missing required arg, file exists, etc.)
  3 — repo root not found
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` until we find a `.claude/team-memory/lessons/` dir.

    Lets the tool be invoked from anywhere in the worktree.
    """
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        if (ancestor / ".claude" / "team-memory" / "lessons").is_dir():
            return ancestor
    return None


SKELETON = """\
# {title}

**Status**: active
**Last verified**: {today}
**Severity**: {severity}

## Symptom
{symptom}

## Root cause
{root_cause}

## Fix / verification before pushing
{fix}

## Real example
{example}
"""

CI_BLIND_SECTION = """
## Why CI doesn't catch it
{ci_blind}
"""

RELATED_FOOTER = """
## Related
- *(add cross-references to other lessons / runbooks / decisions as needed)*
"""


def slugify(s: str) -> str:
    """Defensive slug normalization (the agent should pass clean slugs already)."""
    s = s.strip().lower()
    out = []
    last_dash = False
    for c in s:
        if c.isalnum():
            out.append(c)
            last_dash = False
        else:
            if not last_dash:
                out.append("-")
                last_dash = True
    result = "".join(out).strip("-")
    return result or "untitled"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Draft a team-memory lesson file from the current session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--slug", required=True, help="kebab-case file root, e.g. 'postgrest-needs-real-fk'")
    p.add_argument("--title", required=True, help="human-readable title (top of file)")
    p.add_argument("--symptom", required=True, help="one-paragraph problem statement")
    p.add_argument("--root-cause", dest="root_cause", required=True, help="one-paragraph cause")
    p.add_argument("--fix", required=True, help="concrete verification steps")
    p.add_argument("--example", required=True, help="real PR/commit/session reference")
    p.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--ci-blind", dest="ci_blind", default=None, help="optional: why CI doesn't catch it")
    p.add_argument("--force-overwrite", action="store_true", help="overwrite an existing lesson file")
    p.add_argument("--print-only", action="store_true", help="print to stdout, do not write the file")
    args = p.parse_args()

    repo_root = find_repo_root(Path.cwd())
    if repo_root is None:
        print(
            "ERROR: could not locate `.claude/team-memory/lessons/` from this directory.\n"
            "Run from inside a git repo with a .claude/team-memory/ directory.",
            file=sys.stderr,
        )
        return 3

    lessons_dir = repo_root / ".claude" / "team-memory" / "lessons"
    slug = slugify(args.slug)
    out_path = lessons_dir / f"{slug}.md"

    body = SKELETON.format(
        title=args.title.strip(),
        today=date.today().isoformat(),
        severity=args.severity,
        symptom=args.symptom.strip(),
        root_cause=args.root_cause.strip(),
        fix=args.fix.strip(),
        example=args.example.strip(),
    )
    if args.ci_blind:
        body += CI_BLIND_SECTION.format(ci_blind=args.ci_blind.strip())
    body += RELATED_FOOTER

    if args.print_only:
        sys.stdout.write(body)
        return 0

    if out_path.exists() and not args.force_overwrite:
        print(
            f"ERROR: {out_path} already exists. Pass --force-overwrite to replace it,\n"
            f"or pick a different --slug.",
            file=sys.stderr,
        )
        return 2

    out_path.write_text(body, encoding="utf-8")
    rel = out_path.relative_to(repo_root)

    print(f"✅ Drafted lesson at: {rel}\n")
    print("Next steps:")
    print("  1. Open the file and tighten any sections that need editing")
    print("  2. Add cross-references in the Related section if applicable")
    print("  3. Update `.claude/team-memory/INDEX.md` to list this lesson")
    print("  4. Stage + commit alongside the PR that fixes the underlying issue:")
    print("")
    print(f"     git add {rel} .claude/team-memory/INDEX.md")
    print(f'     git commit -m "feat(team-memory): add lesson — {args.title.strip()}"')
    print("")
    print("Reviewer will check: scoped to a real failure mode, objective tone,")
    print("no secrets, has a real example reference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
