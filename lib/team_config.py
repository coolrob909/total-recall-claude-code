"""
team_config.py — locate and read team-recall config without coupling to a
specific config file format.

Resolution order:

1. Environment variables (highest priority, useful for CI / one-off runs):
     TOTAL_RECALL_TEAM_URL  — Supabase URL (e.g. https://your-project.supabase.co)
     TOTAL_RECALL_TEAM_KEY  — Supabase service-role JWT
     TOTAL_RECALL_USER_ID   — your user label (e.g. 'alex', 'sam')

2. `.claude/settings.local.json` in the repo root (gitignored), under
   `total_recall.team_recall`:
     {
       "total_recall": {
         "team_recall": {
           "url": "https://your-project.supabase.co",
           "service_key": "<JWT>",
           "engineer_id": "alex"
         }
       }
     }

3. Git config user.name as a fallback for engineer_id only.

Returns a TeamRecallConfig with all three fields set, or None if any of
url / service_key are missing — in which case the caller should treat
team-recall as disabled and continue without it (the local SQLite path
still works fine).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TeamRecallConfig:
    url: str
    service_key: str
    engineer_id: str


def _read_settings_local(start: Path) -> dict | None:
    """Find .claude/settings.local.json, preferring the git repo root."""
    # 1. Git root — authoritative location
    root = _git_toplevel(start)
    if root:
        candidate = root / ".claude" / "settings.local.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None  # git root exists but no config file — don't walk up

    # 2. Non-git fallback: walk up from start (original behavior)
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        candidate = ancestor / ".claude" / "settings.local.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


def _resolve_project_root(
    *,
    cwd_hint: str | None = None,
    payload: dict | None = None,
) -> Path:
    """Best-effort resolution of the project root directory.

    Priority: explicit cwd_hint > payload['cwd'] > Path.cwd().
    The returned path is always resolved (absolute, no symlinks).
    """
    if cwd_hint:
        p = Path(cwd_hint).resolve()
        if p.is_dir():
            return p
    if payload:
        raw = payload.get("cwd")
        if isinstance(raw, str) and raw:
            p = Path(raw).resolve()
            if p.is_dir():
                return p
    return Path.cwd().resolve()


def _git_toplevel(start: Path) -> Path | None:
    """Return the git repo root, or None if not in a git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _git_user_name(start: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            # Lower-case + simplify to letters/numbers/dash
            name = out.stdout.strip().lower()
            return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or None
    except Exception:
        pass
    return None


def load_team_recall_config(cwd: Path | None = None) -> TeamRecallConfig | None:
    """Resolve the config or return None if team-recall is not configured.

    Callers that get None should treat team-recall as disabled (continue
    with the local-only path). Never raise — config errors are common and
    must not break sessions.
    """
    cwd = cwd or Path.cwd()

    # 1. Env vars
    env_url = os.environ.get("TOTAL_RECALL_TEAM_URL")
    env_key = os.environ.get("TOTAL_RECALL_TEAM_KEY")
    env_eid = os.environ.get("TOTAL_RECALL_USER_ID")

    # 2. settings.local.json
    settings_url = settings_key = settings_eid = None
    s = _read_settings_local(cwd)
    if s:
        block = (s.get("total_recall") or {}).get("team_recall") or {}
        settings_url = block.get("url") or None
        settings_key = block.get("service_key") or None
        settings_eid = block.get("engineer_id") or None

    url = env_url or settings_url
    key = env_key or settings_key
    eid = env_eid or settings_eid or _git_user_name(cwd) or "unknown"

    if not url or not key:
        return None

    return TeamRecallConfig(url=url.rstrip("/"), service_key=key, engineer_id=eid)
