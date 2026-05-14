#!/usr/bin/env python3
"""
check-team-memory.py — search the git-versioned team-memory library for
entries relevant to a topic the agent is about to act on.

Used by the `check-team-memory` skill BEFORE the agent does risky work
(PostgREST nested selects, prod migrations, _shared/ module changes,
destructive SQL, deno cache clears, etc). Surfaces existing lessons,
runbooks, and decisions so the team's accumulated discipline kicks in
preemptively rather than after a repeat mistake.

This is a simple keyword scorer — not FTS, not embeddings. The team-memory
library is small enough (~30 active files at time of writing) that a
file-grep + score is fast and debuggable.

Usage:
    check-team-memory.py "postgrest nested" "fk relationship"
    check-team-memory.py --limit 5 "deno cache" "_shared module"
    check-team-memory.py --json "prod migration"

Score is sum of:
- Distinct keyword matches in body × 1
- Distinct keyword matches in filename × 5  (filename match is strong signal)
- Distinct keyword matches in title (first H1) × 3
- Direct presence in INDEX.md categorization × 2

Returns a ranked list with title + relevant excerpt so the agent can
decide which file to read in full.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def find_team_memory_dir(start: Path) -> Path | None:
    """Walk up from `start` until we find `.claude/team-memory/`."""
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        candidate = ancestor / ".claude" / "team-memory"
        if candidate.is_dir():
            return candidate
    return None


@dataclass
class Hit:
    path: Path
    rel_path: str
    title: str
    score: float
    excerpt: str
    matched_keywords: list[str] = field(default_factory=list)


def extract_title(content: str) -> str:
    """First H1 in the file, falling back to filename-derived title."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line.lstrip("# ").strip()
    return ""


def best_excerpt(content: str, keywords: list[str], window: int = 240) -> str:
    """Find the densest window of text containing the most keywords.

    Returns up to `window` chars centered on the first paragraph that hits
    a keyword. Falls back to the file's first non-header paragraph.
    """
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)

    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    # Skip shebang / front matter / pure header blocks
    paragraphs = [p for p in paragraphs if not p.startswith(("---", "**Status**", "**Last verified**", "**Severity**"))]

    best_para = ""
    best_count = -1
    for p in paragraphs:
        # Skip pure header lines
        stripped = p.lstrip("#").strip()
        if not stripped or len(stripped) < 30:
            continue
        matches = len(pattern.findall(p))
        if matches > best_count:
            best_count = matches
            best_para = p
            if best_count >= 3:
                break  # good enough

    if not best_para:
        # No keyword match — fall back to first prose paragraph
        for p in paragraphs:
            if not p.startswith("#") and len(p) > 30:
                best_para = p
                break

    excerpt = best_para[:window]
    if len(best_para) > window:
        excerpt = excerpt.rstrip() + " …"
    return excerpt.replace("\n", " ").strip()


def score_file(path: Path, keywords: list[str]) -> Hit | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    title = extract_title(content)
    title_lower = title.lower()
    body_lower = content.lower()
    name_lower = path.stem.lower()

    score = 0.0
    matched: set[str] = set()
    for kw in keywords:
        kw_lower = kw.lower()
        # Filename match (strongest signal)
        if kw_lower in name_lower:
            score += 5.0
            matched.add(kw)
        # Title match
        if kw_lower in title_lower:
            score += 3.0
            matched.add(kw)
        # Body matches (count distinct keyword presence, not occurrences)
        if kw_lower in body_lower:
            score += 1.0
            matched.add(kw)

    if score <= 0.0:
        return None

    excerpt = best_excerpt(content, keywords)
    return Hit(
        path=path,
        rel_path="",  # filled by caller
        title=title or path.stem,
        score=score,
        excerpt=excerpt,
        matched_keywords=sorted(matched),
    )


def search(team_dir: Path, keywords: list[str], limit: int) -> list[Hit]:
    """Walk lessons/, runbooks/, decisions/ — skip _archive/ for pre-flight
    (archive is historical context, less actionable than current entries).
    """
    targets = [team_dir / "lessons", team_dir / "runbooks", team_dir / "decisions"]
    hits: list[Hit] = []
    for d in targets:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            if f.name == "README.md":
                continue
            h = score_file(f, keywords)
            if h is not None:
                h.rel_path = str(f.relative_to(team_dir.parent.parent))
                hits.append(h)

    hits.sort(key=lambda h: -h.score)
    return hits[:limit]


def emit_text(hits: list[Hit], keywords: list[str]) -> None:
    if not hits:
        sys.stdout.write(f"No team-memory entries match: {' '.join(keywords)}\n")
        sys.stdout.write("(Searched lessons/, runbooks/, decisions/. Archive intentionally excluded.)\n")
        return

    sys.stdout.write(f"Found {len(hits)} relevant entry(ies) for: {' '.join(keywords)}\n\n")
    for i, h in enumerate(hits, 1):
        sys.stdout.write(f"{i}. {h.title}\n")
        sys.stdout.write(f"   path: {h.rel_path}\n")
        sys.stdout.write(f"   matched: {', '.join(h.matched_keywords)} (score {h.score:.0f})\n")
        sys.stdout.write(f"   excerpt: {h.excerpt}\n\n")

    sys.stdout.write("Read the full file via Read tool before proceeding with risky work.\n")


def emit_json(hits: list[Hit]) -> None:
    payload = [
        {
            "title": h.title,
            "path": h.rel_path,
            "score": h.score,
            "matched_keywords": h.matched_keywords,
            "excerpt": h.excerpt,
        }
        for h in hits
    ]
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Search the team-memory library for entries relevant to a topic.",
    )
    p.add_argument("keywords", nargs="+", help="One or more keywords / phrases")
    p.add_argument("--limit", type=int, default=5, help="Max results (default 5)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    team_dir = find_team_memory_dir(Path.cwd())
    if team_dir is None:
        print(
            "ERROR: could not locate `.claude/team-memory/` from this directory.\n"
            "Run from inside a git repo with a .claude/team-memory/ directory.",
            file=sys.stderr,
        )
        return 3

    hits = search(team_dir, args.keywords, args.limit)

    if args.json:
        emit_json(hits)
    else:
        emit_text(hits, args.keywords)

    return 0


if __name__ == "__main__":
    sys.exit(main())
